"""Shared hardship-recency logic.

A disclosed hardship should be acknowledged exactly ONCE — on the call
immediately following disclosure — not on every subsequent follow-up call.
Determines whether the current call is that one acknowledgment opportunity
("immediate") or whether the hardship was already addressed in an earlier
follow-up ("stale"), so the prompt can stop re-mentioning it.
"""

NOT_CONNECTED_DISPOSITIONS = {
    "no-answer", "no answer", "busy", "switched off", "not reachable",
    "disconnected", "incoming call barred", "failed", "invalid number", "wrong number",
}


def _flatten(record):
    if not isinstance(record, dict):
        return {}
    return record["output"] if isinstance(record.get("output"), dict) else record


def _disposition(record):
    flat = _flatten(record)
    for key in ("disposition", "result", "call_disposition"):
        value = flat.get(key)
        if value:
            return str(value).strip().lower()
    return ""


def _hardship_signal(record):
    """Return (is_hardship, hardship_type) for one call record."""
    flat = _flatten(record)
    signals = record.get("signals") if isinstance(record.get("signals"), dict) else {}
    if signals.get("hardship_detected") is True:
        return True, signals.get("hardship_type") or flat.get("hardship_type")
    hardship_type = flat.get("hardship_type") or signals.get("hardship_type")
    if hardship_type:
        return True, hardship_type
    if _disposition(record) == "financial hardship":
        return True, hardship_type
    return False, None


def get_hardship_recency(recent_interactions):
    """Scan history (newest first), skipping not-connected attempts entirely.

    Returns (recency, hardship_type):
      'immediate' — hardship was disclosed on the most recent CONNECTED call.
                    This call is the one opportunity to acknowledge it.
      'stale'     — hardship was disclosed on an OLDER connected call; a more
                    recent connected call has already happened since, so it
                    was already addressed. Do not re-mention it.
      None        — no hardship on record.
    """
    seen_connected = False
    for record in recent_interactions or []:
        if _disposition(record) in NOT_CONNECTED_DISPOSITIONS:
            continue
        is_hardship, hardship_type = _hardship_signal(record)
        if is_hardship:
            return ("immediate" if not seen_connected else "stale"), hardship_type
        seen_connected = True
    return None, None
