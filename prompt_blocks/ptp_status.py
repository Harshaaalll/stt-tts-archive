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


def _flatten_record(record):
    """DB rows for connected calls nest the analysis under an "output" key
    ({"output": {...}, "signals": {...}}); not-connected rows are flat.
    Return the dict that actually carries the call fields."""
    if isinstance(record, dict) and isinstance(record.get("output"), dict):
        return record["output"]
    return record if isinstance(record, dict) else {}


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
    broken_dates = set()  # dedupe: the same promise often appears in adjacent call rows
    for record in recent_interactions or []:
        record = _flatten_record(record)
        if not record:
            continue
        ptp = _parse_ptp_date(record.get("ptp_date"))
        if ptp is None:
            continue
        is_past = ptp.date() < today.date()
        if is_past:
            broken_dates.add(ptp.date())
        if status is None:
            amount = _extract_amount(record)
            display = ptp.strftime("%B %d, %Y")
            if is_past:
                status = "broken"
                days = (today.date() - ptp.date()).days
            else:
                status = "upcoming"
                days = (ptp.date() - today.date()).days
    return status, display, amount, days, len(broken_dates)


def resolve_commitments(commitments):
    """Deterministically resolve the structured `commitments` array (persisted by the
    analytics service in combined_intelligence) against today's date.

    Returns a dict:
      ptp        : same 5-tuple shape as get_ptp_status() — (status, display, amount,
                   days, broken_count) — derived from PTP-type entries with no outcome.
      settlement : dict describing the most recent open/grace/expired SETTLEMENT offer
                   with no outcome, or None if there isn't one.
      resolved   : list of commitment dicts whose outcome is "kept" or "payment_claimed".
    """
    empty = {"ptp": (None, None, None, 0, 0), "settlement": None, "resolved": []}
    if not commitments:
        return empty

    today = _today_ist()
    resolved = []
    ptp_pending = []
    settlement_pending = []
    for entry in commitments:
        if not isinstance(entry, dict):
            continue
        c_type = entry.get("type")
        if c_type not in ("PTP", "SETTLEMENT"):
            continue
        outcome = entry.get("outcome")
        if outcome in ("kept", "payment_claimed"):
            resolved.append(entry)
            continue
        if outcome:
            # unrecognized non-null outcome — treat as resolved, skip from pending
            continue
        if c_type == "PTP":
            due = _parse_ptp_date(entry.get("due_date"))
            if due is None:
                continue
            ptp_pending.append((due, entry))
        else:
            made_on = _parse_ptp_date(entry.get("made_on"))
            if made_on is None:
                continue
            settlement_pending.append((made_on, entry))

    status, display, amount, days, broken_count = None, None, None, 0, 0
    if ptp_pending:
        broken_dates = {due.date() for due, _ in ptp_pending if due.date() < today.date()}
        broken_count = len(broken_dates)
        due, entry = max(ptp_pending, key=lambda pair: pair[0])
        amount = str(entry.get("amount")) if entry.get("amount") is not None else None
        display = due.strftime("%B %d, %Y")
        if due.date() < today.date():
            status = "broken"
            days = (today.date() - due.date()).days
        else:
            status = "upcoming"
            days = (due.date() - today.date()).days

    settlement = None
    if settlement_pending:
        made_on, entry = max(settlement_pending, key=lambda pair: pair[0])
        days_since_offer = (today.date() - made_on.date()).days
        if days_since_offer <= 7:
            window_state = "open"
        elif days_since_offer <= 10:
            window_state = "grace"
        else:
            window_state = "expired"
        settlement = {
            "window_state": window_state,
            "days_since_offer": days_since_offer,
            "days_left": max(0, 7 - days_since_offer),
            "amount": str(entry.get("amount")) if entry.get("amount") is not None else None,
            "offer_date_display": made_on.strftime("%B %d, %Y"),
        }

    return {"ptp": (status, display, amount, days, broken_count), "settlement": settlement, "resolved": resolved}


def build_reality_check_block(resolved_state, contact_type, default_language):
    """Build the CALL-TIME REALITY CHECK section from resolve_commitments() output.

    Fires only for the primary contact, and only when there's a settlement offer
    state or resolved commitments to surface. Does NOT render the PTP part — that's
    already handled by build_ptp_status_block(); this avoids duplicating that section.
    """
    if contact_type != "primary_contact_number":
        return ""

    settlement = resolved_state.get("settlement")
    resolved = resolved_state.get("resolved") or []
    if not settlement and not resolved:
        return ""

    today = _today_ist()
    lines = [
        "### 📅 CALL-TIME REALITY CHECK — COMPUTED TODAY, OVERRIDES ANY CONFLICTING NARRATIVE TEXT",
        "",
        f"        TODAY'S DATE: {today.strftime('%B %d, %Y')}.",
        "",
    ]

    if settlement:
        amount_part = f" of {settlement['amount']}" if settlement.get("amount") else ""
        if settlement["window_state"] == "open":
            lines.append(
                f"        A settlement{amount_part} was offered on {settlement['offer_date_display']}. "
                f"The 7-day payment window is still OPEN — {settlement['days_left']} day(s) left. "
                f"Encourage the customer to complete payment within this window, in {default_language}."
            )
        elif settlement["window_state"] == "grace":
            days_before_lapse = max(0, 10 - settlement["days_since_offer"])
            lines.append(
                f"        A settlement{amount_part} was offered on {settlement['offer_date_display']}. "
                f"The formal 7-day window is OVER, but a hard-max grace period (10 days) is still running — "
                f"{days_before_lapse} day(s) left before the offer lapses entirely. "
                f"Communicate urgency, in {default_language}: this is the last chance to honor the offer."
            )
        else:
            days_lapsed = settlement["days_since_offer"] - 10
            lines.append(
                f"        A settlement{amount_part} was offered on {settlement['offer_date_display']}. "
                f"That offer LAPSED {days_lapsed} day(s) ago (past the 10-day hard max). "
                f"Do NOT re-offer it or reference it as still available — revert to full-outstanding "
                f"framing unless a fresh settlement is explicitly authorized, in {default_language}."
            )
        lines.append("")

    for entry in resolved:
        amount = entry.get("amount")
        amount_part = f"₹{amount} " if amount else ""
        due = entry.get("due_date") or entry.get("made_on")
        due_disp = due
        parsed_due = _parse_ptp_date(due)
        if parsed_due:
            due_disp = parsed_due.strftime("%B %d, %Y")
        if entry.get("outcome") == "payment_claimed":
            lines.append(
                f"        Customer claims the {amount_part}commitment due {due_disp} was paid — "
                f"verify politely, never demand it again as unpaid."
            )
        else:  # "kept"
            lines.append(
                f"        The {amount_part}commitment due {due_disp} was kept — acknowledge it "
                f"positively, do not treat it as outstanding."
            )
    if resolved:
        lines.append("")

    lines.append(
        "        ⚠️ These computed facts OVERRIDE the narrative, history digest, and any "
        "\"CUSTOMER INTELLIGENCE\" addendum on any conflict."
    )
    return "\n".join(lines) + "\n"


def build_ptp_status_block(status, ptp_date_display, amount, days, broken_count,
                           contact_type, default_language,
                           resecure_hint="a NEW, specific near date",
                           payment_hint="the payment steps"):
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

        ⚠️ THIS SECTION OVERRIDES the narrative, history and any "CUSTOMER INTELLIGENCE"
        addendum that describes this commitment as upcoming or on-track — those were
        written BEFORE the committed date passed. Today's date computation above is
        the truth. Treat the commitment as BROKEN.

        Handle it like this, in {default_language} (generate naturally — do not recite):
        - Acknowledge it exactly ONCE, factually and calmly — never accusatory:
          flavor: "Aapne {ptp_date_display} tak payment ka kaha tha, lekin abhi tak payment nahi aa payi."
        - Do NOT interrogate or demand explanations for the missed date.
        - Then move directly to re-securing {resecure_hint}.
        - Do NOT reopen negotiation below whatever was already agreed.
        """)
    elif status == "upcoming":
        day_word = "day" if days == 1 else "days"
        due_phrase = "TODAY" if days == 0 else f"in {days} {day_word}, on {ptp_date_display}"
        sections.append(f"""### ✅ UPCOMING PAYMENT COMMITMENT — REMINDER FRAMING ONLY

        The customer has an existing commitment{amount_part} due {due_phrase}.
        The date has NOT passed. This is a REMINDER call, not an accountability call.
        This section OVERRIDES any narrative or addendum framing that suggests otherwise.

        Handle it like this, in {default_language}:
        - Reference the commitment warmly and confirm the plan is on track.
        - Re-explain {payment_hint} if they need them.
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
