"""Graph nodes. Each is a pure-ish async function: state in, state delta out."""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from ..caching import TRIAGE_CACHE
from ..config import REVIEW, TIER_DRAFT, TIER_REASONING, TIER_TRIAGE
from ..guardrails import check_complaint, check_reply
from ..llm import format_citations, structured
from ..obs import TRACER
from ..router import route
from ..models import (
    Category,
    Citation,
    Draft,
    GroundingVerdict,
    Review,
    Triage,
    VoiceOutcome,
)
from ..rag.store import CATEGORY_CLAUSES, get_store
from .state import GrievanceState, event

# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------

_TRIAGE_SYSTEM = """You triage inbound public comments for NimbusPay, an Indian UPI wallet.

Comments arrive in English, Hindi (Devanagari), Hinglish written in Latin script,
and other Indian languages. Read them as an Indian support lead would.

Set `severity` on customer impact, not on tone:
  1  mild annoyance, no money at stake
  2  routine issue, small amount, no deadline pressure
  3  real money blocked or lost, customer inconvenienced
  4  large amount, repeated contact, or account fully inaccessible
  5  regulatory, legal, fraud, or safety exposure

`summary` MUST be written in English regardless of the comment's language, in
one sentence, naming the amount and timeframe if present. This summary is used
verbatim as a retrieval query against an English policy index, so write it with
the vocabulary a policy document would use.

`language` is the BCP-47 tag of the language to REPLY in, mirroring the
customer. Hinglish in Latin script is "hi-Latn"."""

# Clause ESC-05: these never get auto-answered, whatever the model computes.
_REGULATORY_PATTERNS = re.compile(
    r"\b(rbi|ombudsman|consumer\s*court|legal\s*notice|lawyer|advocate|police|"
    r"fir|cyber\s*cell|media|journalist|defraud|fraud|scam|stolen)\b",
    re.IGNORECASE,
)


async def triage_node(state: GrievanceState) -> dict:
    complaint = state["complaint"]
    text = complaint["text"]

    with TRACER.span("triage", trace_id=state["case_id"]) as span:
        # INPUT guardrail. The complaint is untrusted text that is about to be
        # placed inside a prompt; anything instruction-shaped is flagged now so
        # the downstream router and the auto-post gate can both see it.
        text, input_violations = check_complaint(text)
        injection = [v.detail for v in input_violations if v.rule == "injection"]

        r = route("triage")
        span.set(model=r.model, injection=bool(injection))

        cached = TRIAGE_CACHE.get(r.model, text, namespace="triage")
        if cached is not None:
            span.set(cache="hit")
            result = Triage(**cached)
            cost = {"stage": "triage", "model": "cache", "usd": 0.0, "inr": 0.0,
                    "prompt_tokens": 0, "output_tokens": 0}
        else:
            span.set(cache="miss")
            fallback = _offline_triage(text)
            result, cost = await structured(
                model=r.model,
                system=_TRIAGE_SYSTEM,
                user=f"Comment by {complaint['author']} on {complaint['channel']}:\n\n{text}",
                schema=Triage,
                stage="triage",
                offline_fallback=fallback,
            )
            TRIAGE_CACHE.put(r.model, text, result.model_dump(mode="json"), namespace="triage")
        span.set(cost_inr=cost.get("inr", 0.0))

    # Deterministic override. A keyword match here outranks the model, because
    # a missed regulatory mention is a compliance incident and a false positive
    # is merely a human reading one extra ticket.
    regulatory = bool(_REGULATORY_PATTERNS.search(text))
    if regulatory and result.severity < 5:
        result.severity = 5

    return {
        "triage": result.model_dump(mode="json"),
        "retrieval_query": result.summary,
        "injection_flagged": bool(injection),
        "costs": [cost],
        "events": [event(
            "triage",
            f"{result.category.value} / severity {result.severity} / {result.sentiment}",
            regulatory_flag=regulatory, injection=bool(injection),
        )],
    }


def _offline_triage(text: str) -> dict:
    """Keyword triage for keyless runs. Crude on purpose — it exists so the
    pipeline is demonstrable, not so it is good."""
    low = text.lower()
    # Keywords are listed in every script the mock feed actually contains.
    # An English-only list silently mis-routes Devanagari complaints to
    # off_topic, which looks like the pipeline is broken when it is only the
    # stub that is.
    pairs = [
        (("double debit", "twice", "duplicate", "do baar", "दो बार",
          "refund", "wapas", "reversal", "paise nahi", "return", "रिफंड",
          "वापस", "पैसे नहीं", "पैसा नहीं"), Category.REFUND),
        (("charge", "charged", "deduct", "kata", "fee", "extra", "चार्ज",
          "कट गए", "कटे", "शुल्क"), Category.BILLING),
        (("freeze", "frozen", "block", "kyc", "login", "locked", "फ्रीज",
          "ब्लॉक", "बंद", "लॉक"), Category.ACCOUNT_ACCESS),
        (("down", "not working", "outage", "server", "डाउन", "चल नहीं"),
         Category.SERVICE_OUTAGE),
        (("rude", "behaviour", "misbehav", "बदतमीज़", "बदतमीज"),
         Category.AGENT_BEHAVIOUR),
        (("privacy", "otp", "phishing", "ओटीपी", "धोखा"), Category.DATA_PRIVACY),
        (("thank", "well done", "love", "clean", "शुक्रिया", "धन्यवाद"),
         Category.PRAISE),
    ]
    category = Category.OFF_TOPIC
    for keys, cat in pairs:
        if any(k in low for k in keys):
            category = cat
            break

    if _REGULATORY_PATTERNS.search(text):
        severity = 5
    elif category in (Category.OFF_TOPIC, Category.PRAISE):
        severity = 1
    else:
        # Money at stake drives severity, matching the real triage rubric.
        amount = _largest_rupee_amount(text) or 0
        severity = 4 if amount > 10_000 else 3

    devanagari = bool(re.search(r"[ऀ-ॿ]", text))
    return {
        "is_complaint": category not in (Category.OFF_TOPIC, Category.PRAISE),
        "category": category.value,
        "severity": severity,
        "sentiment": "positive" if category is Category.PRAISE else "frustrated",
        "language": "hi-IN" if devanagari else "en-IN",
        "summary": f"Customer reports an issue in the {category.value} category: {text[:160]}",
        "entities": {},
        "needs_private_data": category is not Category.PRAISE,
    }


# ---------------------------------------------------------------------------
# Retrieve
# ---------------------------------------------------------------------------

async def retrieve_node(state: GrievanceState) -> dict:
    triage = state["triage"]
    store = get_store()

    query = state.get("retrieval_query") or state["complaint"]["text"]
    boost = CATEGORY_CLAUSES.get(triage["category"], ())
    span_cm = TRACER.span("retrieve", trace_id=state["case_id"], strategy="rrf")
    # RRF over dense + BM25. Measured at strict@5 1.00 on the golden set vs
    # 0.89 for the old score blend — see `python -m sanwaad.evals.retrieval`.
    with span_cm as span:
        retrieved = store.search_fused(query, k=5, boost_prefixes=boost)
        span.set(hits=[c.clause_id for c in retrieved])

    # Brand-voice and privacy clauses are constitutional, not retrieved.
    constitutional = store.always_on(("BV-", "PRV-"))

    seen, merged = set(), []
    for c in retrieved + constitutional:
        if c.clause_id not in seen:
            seen.add(c.clause_id)
            merged.append(c)

    return {
        "citations": [c.model_dump() for c in merged],
        "events": [event(
            "retrieve",
            f"{len(retrieved)} retrieved + {len(constitutional)} constitutional",
            retrieved=[c.clause_id for c in retrieved],
        )],
    }


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------

_DRAFT_SYSTEM = """You write public replies for NimbusPay on social media.

You are given the customer's comment and the exact policy clauses that govern
it. Follow these rules absolutely:

- Every factual claim, timeline, amount, or entitlement you state MUST come
  from a supplied clause. If the clauses do not answer something, say what you
  will check instead of guessing. Inventing a refund timeline is the single
  worst thing you can do here.
- List in `citations` the id of every clause you actually relied on. Do not
  list clauses you did not use.
- Reply in the language given by `reply_language`, mirroring the customer.
  For "hi-Latn", write Hinglish in Latin script the way Indians actually type.
- Under 60 words. This is a public thread, not an email.
- Never repeat back any phone number, account number, transaction id or UTR.
- End with exactly one concrete next step.
- Set `promises_compensation` true if your text commits to any money moving:
  a refund, a credit, a waiver, or a compensation amount."""


async def draft_node(state: GrievanceState) -> dict:
    triage = state["triage"]
    citations = [Citation(**c) for c in state["citations"]]
    revision = state.get("revision_count", 0)

    correction = ""
    grounding = state.get("grounding")
    if grounding and grounding.get("unsupported_claims"):
        correction = (
            "\n\nYour previous draft was REJECTED as ungrounded. These claims are "
            "not supported by any clause and must be removed or replaced with a "
            "commitment to check:\n- "
            + "\n- ".join(grounding["unsupported_claims"])
        )

    user = f"""Customer comment ({triage['sentiment']}, severity {triage['severity']}):
{state['complaint']['text']}

What they are reporting: {triage['summary']}
reply_language: {triage['language']}

Governing clauses:
{format_citations(citations)}{correction}"""

    r = route("draft", severity=triage["severity"], revision=revision,
              injection_flagged=bool(state.get("injection_flagged")),
              language=triage["language"])

    result, cost = await structured(
        model=r.model,
        system=_DRAFT_SYSTEM,
        user=user,
        schema=Draft,
        temperature=0.3,
        stage=f"draft{'_revision' if revision else ''}",
        offline_fallback={
            "text": (
                "Sorry about this — that is a genuinely frustrating position to be in. "
                "I'm pulling up the details on the transaction you mentioned now. "
                "Sending you a DM so we can check it properly without your details "
                "being public. — Adhik, ref " + state["case_id"][-6:]
            ),
            "citations": [c.clause_id for c in citations[:3]],
            "promises_compensation": False,
        },
    )

    # OUTPUT guardrail, before the model-based grounding check. Deterministic,
    # free, and it repairs PII in place rather than discarding a good reply.
    guard = check_reply(result.text)
    result.text = guard.text
    if guard.blocked:
        result.promises_compensation = True  # forces the human gate below

    with TRACER.span("draft", trace_id=state["case_id"]) as span:
        span.set(model=r.model, route_reason=r.reason, revision=revision,
                 cost_inr=cost.get("inr", 0.0), words=len(result.text.split()),
                 guardrail_blocked=guard.blocked,
                 violations=[v.rule for v in guard.violations])

    return {
        "draft": result.model_dump(mode="json"),
        "revision_count": revision + 1,
        "guardrails": [v.__dict__ for v in guard.violations],
        "costs": [cost],
        "events": [event(
            "draft",
            f"{'revision ' + str(revision) if revision else 'first draft'} via {r.model}, "
            f"{len(result.text.split())} words, cites {result.citations}"
            + (f" — GUARDRAIL {[v.rule for v in guard.violations]}" if guard.violations else ""),
        )],
    }


# ---------------------------------------------------------------------------
# Grounding check
# ---------------------------------------------------------------------------

_GROUND_SYSTEM = """You are a compliance checker. You are given a proposed public
reply and the policy clauses available to its author.

For each factual claim in the reply — timelines, amounts, entitlements,
processes, guarantees — decide whether a supplied clause supports it.

List in `unsupported_claims` every claim with no clause behind it. Be strict:
"within 3 working days" is unsupported unless a clause states that window.

Do NOT flag: apologies, empathy, offers to check, offers to DM, or requests
for information. Those commit us to nothing.

`grounded` is true only when `unsupported_claims` is empty."""


async def ground_check_node(state: GrievanceState) -> dict:
    draft = state["draft"]
    citations = [Citation(**c) for c in state["citations"]]

    result, cost = await structured(
        model=TIER_DRAFT,
        system=_GROUND_SYSTEM,
        user=f"Proposed reply:\n{draft['text']}\n\nAvailable clauses:\n{format_citations(citations)}",
        schema=GroundingVerdict,
        temperature=0.0,
        stage="ground_check",
        offline_fallback={"grounded": True, "unsupported_claims": [],
                          "reasoning": "offline mode: not verified"},
    )

    return {
        "grounding": result.model_dump(mode="json"),
        "costs": [cost],
        "events": [event(
            "ground_check",
            "grounded" if result.grounded
            else f"{len(result.unsupported_claims)} unsupported claim(s)",
            unsupported=result.unsupported_claims,
        )],
    }


# ---------------------------------------------------------------------------
# Review gate
# ---------------------------------------------------------------------------

def auto_post_allowed(state: GrievanceState) -> tuple[bool, str]:
    """Decide whether this reply may go out without a human.

    Returns (allowed, reason). The reason is logged either way, because
    "why did this need a human" is the question a pilot customer asks most.
    """
    triage = state["triage"]
    draft = state["draft"]
    grounding = state.get("grounding") or {}

    if triage["severity"] > REVIEW.auto_post_max_severity:
        return False, f"severity {triage['severity']} above auto-post ceiling {REVIEW.auto_post_max_severity}"
    if REVIEW.require_grounded and not grounding.get("grounded"):
        return False, "draft contains unsupported claims"
    if REVIEW.forbid_auto_compensation and draft.get("promises_compensation"):
        return False, "draft commits money; clause RFD-05 requires approval"
    if triage.get("needs_private_data"):
        return False, "resolution needs account data not available publicly"
    if state.get("injection_flagged"):
        return False, "complaint contains instruction-like text; never auto-post"
    blocking = [g for g in (state.get("guardrails") or []) if g.get("severity") == "block"]
    if blocking:
        return False, f"guardrail block: {[g['rule'] for g in blocking]}"
    return True, "low severity, fully grounded, commits nothing"


async def review_gate_node(state: GrievanceState) -> dict:
    allowed, reason = auto_post_allowed(state)
    if allowed:
        review = Review(decision="approve", reviewer="auto", note=reason, auto=True,
                        final_text=state["draft"]["text"])
        return {
            "review": review.model_dump(mode="json"),
            "events": [event("review_gate", f"auto-approved: {reason}")],
        }

    # Human needed. LangGraph's interrupt suspends the run durably; the case
    # sits in the review queue until the API resumes it with a decision, which
    # may be minutes or days later, in a different process.
    from langgraph.types import interrupt

    decision = interrupt({
        "case_id": state["case_id"],
        "reason": reason,
        "complaint": state["complaint"]["text"],
        "draft": state["draft"]["text"],
        "citations": [c["clause_id"] for c in state["citations"]],
        "triage": state["triage"],
        "grounding": state.get("grounding"),
    })

    review = Review(**decision) if isinstance(decision, dict) else Review(decision="reject")
    if review.decision == "approve" and not review.final_text:
        review.final_text = state["draft"]["text"]

    # Capture the correction while it exists. This is the only moment the pair
    # (what the model wrote, what a human actually sends) is available.
    try:
        from ..feedback import FeedbackRecord, record as record_feedback

        record_feedback(FeedbackRecord(
            case_id=state["case_id"], decision=review.decision,
            complaint=state["complaint"]["text"], draft=state["draft"]["text"],
            final=review.final_text or "", category=state["triage"]["category"],
            severity=state["triage"]["severity"], language=state["triage"]["language"],
            citations=state["draft"].get("citations", []),
            reviewer=review.reviewer, note=review.note,
        ))
    except Exception as exc:  # feedback capture must never block a review
        from loguru import logger

        logger.warning(f"feedback capture failed for {state['case_id']}: {exc}")

    return {
        "review": review.model_dump(mode="json"),
        "events": [event("review_gate", f"human {review.decision} ({reason})")],
    }


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

async def publish_node(state: GrievanceState) -> dict:
    from ..connectors import get_connector

    review = state["review"]
    complaint = state["complaint"]

    # ACTION guardrail: last check before an irreversible public post. A human
    # can paste anything into the edit box, so the reviewer's text is checked
    # exactly like the model's.
    final_guard = check_reply(review["final_text"] or "")
    if final_guard.blocked:
        return {
            "published": {"blocked": True,
                          "reasons": [v.detail for v in final_guard.violations]},
            "events": [event("publish", "BLOCKED at the action guardrail: "
                             + "; ".join(v.detail for v in final_guard.violations))],
        }

    connector = get_connector(complaint["channel"])
    receipt = await connector.reply(complaint["external_id"], final_guard.text)

    return {
        "published": receipt,
        "events": [event("publish", f"posted to {complaint['channel']}", **receipt)],
    }


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------

class EscalationDecision(BaseModel):
    needed: bool
    reason: str
    suggested_channel: str = Field(default="webrtc", description="webrtc or exotel")


async def escalation_node(state: GrievanceState) -> dict:
    """Apply clause ESC-02 rather than asking a model to remember it.

    The escalation triggers are written down and enumerable, so they are code.
    A model is not more accurate here, only more expensive and less auditable.
    """
    triage = state["triage"]
    text = state["complaint"]["text"]
    reasons = []

    if triage["severity"] >= REVIEW.escalate_to_voice_min_severity:
        reasons.append(f"severity {triage['severity']} (ESC-02)")
    if triage["category"] in ("account_access", "data_privacy"):
        reasons.append(f"{triage['category']} always escalates (ESC-02)")
    amount = _largest_rupee_amount(text)
    if amount and amount > 2000:
        reasons.append(f"disputed amount ₹{amount:,.0f} exceeds ₹2,000 (ESC-02)")
    if re.search(r"\b(call me|baat kar|phone|speak to|talk to someone)\b", text, re.I):
        reasons.append("customer explicitly asked to speak to someone (ESC-02)")

    needed = bool(reasons)
    return {
        "escalation": {
            "needed": needed,
            "reasons": reasons,
            "channel": "webrtc" if needed else "none",
        },
        "events": [event(
            "escalation",
            "voice offered: " + "; ".join(reasons) if needed else "no voice escalation needed",
        )],
    }


_RUPEE_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)


def _largest_rupee_amount(text: str) -> Optional[float]:
    values = []
    for m in _RUPEE_RE.finditer(text):
        try:
            values.append(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    return max(values) if values else None


# ---------------------------------------------------------------------------
# Voice handoff
# ---------------------------------------------------------------------------

async def voice_node(state: GrievanceState) -> dict:
    """Suspend until a voice call completes.

    The graph does not place the call. It publishes everything the voice agent
    needs — citations included — and waits. Whether the call arrives over
    WebRTC from a browser or over Exotel from a phone is the transport's
    problem, not the state machine's.
    """
    from langgraph.types import interrupt

    outcome = interrupt({
        "await": "voice_call",
        "case_id": state["case_id"],
        "channel": state["escalation"]["channel"],
        "brief": {
            "summary": state["triage"]["summary"],
            "language": state["triage"]["language"],
            "category": state["triage"]["category"],
            "public_reply": state["review"]["final_text"],
            "clause_ids": [c["clause_id"] for c in state["citations"]],
        },
    })

    result = VoiceOutcome(**outcome) if isinstance(outcome, dict) else VoiceOutcome()
    return {
        "voice": result.model_dump(mode="json"),
        "events": [event(
            "voice",
            f"{result.channel} call, {result.duration_s:.0f}s, "
            f"{'resolved' if result.resolved else 'unresolved'}",
            citations_used=result.citations_used,
        )],
    }


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------

async def close_node(state: GrievanceState) -> dict:
    """Roll up cost and emit the consistency receipt."""
    from ..consistency import build_receipt

    costs = state.get("costs", [])
    total_usd = sum(c.get("usd", 0.0) for c in costs)
    total_inr = sum(c.get("inr", 0.0) for c in costs)

    voice = state.get("voice") or {}
    receipt = build_receipt(
        text_clauses=(state.get("draft") or {}).get("citations", []),
        voice_clauses=voice.get("citations_used", []),
    )

    resolved = bool(voice.get("resolved")) or not (state.get("escalation") or {}).get("needed")

    return {
        "closure": {
            "resolved": resolved,
            "total_cost_usd": round(total_usd, 6),
            "total_cost_inr": round(total_inr, 4),
            "llm_calls": len([c for c in costs if c.get("model") not in ("", "offline")]),
            "consistency": receipt.model_dump(),
        },
        "events": [event(
            "close",
            f"closed {'resolved' if resolved else 'unresolved'}, ₹{total_inr:.4f}, "
            f"channels {'consistent' if receipt.consistent else 'DIVERGED'}",
        )],
    }
