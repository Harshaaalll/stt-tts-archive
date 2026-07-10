def apply_language_directive(template, customer_context_):
    """
    Makes a prompt block language-aware. Used by every settlement-flow block's
    get_* function so the model is reminded about the active call language even
    deep inside individual blocks (otherwise the Hindi examples and tactic
    templates cause the model to drift back into Hindi mid-call).

    Behavior:
      1. Replaces any `{default_language}` placeholder in the block text with
         the active language pulled from customer_context_['default_language'].
      2. When the active language is non-Hindi, prepends a strong LANGUAGE LOCK
         header at the top of the block so the model treats the Hindi content
         below as a structural template, not a script to parrot.
    """
    if not template or not customer_context_:
        return template
    lang_val = str(customer_context_.get("default_language", "Hindi"))
    template = template.replace("{default_language}", lang_val)
    if lang_val.strip().lower() != "hindi":
        header = (
            f"⚠️ **LANGUAGE LOCK FOR THIS BLOCK**: Speak ONLY in **{lang_val}**. "
            f"The Hindi text below is a STRUCTURAL/STYLISTIC template — translate every "
            f"Hindi sentence into {lang_val} before speaking. NEVER echo Hindi verbatim. "
            f"Only English loanwords (loan, EMI, settlement, payment, account, senior manager, "
            f"outstanding amount, CIBIL, PhonePe, etc.) stay English.\n\n"
        )
        template = header + template
    return template
