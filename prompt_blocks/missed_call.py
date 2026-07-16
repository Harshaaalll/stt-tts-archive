"""Shared missed-call acknowledgement logic.

Classifies the latest not-connected call attempt from interaction history and
builds the acknowledgement section injected into a client's system prompt.
Used by seed_fincap_contextual_prompt, fusion_contextual_prompt_emi and
fusion_contextual_prompt_explore.
"""

# Non-connected dispositions, bucketed by what the customer actually experienced
# (matched case-insensitively against the latest interaction records):
#   answer_missed     → phone rang, customer did not pick up → acknowledge subtly
#   phone_unreachable → call never rang (phone off / no network) → acknowledge differently
#   silent            → technical failure or wrong/invalid number → say nothing about it
ANSWER_MISSED_DISPOSITIONS = {"no-answer", "no answer", "busy"}
PHONE_UNREACHABLE_DISPOSITIONS = {"switched off", "not reachable", "disconnected", "incoming call barred"}
SILENT_DISPOSITIONS = {"failed", "invalid number", "wrong number"}
NOT_CONNECTED_DISPOSITIONS = (
    ANSWER_MISSED_DISPOSITIONS | PHONE_UNREACHABLE_DISPOSITIONS | SILENT_DISPOSITIONS
)


def _extract_disposition(record):
    if not isinstance(record, dict):
        return ""
    # Connected-call rows nest fields under "output"; not-connected rows are flat
    if isinstance(record.get("output"), dict):
        record = record["output"]
    for key in ("disposition", "result", "call_disposition"):
        value = record.get(key)
        if value:
            return str(value).strip().lower()
    return ""


def get_missed_call_context(recent_interactions):
    """Inspect recent interactions (newest first) and classify the latest call attempt.

    Returns (bucket, streak, latest_disposition) where bucket is one of
    'answer_missed' / 'phone_unreachable' / 'silent', or None when the latest
    call connected or there is no history. Streak counts consecutive
    not-connected attempts ending at the latest record.
    """
    if not recent_interactions:
        return None, 0, ""
    latest = _extract_disposition(recent_interactions[0])
    if latest not in NOT_CONNECTED_DISPOSITIONS:
        return None, 0, latest
    streak = 0
    for record in recent_interactions:
        if _extract_disposition(record) in NOT_CONNECTED_DISPOSITIONS:
            streak += 1
        else:
            break
    if latest in ANSWER_MISSED_DISPOSITIONS:
        bucket = "answer_missed"
    elif latest in PHONE_UNREACHABLE_DISPOSITIONS:
        bucket = "phone_unreachable"
    else:
        bucket = "silent"
    return bucket, streak, latest


def build_missed_call_block(bucket, streak, latest_disposition, contact_type,
                            default_language, next_phase_hint="the next phase of the call"):
    """Build the missed-call acknowledgement section for the system prompt.

    Only fires for the primary contact (references must never hear about call
    attempts; the co-applicant flow already explains unreachability). Returns
    "" when the latest call connected or the disposition should stay silent —
    except wrong/invalid number, which gets an identity-care hint instead.
    """
    if contact_type != "primary_contact_number" or bucket is None:
        return ""

    if bucket == "silent":
        if latest_disposition in ("wrong number", "invalid number"):
            return """### ⚠️ PREVIOUS ATTEMPT REACHED A WRONG/INVALID NUMBER — IDENTITY CARE

        A previous call attempt reached a wrong or invalid number. Do NOT mention any
        previous call attempt to this person. Be EXTRA careful during identity
        verification — confirm you are speaking with the right person before revealing
        anything about the loan.

        ---
        """
        return ""

    attempts_line = (
        f"The last {streak} call attempts to this customer did not connect."
        if streak > 1
        else "The last call attempt to this customer did not connect."
    )
    if bucket == "answer_missed":
        situation = "the phone rang but the customer did not pick up (No Answer / Busy)"
        example = (
            '"Maine aapko pehle bhi call kiya tha lekin baat nahi ho payi — '
            'shayad aap busy honge. Achha hua aaj baat ho gayi."'
        )
    else:
        situation = (
            "the customer's phone could not be reached "
            "(switched off / not reachable / call not going through)"
        )
        example = (
            '"Maine pehle bhi call karne ki koshish ki thi lekin aapka phone '
            'lag nahi pa raha tha. Achha hua aaj baat ho gayi."'
        )
    multiple_note = (
        '\n        Since there were multiple attempts, you may gently reflect that '
        '("kai baar koshish ki thi") — still warm, never a complaint.'
        if streak > 1
        else ""
    )
    return f"""### MISSED CALL ACKNOWLEDGEMENT — AFTER IDENTITY VERIFICATION

        {attempts_line} What happened: {situation}.

        Immediately after identity is confirmed — acknowledge this ONCE,
        briefly and warmly, in {default_language}. Never accusatory, never guilt-tripping,
        never demand a reason for the missed call. Generate naturally — do not recite.
        Example flavor: {example}{multiple_note}

        Rules:
        - Say it exactly ONCE per call. Never bring it up again later.
        - Do NOT ask "aapne call kyun nahi uthaya" — no interrogation about missed calls.
        - Then flow directly into {next_phase_hint}.

        ---
        """
