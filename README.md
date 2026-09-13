# Financial Reconciliation Engine

## Local reliability milestone (in progress)

The new batch path imports immutable source files atomically, treats identical
replays as no-ops, and commits only disjoint deterministic allocations. Conflicting
source corrections fail without overwriting prior data. Competing matches remain
a review queue. PostgreSQL constraints preserve ownership across runs; an explicit
balanced allocation group can represent a many-to-one batch.

Start with [the local operations runbook](docs/local-operations.md). It includes
the test commands, failure/retry exercise, metrics and remaining release gates.
This is a local milestone, not a verified cloud deployment. The existing agent
pipeline below remains an experimental investigation path: speculative batch
proposals cannot be committed without a validated complete allocation group.

An agentic reconciliation pipeline for ACH/card settlement — matches an
internal transaction ledger against an external network settlement file
that shares **no common transaction identifier**, using deterministic
rules where possible and an LLM investigator (via MCP tools) only where
rules genuinely can't resolve the case.

## Architecture

```
Postgres (internal_ledger, network_settlement)
        │
        ▼
Deterministic matcher  ── clears ~80% of volume on rules alone
        │ (unresolved/ambiguous cases only)
        ▼
LangGraph StateGraph (raw StateGraph, not langgraph.prebuilt):
  classify → resolve_batches → investigate → propose_resolutions
        │                          │                │
        │                    MCP server tools   structured proposal,
        │                    (SQL-backed)       schema-validated
        ▼
  human_approval (interrupt() — pauses for anything high-value
                   or low-confidence, resumes on human decision)
        │
        ▼
     summarize
```

See `docs/settlement_file_field_mapping.md` for how the synthetic dataset
maps to a real settlement report's field structure.

## Key engineering decisions

**Deterministic-first.** The matcher (`matching/deterministic_matcher.py`)
clears clean matches, fee variance, splits, and descriptor-mangled cases
on rules alone — no LLM call. Only what rules can't resolve (batching,
timing outliers, duplicates, orphans) reaches the agent layer. This
mirrors the same "don't spend a model call on what a rule can solve"
decision made in Project 1 (clause chunking over recursive for RAG).

**Provider-agnostic via litellm.** Both the investigator
(`agent/investigator.py`) and proposer (`agent/proposer.py`) route model
calls through `litellm`, so switching providers (Claude, Gemini, local
Ollama) is a one-line env var change (`INVESTIGATOR_MODEL`,
`PROPOSER_MODEL`), not a rewrite.

**Guardrails AI was evaluated and dropped for this project.** The
original plan used Guardrails AI's `Guard.for_pydantic` for schema
validation on the resolution proposer. It worked reliably against
Gemini, but its own model-calling mechanism proved flaky specifically
against local Ollama models — roughly 75% of calls returned completely
empty output, while a hand-written `litellm` call using the exact same
model succeeded 8/8 times in the same session. Rather than keep patching
around a third-party integration bug, `proposer.py` now calls the model
directly and validates the result against the same Pydantic schema with
its own retry-on-failure loop — the same *principle* Guardrails
implements (nothing gets trusted without passing schema validation),
without the library. This was a deliberate choice, not a workaround left
in by accident: Guardrails works fine against hosted APIs, and the
decision to drop it here was made after isolating the failure to Ollama
specifically, not assumed.

**Deterministic dollar threshold, independent of model confidence.**
`human_approval` forces review whenever either the proposer flags low
confidence *or* the transaction amount crosses `HIGH_VALUE_THRESHOLD` —
the threshold check is hard-coded, not delegated to the model's own
judgment about whether something needs sign-off.

## Known limitations (intentionally deferred)

- **Matching is greedy, not globally optimal.** Batch resolution uses
  subset-sum search on amount alone, which produces false positives when
  multiple transactions share an amount (measured ~59% precision in
  testing). A real fix — weighted composite scoring plus optimal bipartite
  assignment — is deliberately out of scope for the portfolio version;
  see project notes for the plan to apply this at an actual employer.
- **`card_number_masked` is present in the schema but unused in matching.**
  It's a stronger candidate key than `account_last4` and is the natural
  next upgrade to the matcher.

## Docker

The image is a CLI pipeline + MCP server, not a web service — no HTTP
endpoints, so no `EXPOSE`/`uvicorn`. Build:

```bash
docker build -t recon-engine .
```

`data/output` (generated CSVs) and `data/cache` (investigation/proposal
cache) are written at runtime, not baked into the image — mount a volume
so they persist across separate `docker run` invocations:

```bash
docker run --env-file .env -v $(pwd)/data:/app/data recon-engine \
  python -m recon_engine.data_gen.generate_dataset

docker run --env-file .env -v $(pwd)/data:/app/data recon-engine \
  python -m recon_engine.db.loader

docker run --env-file .env -v $(pwd)/data:/app/data recon-engine \
  python -m recon_engine.graph.run_classification

docker run --env-file .env -v $(pwd)/data:/app/data recon-engine \
  python -m recon_engine.evaluation.evaluate_agent_resolutions
```

Note: `run_classification`'s human-in-the-loop `interrupt()` needs an
interactive terminal to prompt for approval — add `-it` to that `docker run`
if running it directly (not needed for the others, which don't prompt).

## Setup

```bash
uv sync
cp .env.example .env   # fill in DATABASE_URL (Neon Postgres) and a model provider key
uv run python -m recon_engine.data_gen.generate_dataset
uv run python -m recon_engine.db.loader
uv run python -m recon_engine.matching.evaluate_baseline_db
uv run python -m recon_engine.graph.run_classification
```
