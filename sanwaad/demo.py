"""End-to-end walkthrough on the mock feed.

    python -m sanwaad.demo

Runs without any API key. With GOOGLE_API_KEY set it uses real Gemini for
triage, drafting and grounding, and the printed cost stops being zero.
"""

from __future__ import annotations

import asyncio
import sys

from .connectors import get_connector
from .llm import is_offline
from .pipeline import resume_case, run_case

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m",
)


def rule(title: str = "") -> None:
    print(f"\n{DIM}{'─' * 78}{RESET}")
    if title:
        print(f"{BOLD}{title}{RESET}")


async def main() -> int:
    if is_offline():
        print(f"{YELLOW}No GOOGLE_API_KEY — running the deterministic offline stub. "
              f"The graph, retrieval, gating and receipts are all real; only the "
              f"model calls are faked.{RESET}")

    complaints = await get_connector("mock").fetch()

    rule("1. INBOUND")
    for c in complaints[:3]:
        print(f"  {c.author}: {c.text[:88]}…")

    target = complaints[1]  # the Devanagari frozen-wallet case
    rule(f"2. CASE — {target.author}")
    print(f"  {target.text}\n")

    out = await run_case(target)
    case_id, state = out["case_id"], out["state"]
    triage = state["triage"]

    print(f"  triage    → {triage['category']}, severity {triage['severity']}, "
          f"reply in {triage['language']}")
    print(f"  retrieved → {[c['clause_id'] for c in state['citations'][:5]]}")
    print(f"\n  draft:\n    {state['draft']['text']}")
    print(f"  cites: {state['draft']['citations']}")

    rule("3. REVIEW GATE")
    if out["pending"]:
        print(f"  {RED}HELD{RESET} — {out['pending']['reason']}")
        print(f"  {DIM}A human approves in the console; the graph stays parked in "
              f"SQLite until they do.{RESET}")
        out = await resume_case(case_id, {
            "decision": "approve", "reviewer": "demo", "note": "approved in demo",
        })
        state = out["state"]
    else:
        print(f"  {GREEN}AUTO-APPROVED{RESET} — {state['review']['note']}")

    rule("4. ESCALATION")
    esc = state.get("escalation") or {}
    if esc.get("needed"):
        for r in esc["reasons"]:
            print(f"  → {r}")
        print(f"  {DIM}Browser callback at /call/{case_id}. Simulating the call…{RESET}")
        out = await resume_case(case_id, {
            "happened": True, "channel": "webrtc", "duration_s": 186.0, "resolved": True,
            "summary": "Explained the freeze reason and the re-KYC path.",
            "citations_used": ["KYC-01", "KYC-02", "BV-03"],
        })
        state = out["state"]
    else:
        print("  none needed")

    rule("5. CLOSURE")
    closure = state["closure"]
    con = closure["consistency"]
    print(f"  resolved       {closure['resolved']}")
    print(f"  cost           ₹{closure['total_cost_inr']:.4f}  "
          f"({closure['llm_calls']} model calls)")
    print(f"  human baseline ₹18–85 for the same work")
    print(f"\n  consistency    "
          f"{GREEN + 'channels agree' + RESET if con['consistent'] else RED + 'DIVERGED' + RESET}")
    print(f"    shared     {con['shared_clauses']}")
    print(f"    text only  {con['text_only']}")
    print(f"    voice only {con['voice_only']}")

    rule("TIMELINE")
    for e in state["events"]:
        print(f"  {e['stage']:<13} {e['message']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
