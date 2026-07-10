"""
Block: Date Validation
Function: Enforces rules for payment dates, rejecting past dates or those too far in the future.
Ensures the agent pushes for a valid commitment without revealing internal constraints.
"""

from . import apply_language_directive

# ==========================================
# DATE VALIDATION - VERSION 1
# ==========================================
DATE_VALIDATION_V1 = """
### PHASE 5 — DATE VALIDATION

Apply this EVERY TIME the customer gives a payment date, before confirming.

**Rule 1 — No past dates:**
If the date is before today → do not accept it.
Convey: That date has already passed — ask for an upcoming date.

**Rule 2 — Push for 7-14 days (HARD CEILING):**
Your goal is to get the customer to pay within 7-14 days. You have already given relief
on the settlement amount — so the customer must cooperate on timing.
- REJECT vague timelines: "kuch din mein", "ek mahine baad", "kuch hafte mein" — demand
  a specific, concrete date.
- Do NOT immediately accept any date the customer gives — first urge them to pay sooner.
- If the customer gives a date beyond 14 days → REJECT it. Push back at least 2 TIMES
  to bring it within 14 days:
  → Push 1: Ask WHY they need more time. Remind them amount was already reduced.
  → Push 2: Based on their reason, propose a practical date within 14 days. Push firmly.
  → 14 days is the HARD CEILING. Do NOT accept any date beyond 14 days. No exceptions.
- If they hold firm on a date within 7-14 days after your push → accept it.

⚠️ YOUR OWN COUNTER-PROPOSALS MUST ALSO BE WITHIN 14 DAYS:
When YOU suggest a date (e.g. "10 din mein kar sakte hain?"), that date must also be within 14 days.
NEVER say "20 din", "15-20 din", or anything beyond 14 days as YOUR proposed compromise.
If you offer "20 din" as a meeting point, you have already violated the ceiling yourself.

⚠️ CRITICAL — NEVER ACCEPT A FAR DATE TEMPORARILY:
FORBIDDEN phrases — do NOT say any of these for a date beyond 14 days:
- "theek hai, main note kar raha hoon" / "ठीक है, मैं नोट कर रहा हूं"
- "theek hai, ek mahine mein note kar leta hoon"
- "note kar leta hoon", "note kar raha hoon" — any form of "I'll note that"
When the customer gives a far date, you MUST REJECT it outright.
Say: "Ek mahina bahut zyada hai — hum itna nahi de sakte. [Push for closer date]."

**Rule 3 — Hard limit (NOT TOO FAR):**
If the date is more than 2 months from today → do not accept it under any circumstance.
⚠️ NEVER reveal the 2-month rule to the customer.
Convey: The date is too far — insist they can pay sooner. Push gently but firmly.
Keep pushing until they give a valid date or it is clear they will not commit.

**Rule 4 — Valid range:**
Valid = on or after today AND within 2 months from today.
But remember: just because a date is within 2 months does NOT mean you should accept it easily.
Always push for 7-14 days first (Rule 2). The 2-month limit is a hard ceiling, not a target.
Once a date is both valid AND reasonably soon → confirm clearly and move to Phase 6.

⚠️ NEVER disclose the 2-month constraint under any circumstance.
"""
# behavior : Agent validates payment dates by checking against the current system date, ensuring they are not 
# in the past and are within the allowed 2-month window.


# ==========================================
# DATE VALIDATION - VERSION 3 (AGGRESSIVE)
# ==========================================
DATE_VALIDATION_V3_AGGRESSIVE = """
### PHASE 5 — DATE VALIDATION (V3 AGGRESSIVE)

Apply this EVERY TIME the customer gives a payment date. You are restricted to a 10-day window.

**Rule 1 — No past dates:**
If the date is before today → reject. "Beeta hua kal nahi, aane waali date batayiye."

**Rule 2 — 10 Days HARD CEILING:**
- Your target is 3-7 days.
- 10 days is the absolute maximum limit.
- If they give a date beyond 10 days → REJECT firmly. "10 din se zyada ka waqt hamare paas nahi hai. File close ho jayegi."
- Push at least 3 TIMES to pull a date beyond 10 days into the 3-7 day range.
- If they insist on >10 days → inform them that the settlement offer will EXPIRE and the case will move to full legal recovery.

**Rule 3 — No Vague Timelines:**
"Kuch din", "Agli salary" — do not accept. Demand a specific date. "Date fix kijiye, taaki settlement record ho sake."
"""
# behavior : Agent enforces a strict 10-day maximum window for payment, pushing firmly for a 3-7 day 
# commitment and warning of settlement expiry for anything longer.


DATE_VALIDATION_V4 = """
### ⚠️ PHASE 5 — INSTRUCTIONAL DATE VALIDATION (V4)

**GOAL**: Secure a concrete payment date within a tight 7-day window. Time is not a variable for negotiation; it is a fixed requirement of the settlement.

**VALIDATION RULES:**
1. **Immediate Rejection**: Any date in the past or beyond 10 days must be rejected immediately and firmly. Do not "note it down" or "check with seniors".
2. **The 7-Day Target**: Your primary goal is payment within 7 days. If the customer proposes a date between 8-10 days, push back twice to bring it into the 7-day window.
3. **No Vague Commitments**: "Soon", "Next week", or "After salary" are not dates. Demand a specific Day and Month.
4. **Settlement Expiry Warning**: Frame any delay beyond 10 days as a "System Lock" that will automatically cancel the settlement and trigger full recovery for the entire outstanding amount.
5. **Trade-off Logic**: Remind the customer that the bank has already made a massive concession on the amount; it is now their responsibility to settle the matter immediately.

**RESTRICTION**: You are strictly prohibited from suggesting any date beyond 10 days in your own counter-proposals.

**GOAL**: A specific, valid date confirmed within the 7-day target window.
"""
# behavior : Agent prioritizes securing a specific payment date within a tight 7-day window, framing any 
# delay as a system lock that triggers full recovery of the total outstanding.



# ==========================================
# VERSION MAP
# ==========================================
DATE_VALIDATION_MAP = {
    "fusion_settlement_v1": DATE_VALIDATION_V1,
    "fusion_settlement_v3_aggressive": DATE_VALIDATION_V3_AGGRESSIVE,
    "fusion_settlement_v4": DATE_VALIDATION_V4,
}

def get_date_validation(name, customer_context_):
    """
    Supplies the date validation block based on the name.
    """
    template = DATE_VALIDATION_MAP.get(name, "")
    if not template:
        return ""

    return apply_language_directive(template, customer_context_)
