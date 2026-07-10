"""
Block: Payment Guidance
Function: Defines Phase 4 of the EMI collection call — guiding the customer on how to make
the EMI payment after a PTP has been collected. Two methods: QR code on passbook back and
PhonePe app.

Reusability: Specific to EMI collection calls — not applicable to settlement or explore calls.
"""

from . import apply_language_directive

# ==========================================
# PAYMENT GUIDANCE - VERSION EMI_V1
# ==========================================
PAYMENT_GUIDANCE_EMI_V1 = """
### PHASE 4 — PAYMENT METHOD GUIDANCE

**Goal:** After PTP is confirmed, tell the customer exactly how to pay. Keep it simple and clear.

"[CALLER_NAME] ji, payment karna bahut aasaan hai. Main aapko ek WhatsApp link bhej raha/rahi hoon, aap wahan se apni EMI payment start kar sakte hain. Iske alawa, do aur tarike hain:"

---

**METHOD 1 — QR CODE ON PASSBOOK BACK:**

"Aapki passbook ke peeche ek QR code hai.
Kisi bhi UPI app se scan karein aur payment karein."

---

**METHOD 2 — PHONPE APP:**

"Ya phir PhonePe app se bhi payment ho jayegi."

**If customer asks for more detail on PhonePe:**
"PhonePe kholein, scan & pay use karein, passbook ke peeche ka QR code scan karein,
amount daalein aur pay karein."

---

**CUSTOMER DOES NOT HAVE PHONPE OR ANY UPI APP:**

"Koi baat nahi — kisi ghar wale ka phone use karein jismein koi bhi UPI app ho,
passbook ke peeche ka QR code scan karein aur payment karein."

---

**CUSTOMER ASKS ABOUT OTHER METHODS (branch, cash, etc.):**

"Sabse aasaan tarika WhatsApp link, QR code ya PhonePe hai — ghar baithe ho jayega.
Agar koi dikkat aaye toh hume call kar sakte hain."

---

⚠️ STRICT PROHIBITIONS — NEVER DO THESE:
- Do NOT share any UPI ID yourself
- Do NOT share any bank account number
- Do NOT accept payment on the call
- Do NOT promise any discount, waiver, or reduced EMI amount
- Do NOT mention any payment method other than WhatsApp link, QR code (passbook), or PhonePe
"""
# behavior : Agent guides customer to pay via QR code on passbook back or PhonePe app —
# strictly avoiding UPI IDs, payment links, bank details, or any other payment method.


# ==========================================
# VERSION MAP
# ==========================================
PAYMENT_GUIDANCE_MAP = {
    "fusion_emi_v1": PAYMENT_GUIDANCE_EMI_V1,
}


def get_payment_guidance(name, customer_context_=None):
    """
    Supplies the payment guidance block based on the version name.
    Payment guidance is fully static — no customer context substitutions needed.
    """
    template = PAYMENT_GUIDANCE_MAP.get(name, "")
    return apply_language_directive(template, customer_context_)
