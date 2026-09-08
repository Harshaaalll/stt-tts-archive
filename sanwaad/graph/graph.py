"""Graph assembly.

Why a state machine instead of one large system prompt: the existing voice
agents in this repo carry a `_TERMINATE_TOOL_DESCRIPTION` with capitalised
FORBIDDEN clauses, which is what prompt engineering looks like when it is
being asked to do a control-flow job. Phase transitions belong in edges, where
they are deterministic, inspectable, and testable without spending a token.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import (
    close_node,
    draft_node,
    escalation_node,
    ground_check_node,
    judge_node,
    pattern_node,
    prioritise_node,
    publish_node,
    retrieve_node,
    review_gate_node,
    triage_node,
    voice_node,
)
from .state import GrievanceState

MAX_REVISIONS = 2


def _after_prioritise(state: GrievanceState) -> str:
    """The only place a case is dropped without a reply.

    By this point three independent readings agree it is not worth drafting
    for: triage found no grievance, the judge found no credible author, and
    the pattern agent found nothing else like it. Any one of them alone would
    be a bad reason to stay silent.
    """
    return "retrieve" if (state.get("priority") or {}).get("drafting", True) else "close"


def _after_ground_check(state: GrievanceState) -> str:
    grounding = state.get("grounding") or {}
    if grounding.get("grounded"):
        return "review_gate"
    if state.get("revision_count", 0) >= MAX_REVISIONS:
        # Two failed attempts to ground a claim means the policy index does not
        # answer this complaint. That is a knowledge gap, not a drafting bug,
        # and a human should see it rather than a third model call burning
        # tokens on the same missing clause.
        return "review_gate"
    return "draft"


def _after_review(state: GrievanceState) -> str:
    return "publish" if (state.get("review") or {}).get("decision") != "reject" else "close"


def _after_escalation(state: GrievanceState) -> str:
    return "voice" if (state.get("escalation") or {}).get("needed") else "close"


def build_graph(checkpointer=None):
    g = StateGraph(GrievanceState)

    g.add_node("triage", triage_node)
    g.add_node("pattern", pattern_node)
    g.add_node("judge", judge_node)
    g.add_node("prioritise", prioritise_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("draft", draft_node)
    g.add_node("ground_check", ground_check_node)
    g.add_node("review_gate", review_gate_node)
    g.add_node("publish", publish_node)
    g.add_node("escalation", escalation_node)
    g.add_node("voice", voice_node)
    g.add_node("close", close_node)

    g.add_edge(START, "triage")
    # Everything reaches the pattern agent, including praise and off-topic
    # chatter. Triage used to close those immediately to save a call; it no
    # longer may, because "nine people said something odd about the new
    # checkout" is a signal even when not one of them filed a complaint, and
    # the only node that can see it is the one holding the window. The saving
    # was never real either: fingerprinting is an embedding, which is local
    # and free, and the case still closes without a drafting call.
    g.add_edge("triage", "pattern")
    # pattern -> judge -> prioritise is a real dependency chain, not a
    # preference: the judge's coordination signal ("how many other accounts
    # posted this exact sentence") is a fact about the window, and only the
    # pattern agent holds it.
    g.add_edge("pattern", "judge")
    g.add_edge("judge", "prioritise")
    g.add_conditional_edges("prioritise", _after_prioritise,
                            {"retrieve": "retrieve", "close": "close"})
    g.add_edge("retrieve", "draft")
    g.add_edge("draft", "ground_check")
    g.add_conditional_edges("ground_check", _after_ground_check,
                            {"draft": "draft", "review_gate": "review_gate"})
    g.add_conditional_edges("review_gate", _after_review,
                            {"publish": "publish", "close": "close"})
    g.add_edge("publish", "escalation")
    g.add_conditional_edges("escalation", _after_escalation, {"voice": "voice", "close": "close"})
    g.add_edge("voice", "close")
    g.add_edge("close", END)

    return g.compile(checkpointer=checkpointer)
