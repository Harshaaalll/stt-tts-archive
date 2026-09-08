# stt-tts — Indian-language voice agents, and what came after

An R&D repo for real-time speech agents that hold a phone conversation in
Hindi, Hinglish and English. It contains two things:

- **the voice-agent bake-off** — ten STT → LLM → TTS pipelines for the *Fusion
  Finance* collections agent, all speaking the same `/fusion-mfi-ws/` WebSocket
  contract so Exotel can be pointed at any of them without changing a URL;
- **[`sanwaad/`](sanwaad/README.md)** — the current project. A LangGraph state
  machine that resolves a grievance across the written channel and the voice
  channel from one policy index, and emits a receipt proving both cited the
  same clauses.

If you are here for the current work, start at the Sanwaad section below.
The pipelines after it are the road that led there, and the History section
walks that road commit by commit.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # every key is optional

python -m sanwaad.demo        # Sanwaad CLI walkthrough, no key needed
python -m sanwaad.api.server  # review console at http://localhost:7870
pytest tests/ -q

python deepgram_gemini_murf/server.py   # a bake-off agent, port 7860
```

The bake-off servers all bind port 7860 — run one at a time. Sanwaad is on
7870, so it can run alongside any of them.

## Sanwaad — the current project

A public complaint and a support call are the same grievance arriving through
two doors. Sanwaad runs both through **one LangGraph state machine** grounded
in **one policy index**, so the posted reply and the spoken answer provably
cite the same clauses — and it emits a receipt saying so.

Four agents read every comment before a word is written, because a comment
read alone does not contain what a reply needs:

| Agent | Question | Why one comment cannot answer it |
|---|---|---|
| **listener** | Is anyone talking about us, tagged or not? | A mentions queue only sees the people polite enough to @ you |
| **judge** | Customer, audience, troll or bot? | The words are identical; the account is not |
| **pattern** | Is this the ninth of these? | No single comment says "there is an outage" |
| **ghostwriter** | What do we say that is true, in our voice? | — |

```
every platform, tagged or not
        │
        ▼
   listener ─────────────►  dedupe, mark untagged
        │
        ▼
   ┌─ triage ──────────────┐  cheap model, every inbound item
   │   severity floor      │  a severity-5 item is never dropped
   ▼                       │
  pattern  ──►  judge  ──►  prioritise
   how many        who is      one queue order, and the only place
   of these?       saying it   a case is dropped unanswered
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
  review_gate ── interrupt() ──► human approves in the console
        │                        (a crisis ALWAYS stops here)
        ▼
    publish ──► escalation ──► voice (WebRTC, same clauses) ──► close
                                                                  │
                                          consistency receipt ◄────┘
```

```bash
python -m sanwaad.demo        # CLI walkthrough, no API key needed
python -m sanwaad.api.server  # review console at http://localhost:7870
pytest tests/ -q              # 102 tests, no key required
```

With no keys at all the graph, retrieval, gating and receipts are all real —
only the model calls are stubbed. First run downloads a ~470MB ONNX embedding
model; every later start is instant.

**What it is made of**

| Path | Does |
|---|---|
| `sanwaad/listener.py` | Multi-channel polling, dedupe, untagged-mention detection |
| `sanwaad/judge.py` | Author scoring — signals, bands, the reply-worthy rule |
| `sanwaad/pattern.py` | The cross-case window, clustering, crisis levels |
| `sanwaad/graph/` | State, nodes and edges — the phase machine |
| `sanwaad/rag/` | Clause parsing, local ONNX embeddings, RRF fusion, agentic retrieval |
| `sanwaad/policy/` | The knowledge base — plain markdown, `## [ID] Heading` |
| `sanwaad/connectors/` | Reddit and a mock feed; adding a channel is one `Connector` |
| `sanwaad/voice/` | The brief builder that bridges to the voice leg, and a WebRTC agent |
| `sanwaad/api/` | FastAPI, the human review console, the call page |
| `sanwaad/consistency.py` | The receipt, and the table of clause pairs that cannot both hold |

**Three ideas worth stealing** (the four agents are covered in Sanwaad's own README)

1. **The consistency receipt.** The real failure of a support org is not a
   wrong answer, it is *two* answers — social says seven days, the call centre
   says three. Both channels retrieve from one index, so which clauses each
   relied on is recorded and compared, and a contradiction is flagged
   mechanically instead of being discovered in a screenshot.
2. **Retrieval runs on triage's English summary, not the raw comment.**
   Measured, not assumed: Latin-script Hinglish lands nowhere near English
   policy text in this embedding space, so `"paise wapas nahi aaye"` misses the
   refund clauses entirely. Triage already produces the summary, so the fix is
   free.
3. **Phases belong in edges, not in prompts.** The collections agents in this
   repo carry a `_TERMINATE_TOOL_DESCRIPTION` full of capitalised FORBIDDEN
   clauses — prompt engineering doing a control-flow job. Here the graph owns
   the phase, so transitions are deterministic, inspectable and testable
   without spending a token.

**Cost and safety.** Triage runs on `gemini-2.5-flash-lite` for every item;
drafting and grounding only run on genuine complaints; retrieval is local and
free; voice runs on escalations only, over browser WebRTC with no per-minute
telephony charge. Praise costs exactly one flash-lite call and stops. The
listener, the pattern agent and the judge are I/O, a dot product and a scoring
function respectively — adding them cost roughly nothing per comment and
*removed* cost, since a troll no longer earns a drafting call. Auto-posting
has to be earned — severity ≤ 2, fully grounded, no money promised, no private
data needed, no crisis in progress — and `SANWAAD_ALLOW_POSTING` gates writes
to real platforms, defaulting to off.

Full detail, including the cost table and the layout, in
[`sanwaad/README.md`](sanwaad/README.md).

## The pipelines

Each directory is the same three files — `voice_agent.py` (the pipeline),
`agent.py` (the Fusion Finance persona wired into it), `server.py` (the
FastAPI/WebSocket endpoint) — differing only in which providers they call.

| Directory | STT | LLM | TTS |
|---|---|---|---|
| `deepgram_gemini_murf/` | Deepgram | Vertex Gemini (explicit cache) | Murf |
| `deepgram_gemini_murf_with_filler/` | Deepgram | Vertex Gemini | Murf, with fillers |
| `deepgram_groq_murf/` | Deepgram | Groq Llama-3.3-70B | Murf |
| `gemini_gemini_murf/` | Gemini | Vertex Gemini | Murf |
| `sarvam_gemini_murf/` | Sarvam `saarika` | Vertex Gemini | Murf |
| `sarvam_gemini_murf_with_fillers/` | Sarvam | Vertex Gemini | Murf, with fillers |
| `sarvam_groq_murf/` | Sarvam | Groq Llama-3.3-70B | Murf |
| `sarvam_sarvamllm_murf/` | Sarvam | Sarvam `sarvam-m` | Murf |
| `simple_agent/` | Sarvam | Sarvam | Murf |
| `simple_sarvam_agent/` | Sarvam | Gemini | Sarvam |

The two `simple_*` agents exist to measure the provider-side latency floor:
no fillers, no sanitiser, no normaliser, no tools, no tracer. Whatever they
cannot get under is not the application's fault.

## Shared modules

| File | Does |
|---|---|
| `prompt_blocks/` | The system prompt as ~25 composable blocks — negotiation strategy, identity verification, lie detection, PTP collection, settlement and EMI phases, few-shot examples |
| `fusion_prompt_panch.py` | Assembles the blocks into the full settlement prompt (~19k chars) |
| `fusion_contextual_prompt_explore.py` | The exploration-phase variant, plus a Murf-specific build |
| `fusion_prompt_slim.py` | ~4k-char drop-in for Groq free-tier A/B tests — never production |
| `history_retriever.py` | Per-flow asyncpg pools; pulls a customer's recent interactions into the prompt |
| `text_normalizer.py` | Digits → spoken words, Indic and English, so TTS says "अठारह हज़ार" not "18000" |
| `filler_classifier.py` | Buckets an utterance positive/negative/neutral to pick the right filler |
| `latest_system_prompt*.txt` | Snapshots of assembled prompts, kept for diffing |
| `recording_taglines.txt` | Maps each recording to the exact optimisation config that produced it |

## History

Read top to bottom, this repo is one question narrowing: *why does an Indian-
language voice agent feel slow and wrong, and which layer is to blame?*

**Bootstrap — latency is the whole problem** (2026-07-10, `b3cc953`→`e9aff9c`)
The first working pipeline, immediately followed by five commits that do
nothing but cut delay: log levels trimmed, local VAD put behind
`USE_LOCAL_VAD` and then disabled outright, Gemini moved to the Singapore
region, the prompt cache pre-warmed, barge-in unmuted during the greeting, and
Deepgram pinned to the call's actual language instead of `multi` — multi-
language detection was costing more than it earned.

**Fillers and semantic VAD — latency you cannot remove, you hide** (2026-07-10,
`313e211`→`c7c52bf`, branches `semantic_vad` and `fillers`)
Once the network floor was hit, the remaining silence had to be covered rather
than eliminated. Semantic VAD landed on its own branch, merged into `fillers`,
and `filler_classifier.py` grew out of the finding that an acknowledgment has
to match the sentiment of what was just said — "जी बिलकुल" after a refusal
reads as a bot.

**The bake-off — stop guessing which provider is at fault** (2026-07-13 →
07-16, `d0b93c0`→`c551c8e`)
Rather than argue about vendors, one directory per combination, identical
persona and endpoint, measured against each other. `bf40725` calls Deepgram +
Gemini + Murf the combination to build on; `54bf3a4` fixes it for non-English
languages; `a78408a` finds Sarvam + Gemini + Murf works too, and `c551c8e`
brings the Sarvam agent up to parity. `1871c0b` switches Murf to the FALCON
model. `dc79311` lands the natural-sounding prompt and names what is still
missing: tools.

**Tools and per-customer context** (2026-07-17 → 08-20, `64750ab`→`5f81c18`)
Tool calling fixed, then the prompt taught to carry a specific customer's
details and follow-up logic. *These three commits live on the `fillers` branch
of the original remote, not on `main`.*

**Sanwaad — the constraint moves up a layer** (2026-09-08, `b41789a`)
By this point the phase machine lived inside the prompt: a
`_TERMINATE_TOOL_DESCRIPTION` full of capitalised FORBIDDEN clauses, which is
what prompt engineering looks like when asked to do control flow. Sanwaad
takes that job back — phases become graph edges, grounding becomes a gate a
draft has to pass, and the written and spoken channels are made to retrieve
from one clause index so they can be *proved* to agree.

**Four agents in front of the graph** (2026-09-09)
The first cut answered whatever it was handed, one comment at a time — which
meant it drafted a careful grounded reply to an hour-old throwaway shouting
"SCAM", and saw a six-person payment outage as six unrelated tickets. Three
agents were added ahead of the writer, each answering a question no single
comment contains: a **listener** that catches the brand being named rather
than tagged, a **judge** that reads the account behind the words, and a
**pattern** agent that counts distinct people saying the same thing inside a
window. All three are free per comment — I/O, a scoring function, and a dot
product against embeddings retrieval already computed. Details in
[`sanwaad/README.md`](sanwaad/README.md).

## Configuration

`.env.example` documents the Sanwaad variables. The bake-off agents read
`SARVAM_API_KEY`, `DEEPGRAM_API_KEY`, `GROQ_API_KEY`, `MURF_API_KEY`,
`MURF_VOICE_ID` / `MURF_MODEL`, the `GCP_*` / Vertex variables, and the
per-flow `DB_*_{FLOW}` set used by `history_retriever.py`.

Generated state — the policy index, the SQLite checkpoint DB, traces and
feedback logs — is gitignored and rebuilt on first run.

## Repo layout

```
sanwaad/            the current project (own README)
  listener.py       hears every platform, tagged or not
  judge.py          reads who is speaking
  pattern.py        counts how many are saying it
  graph/            the state machine that writes and escalates
tests/              pytest suite for the Sanwaad layer
prompt_blocks/      composable system-prompt blocks
<stt>_<llm>_<tts>/  one directory per pipeline combination
fusion_*.py         prompt assembly
*.py                shared helpers (history, normalisation, fillers)
```
