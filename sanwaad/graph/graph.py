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
    publish_node,
    retrieve_node,
    review_gate_node,
    triage_node,
    voice_node,
)
from .state import GrievanceState

MAX_REVISIONS = 2


def _after_triage(state: GrievanceState) -> str:
    """Route past triage, with a floor that classification cannot undercut.

    `is_complaint` alone is not safe to route on. A comment threatening the
    RBI Ombudsman can be scored severity 5 and still land in an odd category,
    and dropping it because the category looked wrong is precisely the failure
    that ends up screenshotted. Severity is the safety signal; category is
    only a retrieval hint. When they disagree, severity wins.
    """
    triage = state.get("triage") or {}
    if triage.get("severity", 1) >= 4:
        return "retrieve"
    if not triage.get("is_complaint"):
        return "close"
    return "retrieve"


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
    g.add_node("retrieve", retrieve_node)
    g.add_node("draft", draft_node)
    g.add_node("ground_check", ground_check_node)
    g.add_node("review_gate", review_gate_node)
    g.add_node("publish", publish_node)
    g.add_node("escalation", escalation_node)
    g.add_node("voice", voice_node)
    g.add_node("close", close_node)

    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", _after_triage, {"retrieve": "retrieve", "close": "close"})
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
