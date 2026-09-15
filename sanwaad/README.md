# संवाद Sanwaad — closed-loop grievance resolution

A public complaint and a support call are two channels of the same grievance.
Sanwaad runs both through **one LangGraph state machine** grounded in **one
policy index**, so the written reply and the spoken answer provably cite the
same clauses — and it emits a receipt proving it.

Four agents read every inbound comment before anything is written. Each exists
because a single comment, read alone, does not contain what it needs:

| Agent | Question it answers | Why triage cannot |
|---|---|---|
| **listener** | Is anyone talking about us, tagged or not? | A mentions queue only sees the people polite enough to @ you |
| **judge** | Who is saying this — customer, audience, troll, bot? | The words are identical; the account is not |
| **pattern** | Is this the ninth of these? | No single comment says "there is an outage" |
| **ghostwriter** | What do we say, in our voice, that is true? | — |

```
every platform, tagged or not
        │
        ▼
   listener ─────────────►  dedupe, mark untagged
        │
        ▼
   ┌─ triage ──────────────┐  cheap model, every inbound item
   │   severity floor      │  a severity-5 item is never dropped, whatever its category
   ▼                       │
  pattern  ──►  judge  ──►  prioritise
   how many        who is      one queue order, and the only
   of these?       saying it   place a case is dropped unanswered
        │
        ▼
 retrieve  ◄─── policy index (33 clauses, local ONNX embeddings, ₹0)
        │
        ▼
ghostwriter ──► ground_check ──┐  every claim must trace to a clause
        ▲                      │  ungrounded → revise (max 2) → human
        └──────────────────────┘
        │
        ▼
     plan ──► propose the fix (refund? ticket?) — code checks 9 rules
        │
        ▼
  review_gate ── interrupt() ──► a person approves the reply AND each money move
        │                        (a crisis ALWAYS stops here)
        ▼
    publish ──► act ──► escalation ──► voice (WebRTC, same clauses) ──► close
                 └─ re-checks every rule, then executes approved actions via tools
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
python -m sanwaad.evals.trajectory  # 15 scenarios, graded step by step
pytest tests/ -q                # no API key required
```

First run downloads a ~470MB ONNX embedding model. Every later start is instant.

Add `GOOGLE_API_KEY` to `.env` for real triage/drafting/grounding. Add
`SARVAM_API_KEY` + `MURF_API_KEY` to place actual voice calls.

## The four agents

**Listener — the complaints that were never addressed to you.** The comments
that damage a brand are almost never in the mentions tab. Someone writes "this
app just ate ₹4,500" under a stranger's post and no notification is ever
generated, because you were described rather than tagged. The listener searches
for the brand being *named*, records which happened, and remembers what it has
already handed downstream — a queue that re-opens a case on every poll
double-replies in public, which is the one mistake a support account cannot
take back. On our own feeds (the Play Store listing) the name is not required
at all: there, "this app" is us.

**Judge — the same words are not worth the same reply.** An hour-old account
with no karma posting *"SCAM, they hang the site on purpose, BOYCOTT"* and a
two-year-old account quoting a UTR are not the same event. The scoring is a
function, not a model call: the signals are enumerable (account age, karma,
prior cases, whether the text contains something only the aggrieved could
know), and "why was this ignored?" needs a list of named signals as its
answer, not a softmax. A model is bought exactly once, for the ambiguous
middle band, and may only move the verdict *within* that band — otherwise one
persuasive paragraph promotes a throwaway to a priority customer.

Two rules that matter more than the score:

- **Reach overrides suspicion.** The same accusation from a 61,000-follower
  account still gets answered. Silence reads as confirmation, and the audience
  is real even when the grievance is not. Judge the person to decide what to
  *spend*; judge the audience to decide whether to *speak*.
- **Anger is never evidence.** Indian customers are direct, and someone who
  has lost ₹18,000 is entitled to be furious. Only outrage with nothing
  checkable attached counts against an account.

**Pattern — watch the wave, not the drop.** One person saying payments are
failing is a ticket. Six people saying it inside twelve minutes is an incident,
and that fact exists nowhere in any single comment. Two design choices carry
this:

- **It counts people, not posts.** One furious customer posting six times is
  one furious customer. Collapsing to distinct authors before any threshold is
  checked is the difference between an early-warning system and one that
  panics at whoever is loudest.
- **It costs nothing per comment.** Similarity is a dot product against
  embeddings retrieval already computed. A detector that costs a token per
  comment gets sampled, and a sampled detector misses the first ten minutes —
  the only ten that matter.

Velocity is measured over the span the cluster actually occupies, not the
configured window, so three people in five minutes trips the alarm while the
same three across a day do not. A detected crisis then does two things: it
forces every reply through a human, because forty individually-correct
auto-replies with slightly different wording *is* the screenshot; and it tells
the ghostwriter, so the sixtieth person to report an outage is not told we
will look into their case.

**Ghostwriter — the reply, and the quiet part.** Writes in brand voice from
retrieved clauses, cites what it used, and never states a timeline no clause
supports. Meanwhile the real issue moves: escalation applies clause ESC-02 in
code, and a confirmed customer caught in a live incident gets a callback
whatever their individual severity said — their problem is not small, it is
early.

## Production building blocks

The agents are a small part of what makes this a system someone could rely on.
[`DESIGN.md`](DESIGN.md) teaches each building block as a lesson; in brief:

**Model layer.** `router.py` picks the cheapest model that can do each step and
gives it a token cap, a timeout and a fallback. `llm.structured` treats the
model as an unreliable dependency: output that fails its schema is retried
once with the problem named, errors and timeouts move to the fallback model,
and a step that still fails either runs a safe default marked `degraded` or
raises. A grounding check that could not run is never read as "grounded".

**Tools.** Four tools, one per rung of the risk ladder — `lookup_transaction`
(read), `open_ticket` (low-risk write), `post_reply` and `initiate_reversal`
(high-risk writes). Every call goes through `ToolRegistry.call`: does the tool
exist, may this agent call it, are the arguments valid, is there an approval
bound to these exact arguments, run with a timeout and retry only when safe,
validate the output, write a redacted audit record. Each contract also exports
as an MCP tool definition.

**Approvals.** The model suggests; code validates; a person approves; the tool
executes. For Karthik's ₹640 double debit, `plan` proposes reversing
`NP-TXN-640-B`, `validate_action` runs nine named checks against the ledger
(exists, owned by this author, identity verified, amount matches, eligible
under RFD-06, under the ₹25,000 ceiling, not already reversed…), the console
shows the proposal with every check, and `act` validates *again* before
calling the tool, because the ledger may have changed while the case waited.
Moving money can never be auto-approved.

**Memory and state.** `python -m sanwaad.memory` prints eight stores —
workflow state, the cross-case window, policy knowledge, dedupe ids, human
corrections, traces, the tool audit log and the systems of record — with where
each lives, how long it is kept, whether a model may see it, and what happens
to personal data. Long-lived stores are redacted at write time.

**Orchestration.** `agents.py` gives every agent a contract; the graph enforces
that each node writes only its own keys and the registry enforces that each
agent calls only its own tools.

**Context.** Each step gets the smallest context that lets it decide.
Customer text, model-written summaries and tool results arrive inside
`<untrusted>` blocks that cannot be closed from inside, and every prompt
carries the rule for reading them.

**Evaluation.** `python -m sanwaad.evals.trajectory` runs 15 scenarios —
happy path, ambiguous, out of scope, partial information, four policy edges,
tool failure, two malicious inputs, two escalations, a live incident and a
troll — and grades every step, not just the reply. A failure is reported at
the first step that went wrong, and three safety invariants are checked on
every run.

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
| listener | none | free, every poll |
| triage | `gemini-2.5-flash-lite` | every inbound item |
| pattern | local ONNX | free, every inbound item |
| judge | rules | free, every inbound item |
| judge, second opinion | `gemini-2.5-flash-lite` | only the ambiguous band |
| draft + grounding | `gemini-2.5-flash` | genuine complaints only |
| plan | `gemini-2.5-flash` | complaints being answered; every proposal checked by code |
| retrieval | local ONNX | free, every turn, both channels |
| voice | Sarvam STT + Gemini + Murf | escalations only |

Note which of the four agents cost anything. The listener is I/O, the pattern
agent is a dot product against vectors retrieval already needed, and the judge
is a scoring function that buys one cheap opinion only when its own signals
are inconclusive. Adding three agents to the pipeline added roughly nothing to
the per-comment bill — and *removed* cost, because a troll no longer gets a
drafting call and an off-topic comment never reaches one.

Praise and off-topic comments cost exactly one flash-lite call and stop. The
voice leg runs over browser WebRTC, which carries no per-minute telephony
charge at all — Exotel stays wired up for customers who want a real phone call.

`closure.total_cost_inr` is the per-case rollup across both channels, in the
same INR/USD shape as the existing agents' CSV export, so text and voice land
in one comparable number rather than two dashboards.

## Safety posture

Auto-posting is deliberately hard to earn: severity ≤ 2, fully grounded, no
money promised, no private data needed, no crisis in progress, **and no money-moving action proposed**.
Everything else waits for a human.
An LLM posting an unsupervised apology about someone's money is the failure
mode that ends a pilot. `SANWAAD_ALLOW_POSTING` gates writes to real
platforms and defaults to off.

One thing the judge deliberately does **not** do: mark a case handled. A
comment ruled not worth answering is still triaged, still fingerprinted, still
counted by the pattern agent and still visible in the console. "We ignored it"
and "we never saw it" are different failures, and only one of them is
defensible — so a troll's post still contributes to spotting the outage
underneath it.

## Layout

```
sanwaad/
  config.py         model tiers, pricing, the auto-post / judge / crisis policies
  models.py         domain types
  llm.py            structured calls + per-call cost, with an offline mode
  listener.py       multi-channel polling, dedupe, untagged-mention detection
  judge.py          author scoring — signals, bands, the reply-worthy rule
  pattern.py        the cross-case window, clustering, crisis levels
  agents.py         agent contracts: role, writes, tools
  actions.py        proposed fixes and the checks they must pass
  context.py        minimal, trust-separated prompt context
  memory.py         the memory map and retention
  tools/            contracts, registry, built-in tools, mock ledger
  evals/            golden set, retrieval eval, trajectory eval, harness
  DESIGN.md         the course: agentic system design through this code
  consistency.py    the receipt, and the contradiction table
  graph/            state, nodes, edges
  rag/              clause parsing, local ONNX embeddings, hybrid retrieval
  connectors/       mock feed, Reddit
  voice/            brief builder (the bridge), WebRTC agent
  api/              FastAPI + review console + call page
  policy/           the knowledge base — plain markdown, `## [ID] Heading`
```

Adding a channel means writing one `Connector` and listing it in
`SANWAAD_CHANNELS`; the state machine does not change.
Adding a policy means dropping a markdown file into `policy/` and deleting
`data/policy_index.json`.
