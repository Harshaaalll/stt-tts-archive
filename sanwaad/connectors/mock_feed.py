"""A seeded feed of realistic complaints.

Written to exercise the paths that matter rather than to look tidy: Devanagari
and Latin-script Hinglish, a regulatory mention that must bypass auto-reply, a
sub-₹100 grumble that should sail through unattended, a fraud report, and a
compliment that must cost one triage call and stop.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..config import DATA_DIR
from ..models import Channel, Complaint
from .base import Connector

_SEED = [
    ("u/rohit_mhrs",
     "NimbusPay se ₹4,500 transfer kiya tha 6 din pehle, paise beneficiary ko "
     "nahi mile aur mere account me bhi wapas nahi aaye. Support pe 3 baar likha, "
     "koi jawab nahi. Ye paisa mera hai ya aapka?"),
    ("u/anita_k92",
     "मेरा वॉलेट अचानक फ्रीज कर दिया गया, ₹18,000 अंदर पड़े हैं और ऐप कुछ बताता ही नहीं। "
     "कल मेरी बेटी की फीस भरनी है। कोई इंसान जवाब दे सकता है क्या?"),
    ("u/deep_ranjan",
     "Charged ₹15 on a ₹2,000 wallet-to-bank transfer even though I'm well under "
     "the 10k free limit this month. Small amount but it's the principle. What is "
     "this charge for?"),
    ("u/sameer.p",
     "Been 40 days since my dispute. No resolution, no callback despite two "
     "promises. Filing with the RBI Ombudsman this week and posting the whole "
     "chat log. Absolutely done with this company."),
    ("u/priyaaa_23",
     "Someone called claiming to be from NimbusPay and asked for the OTP to "
     "'unblock' my account. I didn't share it but they knew my last transaction "
     "amount. How did they get that?!"),
    ("u/karthik_rn",
     "Double debit for one Swiggy order — ₹640 taken twice. Order id is in my "
     "app. Please fix, this happened last month too."),
    ("u/meera.writes",
     "Honestly the new UPI autopay flow is so much cleaner than before. "
     "Whoever redesigned it, well done."),
    ("u/tanvi_s",
     "App down again during peak hours?? Third time this month. Can't pay at the "
     "counter, standing here like an idiot."),
]


class MockFeedConnector(Connector):
    name = "mock"

    def __init__(self):
        self._replies_path = DATA_DIR / "mock_replies.json"

    async def fetch(self, limit: int = 20) -> list[Complaint]:
        now = datetime.now(timezone.utc)
        return [
            Complaint(
                external_id=f"mock_{i:03d}",
                channel=Channel.MOCK,
                author=author,
                text=text,
                url=f"https://example.invalid/thread/mock_{i:03d}",
                created_at=(now - timedelta(minutes=17 * (len(_SEED) - i))).isoformat(),
            )
            for i, (author, text) in enumerate(_SEED[:limit])
        ]

    async def reply(self, external_id: str, text: str) -> dict:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        existing = []
        if self._replies_path.exists():
            existing = json.loads(self._replies_path.read_text(encoding="utf-8"))
        receipt = {
            "reply_id": f"reply_{external_id}",
            "url": f"https://example.invalid/thread/{external_id}#reply",
            "posted_at": datetime.now(timezone.utc).isoformat(),
            "dry_run": True,
        }
        existing.append({**receipt, "in_reply_to": external_id, "text": text})
        self._replies_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        return receipt
