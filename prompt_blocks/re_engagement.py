"""Shared re-engagement logic for follow-up calls.

When a valid narrative exists, the agent must continue from the customer's
already-recorded story (hardship, callbacks, commitments) instead of re-asking
it. The branch is decided in code so a follow-up call can never be scripted as
a first conversation. Used by the fusion EMI and Explore contextual prompts;
seed_fincap has its own Phase 2 variant of the same pattern.
"""


def build_re_engagement_block(has_valid_history, default_language,
                              flow_goal_hint="today's payment conversation",
                              hardship_recency=None):
    """Return the RE-ENGAGEMENT section, or "" on a genuine first-touch call.

    hardship_recency (from hardship_status.get_hardship_recency): 'immediate' → this call
    is the one that should acknowledge the disclosed difficulty. 'stale' → it was already
    acknowledged on an earlier follow-up — do NOT reference it again. None → default wording.
    """
    if not has_valid_history:
        return ""

    if hardship_recency == "stale":
        difficulty_instruction = """- The customer's difficulty was already acknowledged in an earlier call — that
          acknowledgment happens ONLY ONCE, on the call right after disclosure. Do NOT bring
          up the business loss / medical emergency / job loss / personal issue / any prior
          difficulty again in this call, even briefly. Repeating it sounds like you forgot
          you already discussed it. Go straight to checking their update since the last call."""
    else:
        difficulty_instruction = """- Reference the difficulty already recorded in the narrative naturally (e.g. if they
          mentioned business loss: "pichhli baar aapne business mein nuksaan ke baare mein
          bataya tha") and ask how their situation is NOW. This is the ONLY call where you
          acknowledge it — never repeat this acknowledgment in any later call."""

    return f"""### RE-ENGAGEMENT — FOLLOW-UP CALL (HISTORY EXISTS)

        This is NOT a first conversation. The <internal_narrative> above records what the
        customer already told us. Continuing from that context is MANDATORY — re-asking the
        customer's story is a failure.

        After identity confirmation (and the missed-call acknowledgement, if present):
        - If the narrative shows the customer asked to be called back → acknowledge that you
          are calling back as they asked, before anything else.
        {difficulty_instruction}
        - Do NOT re-ask "why has payment not come through" or any fresh version of that
          question — you already know why. Ask only for the UPDATE since the last conversation.
        - Do NOT invent or assume any detail that is not in the narrative.

        Convey in {default_language} (generate naturally — do not recite).

        **PAUSE. Wait for the customer to respond.**

        Their update sets the direction. Then continue toward {flow_goal_hint}.
        This section OVERRIDES any fresh-call framing in the phases below.

        ---
        """
