# संवाद Sanwaad — closed-loop grievance resolution

A public complaint and a support call are two channels of the same grievance.
Sanwaad runs both through **one LangGraph state machine** grounded in **one
policy index**, so the written reply and the spoken answer provably cite the
same clauses — and it emits a receipt proving it.

```
Reddit / mock feed
        │
        ▼
   ┌─ triage ──────────────┐  cheap model, every inbound item
   │   severity floor      │  a severity-5 item is never dropped, whatever its category
   ▼                       │
 retrieve  ◄─── policy index (33 clauses, local ONNX embeddings, ₹0)
        │
        ▼
    draft ──► ground_check ──┐  every claim must trace to a clause
        ▲                    │  ungrounded → revise (max 2) → human
        └────────────────────┘
        │
        ▼
  review_gate ── interrupt() ──► human approves in the console
        │                        (case parked in SQLite, survives restart)
        ▼
    publish ──► escalation ──► voice (WebRTC, same clauses) ──► close
                                                                  │
                                          consistency receipt ◄────┘
                                          cost per resolution
```

## Run it

No API key needed — the graph, retrieval, gating and receipts are all real;
only the model calls are stubbed.

```bash
source .venv/bin/activate
python -m sanwaad.demo          # CLI walkthrough
python -m sanwaad.api.server    # console at http://localhost:7870
pytest tests/ -q                # 22 tests, no key required
```

First run downloads a ~470MB ONNX embedding model. Every later start is instant.

Add `GOOGLE_API_KEY` to `.env` for real triage/drafting/grounding. Add
`SARVAM_API_KEY` + `MURF_API_KEY` to place actual voice calls.

## The three ideas worth stealing

**1. The consistency receipt.** Support organisations' real failure is not a
wrong answer, it is *two* answers — social says seven days, the call centre
says three. Because both channels retrieve from one clause index, we record
which clauses each relied on and compare. `CONTRADICTORY_PAIRS` in
`consistency.py` encodes the clause pairs that cannot both hold; a case that
touches both across channels is flagged mechanically instead of being
discovered in a screenshot. A call going *deeper* than a tweet is not a
divergence, and the tests pin that distinction.

**2. Retrieval runs on triage's English summary, not the raw comment.**
Measured, not assumed: `"paise wapas nahi aaye 5 din ho gaye"` retrieves the
brand-voice clauses and misses the refund policy entirely, because
Latin-script Hinglish lands nowhere near English policy text in this embedding
space. The same complaint as an English one-liner retrieves RFD-01/02/03
correctly. Triage already produces that summary, so the fix costs nothing —
and the triage category additionally boosts its own clause family, which is
cheaper to encode as a prior than to make an embedding model learn.

**3. Phases belong in edges, not in prompts.** The collections agents in this
repo carry a `_TERMINATE_TOOL_DESCRIPTION` with capitalised FORBIDDEN clauses —
what prompt engineering looks like when asked to do a control-flow job. Here
the graph owns the phase, so transitions are deterministic, inspectable and
testable without spending a token.

## Cost shape

| Stage | Model | Runs on |
|---|---|---|
| triage | `gemini-2.5-flash-lite` | every inbound item |
| draft + grounding | `gemini-2.5-flash` | genuine complaints only |
| retrieval | local ONNX | free, every turn, both channels |
| voice | Sarvam STT + Gemini + Murf | escalations only |

Praise and off-topic comments cost exactly one flash-lite call and stop. The
voice leg runs over browser WebRTC, which carries no per-minute telephony
charge at all — Exotel stays wired up for customers who want a real phone call.

`closure.total_cost_inr` is the per-case rollup across both channels, in the
same INR/USD shape as the existing agents' CSV export, so text and voice land
in one comparable number rather than two dashboards.

## Safety posture

Auto-posting is deliberately hard to earn: severity ≤ 2, fully grounded, no
money promised, no private data needed. Everything else waits for a human.
An LLM posting an unsupervised apology about someone's money is the failure
mode that ends a pilot. `SANWAAD_ALLOW_POSTING` gates writes to real
platforms and defaults to off.

## Layout

```
sanwaad/
  config.py         model tiers, pricing, the auto-post policy
  models.py         domain types
  llm.py            structured calls + per-call cost, with an offline mode
  consistency.py    the receipt, and the contradiction table
  graph/            state, nodes, edges
  rag/              clause parsing, local ONNX embeddings, hybrid retrieval
  connectors/       mock feed, Reddit
  voice/            brief builder (the bridge), WebRTC agent
  api/              FastAPI + review console + call page
  policy/           the knowledge base — plain markdown, `## [ID] Heading`
```

Adding a channel means writing one `Connector`; the state machine does not change.
Adding a policy means dropping a markdown file into `policy/` and deleting
`data/policy_index.json`.
