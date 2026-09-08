"""Model router: pick the cheapest model that can do this particular job.

The naive setup uses one model everywhere and is either needlessly expensive
on easy work or unreliable on hard work. A router turns "which model" into a
per-request decision driven by signals you already have.

Routing on *observable request features* (severity, language, whether a prior
attempt failed) rather than asking a model to judge difficulty first. A
classifier-based router costs an extra call and an extra failure mode to save
one; it only pays once the spread between tiers is large and the features are
genuinely uninformative. Here they are highly informative — triage has already
computed severity — so the routing is a function, and a function can be unit
tested, which a judgement call cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from .config import TIER_DRAFT, TIER_REASONING, TIER_TRIAGE

Stage = Literal["triage", "draft", "ground_check", "voice"]


@dataclass(frozen=True)
class Route:
    model: str
    reason: str
    max_latency_ms: Optional[int] = None   # None = latency does not matter here


# The voice path is latency-bound and must never be routed to a reasoning
# model, whatever it would save. Everything else is throughput-bound.
VOICE_MODEL = "gemini-2.5-flash"
VOICE_TTFT_BUDGET_MS = 500


def route(stage: Stage, *, severity: int = 1, revision: int = 0,
          injection_flagged: bool = False, language: str = "en-IN") -> Route:
    if stage == "voice":
        return Route(VOICE_MODEL, "voice is latency-bound; no reasoning tier",
                     max_latency_ms=VOICE_TTFT_BUDGET_MS)

    if stage == "triage":
        # Runs on every inbound item, so it is the single biggest lever on
        # spend. Nothing about triage justifies a larger model — it is a
        # short classification with a fixed label set.
        return Route(TIER_TRIAGE, "classification over a fixed label set")

    if stage == "ground_check":
        return Route(TIER_DRAFT, "verification needs the drafting tier's reading ability")

    # draft
    if revision > 0:
        return Route(TIER_REASONING,
                     f"revision {revision}: the cheap tier already failed grounding once")
    if injection_flagged:
        return Route(TIER_REASONING,
                     "adversarial input: use the stronger model rather than the cheap one")
    if severity >= 4:
        return Route(TIER_REASONING, f"severity {severity}: high-stakes reply")
    return Route(TIER_DRAFT, f"severity {severity}: routine reply")


def explain() -> list[dict]:
    """The full routing table, for documentation and for the console."""
    cases = [
        ("triage",       dict()),
        ("draft",        dict(severity=2)),
        ("draft",        dict(severity=4)),
        ("draft",        dict(severity=2, revision=1)),
        ("draft",        dict(severity=1, injection_flagged=True)),
        ("ground_check", dict()),
        ("voice",        dict()),
    ]
    out = []
    for stage, kw in cases:
        r = route(stage, **kw)
        out.append({"stage": stage, "when": kw or {"always": True},
                    "model": r.model, "reason": r.reason})
    return out
