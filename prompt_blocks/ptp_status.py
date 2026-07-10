"""Shared PTP (promise-to-pay) status logic.

Deterministically compares the customer's recorded payment commitments against
today's date in Python, so the voice agent never has to do date arithmetic.
Produces the BROKEN / UPCOMING PTP section (and the repeated-promise trend
call-out) injected into a client's system prompt.
"""

import os
from datetime import datetime, timezone, timedelta

_IST = timezone(timedelta(hours=5, minutes=30))
_SYSTEM_DATE_FORMAT = "%A, %B %d, %Y %I:%M %p"


def _today_ist():
    """Today's datetime, honoring the SYSTEM_DATE test override used by the prompts."""
    system_date = os.getenv("SYSTEM_DATE")
    if system_date:
        try:
            return datetime.strptime(system_date, _SYSTEM_DATE_FORMAT).replace(tzinfo=_IST)
        except ValueError:
            pass
    return datetime.now(_IST)


def _parse_ptp_date(value):
    """Parse an ISO-8601 ptp_date (with or without time/offset). None if unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_IST)
    return parsed


def _extract_amount(record):
    for key in ("settlement_amt", "amount", "committed_amount"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def get_ptp_status(recent_interactions):
    """Scan interactions (newest first) for payment commitments and date-check them.

    Returns (status, ptp_date_display, amount, days, broken_count):
      status       : 'broken' / 'upcoming' / None (no dated commitment found)
      ptp_date_display : human-readable committed date (e.g. 'July 10, 2026') or None
      amount       : committed amount string or None
      days         : days overdue (broken) or days until due (upcoming); 0 if None status
      broken_count : count of ALL commitments in the window whose date has passed
    """
    today = _today_ist()
    status, display, amount, days = None, None, None, 0
    broken_count = 0
    for record in recent_interactions or []:
        if not isinstance(record, dict):
            continue
        ptp = _parse_ptp_date(record.get("ptp_date"))
        if ptp is None:
            continue
        is_past = ptp.date() < today.date()
        if is_past:
            broken_count += 1
        if status is None:
            amount = _extract_amount(record)
            display = ptp.strftime("%B %d, %Y")
            if is_past:
                status = "broken"
                days = (today.date() - ptp.date()).days
            else:
                status = "upcoming"
                days = (ptp.date() - today.date()).days
    return status, display, amount, days, broken_count


def build_ptp_status_block(status, ptp_date_display, amount, days, broken_count,
                           contact_type, default_language):
    """Build the PTP status section for the system prompt.

    Fires only for the primary contact. Returns "" when there is no dated
    commitment and no repeated-promise trend to surface.
    """
    if contact_type != "primary_contact_number":
        return ""
    if status is None and broken_count < 2:
        return ""

    amount_part = f" of {amount}" if amount else ""
    sections = []

    if status == "broken":
        day_word = "day" if days == 1 else "days"
        sections.append(f"""### ⚠️ BROKEN PAYMENT COMMITMENT — PRE-COMPUTED, DO NOT RE-CALCULATE

        The customer committed to a payment{amount_part} by {ptp_date_display}.
        That date was {days} {day_word} ago and the payment has NOT been received
        (this call being made means it was not paid).

        Handle it like this, in {default_language} (generate naturally — do not recite):
        - Acknowledge it exactly ONCE, factually and calmly — never accusatory:
          flavor: "Aapne {ptp_date_display} tak payment ka kaha tha, lekin abhi tak payment nahi aa payi."
        - Do NOT interrogate or demand explanations for the missed date.
        - Then move directly to re-securing a NEW, specific near date (within 15 days).
        - Do NOT reopen negotiation below whatever was already agreed.
        """)
    elif status == "upcoming":
        day_word = "day" if days == 1 else "days"
        due_phrase = "TODAY" if days == 0 else f"in {days} {day_word}, on {ptp_date_display}"
        sections.append(f"""### ✅ UPCOMING PAYMENT COMMITMENT — REMINDER FRAMING ONLY

        The customer has an existing commitment{amount_part} due {due_phrase}.
        The date has NOT passed. This is a REMINDER call, not an accountability call.

        Handle it like this, in {default_language}:
        - Reference the commitment warmly and confirm the plan is on track.
        - Re-explain the PhonePe steps if they need them.
        - Do NOT renegotiate, do NOT pressure, do NOT treat the promise as broken.
        """)

    if broken_count >= 2:
        sections.append(f"""### REPEATED PROMISES PATTERN — USE WITH CARE

        This customer has made {broken_count} payment commitments across recent calls
        whose dates passed without payment. You MAY reflect this pattern exactly ONCE,
        calmly and factually, in {default_language} — flavor:
        "Pichhle kai calls se aap keh rahe hain ki payment kar denge, lekin payment nahi aa payi."
        Never taunting, never counting promises out loud aggressively, never humiliating.
        Immediately after reflecting it, pivot to locking ONE concrete commitment today
        (specific date + amount). If active hardship emerges, drop this entirely.
        """)

    return "\n\n        ---\n\n        ".join(s.strip() for s in sections) + "\n\n        ---\n        "
