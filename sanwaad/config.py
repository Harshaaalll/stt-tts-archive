"""Central configuration: model tiers, pricing, paths.

Model tiering is the core cost lever. Triage runs on every inbound comment,
so it uses the cheapest model available; drafting runs only on real
complaints; the expensive tier is reserved for high-severity escalations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = Path(__file__).resolve().parent
POLICY_DIR = PACKAGE_ROOT / "policy"
DATA_DIR = PACKAGE_ROOT / "data"
INDEX_PATH = DATA_DIR / "policy_index.json"
CHECKPOINT_PATH = DATA_DIR / "sanwaad.sqlite"
CASES_PATH = DATA_DIR / "cases.json"


# --- Model tiers -----------------------------------------------------------
# Every inbound item hits TRIAGE. Only genuine complaints reach DRAFT. Only
# severity>=4 reaches REASONING. This ordering is what keeps cost per
# resolution in paise rather than rupees.

TIER_TRIAGE = os.getenv("SANWAAD_MODEL_TRIAGE", "gemini-2.5-flash-lite")
TIER_DRAFT = os.getenv("SANWAAD_MODEL_DRAFT", "gemini-2.5-flash")
# The reasoning tier must be a genuinely stronger model than TIER_DRAFT, or the
# router's escalation branches are decorative. Reached only on a grounding
# failure, an adversarial input, or severity >= 4 — a few percent of traffic.
TIER_REASONING = os.getenv("SANWAAD_MODEL_REASONING", "gemini-2.5-pro")

# Embeddings run locally on ONNX — no per-token cost, no network round trip.
# The multilingual model matters: complaints arrive in Hinglish and Devanagari.
EMBED_MODEL = os.getenv(
    "SANWAAD_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)

# --- Pricing (USD per 1M tokens) -------------------------------------------
# Mirrors the table in the voice agents so text and voice costs roll up into
# one comparable number.

LLM_PRICING_USD = {
    "gemini-2.5-flash": {"input": 0.30, "cache_read": 0.03, "output": 2.50},
    "gemini-2.5-flash-lite": {"input": 0.10, "cache_read": 0.01, "output": 0.40},
    "gemini-2.5-pro": {"input": 1.25, "cache_read": 0.13, "output": 10.00},
    "_default": {"input": 0.30, "cache_read": 0.03, "output": 2.50},
}

# What a human agent costs for the same work, for the comparison the console
# renders. Tune to your market; defaults are Indian BPO ballpark.
HUMAN_COST_INR = {
    "social_reply": 18.0,   # ~3 min of an agent's time at ~₹350/hr
    "voice_call": 85.0,     # ~4 min call + wrap-up
}


def inr_per_usd() -> float:
    try:
        return float(os.getenv("INR_PER_USD", "84.0"))
    except ValueError:
        return 84.0


def llm_cost(model: str, prompt_tokens: int, output_tokens: int) -> dict:
    """Cost of one LLM call in both currencies."""
    rates = LLM_PRICING_USD.get(model, LLM_PRICING_USD["_default"])
    usd = (prompt_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000.0
    return {"model": model, "usd": usd, "inr": usd * inr_per_usd(),
            "prompt_tokens": prompt_tokens, "output_tokens": output_tokens}


@dataclass(frozen=True)
class ReviewPolicy:
    """When may the agent post without a human?

    Deliberately conservative. An LLM posting an unsupervised apology about
    someone's money is the failure mode that ends the pilot, so auto-post is
    gated on low severity AND full grounding AND no compensation promise.
    """

    auto_post_max_severity: int = 2
    require_grounded: bool = True
    forbid_auto_compensation: bool = True
    escalate_to_voice_min_severity: int = 4


REVIEW = ReviewPolicy()
