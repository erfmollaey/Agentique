# Project Plan — Telegram AI Research Bot

**Document type:** Master roadmap
**Status:** Living document
**Primary evidence source:** [`ARCHITECTURE.md`](./ARCHITECTURE.md) — Current Architecture & Audit Reference

> This plan describes intent. It does not describe what exists.
>
> **State of the two references:**
> - [`ARCHITECTURE.md`](./ARCHITECTURE.md) is the **historical audit snapshot** of the repository *before* Phase 1. Its findings are preserved unmodified as the defect baseline.
> - For what exists *now*, see the maturity table below and [`phases/PHASE-01-INFRASTRUCTURE.md`](./phases/PHASE-01-INFRASTRUCTURE.md) § 14.
> - Where the audit says "NOT VERIFIED", this document says the same.

---

## Current Project Status

**Current Phase:** Phase 3 — Research Engine
**Status:** In Progress — P3.0 (Contract Freeze) complete; P3.1 implemented; P3.2 implemented; P3.3 complete; **P3.4 (Extraction + Persian Fixtures) complete (2026-10-07)**; **P3.5 (Deduplication + Ranking) complete (2026-10-07)**; **P3.6 (Research Planning) complete (2026-10-08)**; **P3.7 (Per-source summarization + claim extraction) complete (2026-10-08)**; **P3.8 (Synthesis + Citations) complete (2026-10-08)**; **P3.9 (read-only audit + contract freeze) complete (2026-10-08)**; **P3.9-A prerequisites B1/B2 complete (2026-10-09)** — SourceChunk production + SourceRepository ranking persistence; **P3.9-B B3/B4 partial (2026-10-09)** — container wiring + ownership-scoped lifecycle verified, omissions documented; **P3.9-C B5/B6 operational decisions complete (2026-10-09)** — B6 resolved (AD-046, AD-047), B5 policy resolved (AD-045) with its numeric value explicitly **BLOCKED** pending a measured run; **P3.9 stabilization pass (2026-10-09)** — four reproducible defects fixed, repository green (`1981 passed`, `ruff`/`mypy` clean, `alembic check` clean); **P3.9-D (research orchestration) complete (2026-10-10)** — B7 idempotency (migration `0005_research_request_key`), cooperative deadline + outcome model, `RESEARCH_MAX_SOURCES` work bound, the pipeline orchestrator, one-Celery-task-per-run execution (AD-043) with exactly one terminal message, and the `/research` handler (AD-029/AD-046) with a dedicated research allowance (AD-047, numbers **BLOCKED**); **P3.10 closure pass complete (2026-10-13)** — citation rendering so a delivered answer carries visible inline references and a source list (FR-40, previously the blocking gap), per-run token accounting written to the run (FR-9/SR-5/T-25), and the Tavily provider decision recorded (**AD-050**, superseding the AD-036 selection). Measured **`2221 passed, 0 failed`**, `ruff` clean, `mypy app` clean (96 files), `alembic check` clean. The P3.9-D figure of `2063 passed / 1 failed` is superseded — the environmental DNS failure it recorded **no longer reproduces** and the fetch boundary was never touched. Full record: audit § U and § V, PHASE-03 § 23 and § 24.

Phase 1 and Phase 2 are COMPLETE and closed. Reconciled at P3.0 (2026-10-07): Phase 1 credential rotation (P0-3/SR-4) was completed by a human before Phase 2; GitHub Actions has run and is green; `make verify` is green; the Docker Compose configuration was verified. Telegram message delivery remains the one environment-limited item — `api.telegram.org` refused the connection from the development environment, and delivery logic is covered by substituted-transport tests (see [`phases/PHASE-02-AI-CHAT.md`](./phases/PHASE-02-AI-CHAT.md) § 14).

**Current implementation maturity:**

| Area | Status | Change |
|---|---|---|
| Telegram integration | Implemented | Text, `/start`, `/help`, `/new`, `/conversations`, `/reset`; long-polling transport unchanged; replies target the originating chat |
| FastAPI backend | Partial | Lifespan lifecycle, `/health`, `/ready` now probing the broker **and** the database; no API surface beyond health |
| Celery | Partial | Time limits, acks-late, bounded retry, result retention; single-queue, no routing; the task now runs the chat service |
| Redis | Partial | Broker healthy and probed by `/ready`; no authentication |
| PostgreSQL | **Implemented** | Was "Declared but not implemented". Eight tables now (Phase 2: users/conversations/messages; Phase 3 P3.1: research_runs/sources/source_chunks/claims/citations), Alembic migrations through `0004_research_synthesis` (P3.3: the five research tables; P3.7: `sources.summary`, a `NOT NULL` `citations.quote`, and a chunk/source ownership constraint; P3.8: `research_runs.synthesis`), indexes; `/ready` probes it |
| LLM integration | Implemented | Credentials, endpoint, model, timeout, token limit, validation, usage capture, and a bounded fallback chain are configuration; one provider implementation behind a factory. **Verified against the live provider** — see `AD-028` |
| Conversation persistence | **Implemented** | Was "Not implemented". Users, conversations, and messages persist and survive a restart |
| Web research | **Implemented** | Was "Not implemented". `/research` runs end to end: plan → search → fetch/extract/chunk → dedup/rank → per-source evidence → verified synthesis → **one** terminal message, and a completed run delivers a **cited** answer — inline `[n]` markers plus a numbered source list, chunked safely for Telegram (P3.10; FR-40 was the last missing capability). The run persists the token usage its own provider calls reported (FR-9/SR-5/T-25), and search runs through the `SearchProvider` port with Tavily as the selected default (AD-050). SSRF boundary (P3.3), Persian extraction fixtures (P3.4), deterministic dedup + ranking (P3.5), persisted planning (P3.6), evidence-grounded claims (P3.7), verified citations (P3.8), chunk production and ranking persistence (P3.9-A/B), and orchestration with B7 idempotency (P3.9-D) are complete. **Deferred and recorded, not complete:** the deadline value (`RESEARCH_DEADLINE_SECONDS` is `None`), the rate-limit numbers, cost-budget enforcement, and a consolidated list of unprocessable page types — PHASE-03 § 24.6 |
| File intelligence | Not implemented | Unchanged — Phase 4 |
| Research workspace | Not implemented | Unchanged — Phase 5. A `conversations.project_id` column is reserved for it (AD-015) |
| Agent orchestration | Skeleton only | Unchanged — Phase 6 |
| SaaS | Not implemented | Unchanged — Phase 8 |
| Tests | **Implemented** | Was "Not implemented". **Current: `2221 passing, 0 failing`** (2221 collected; measured 2026-10-13 at P3.10), `ruff check app tests` clean, `mypy app` clean (96 source files), `alembic check` clean, `make verify` green. Database tests run against a real PostgreSQL; search, fetch, ranking, planning, evidence, synthesis, citation-rendering, and token-accounting tests run against fakes and mocked transports with no external call. This supersedes the `2063 passing, 1 failing` figure recorded at P3.9-D: that failure was the SSRF system-resolver test, attributed to this machine wildcard-resolving every DNS name into `198.18.0.0/15`, and **it no longer reproduces** — `nonexistent.invalid` now fails resolution correctly, so the test passes for the right reason and the fetch boundary was never modified to achieve it. The `1981 passing, 0 failing` figure was true at the 2026-10-09 stabilization pass. `alembic current` is `0005_research_request_key (head)`. Full record: Phase 3 audit § T, § U, and § V |

**Not improved by Phase 2, deliberately:** the product still performs no
research. Phase 2 made the bot hold a persistent conversation. It did not add
search, sources, or citations.

**Phase 2 completion evidence** is recorded in
[`phases/PHASE-02-AI-CHAT.md`](./phases/PHASE-02-AI-CHAT.md) § 14: the
requirement matrix, the validation table, the real end-to-end run against the live
provider, and an explicit list of what could not be verified here.

**Not improved by Phase 1, deliberately:** the product still performs no
research. Phase 1 made the existing flow reliable, secure, and testable; it did
not add capability.

### Evidence for the status above

Taken directly from the audit's readiness table and section 3:

- **Telegram — Partial.** Bot initialization and long polling exist (`app/bot/dispatcher.py:6-9`, `app/main.py:19-24`). One command handler (`/start`) and one unfiltered catch-all handler exist (`app/bot/handlers.py:9, 15`). There is no error handler, no content-type filter, no rate limiting, and no session handling.
- **FastAPI backend — Partial.** The app exists at `app/main.py` (24 lines) with one static `/health` route. There is no service layer, no dependency injection, and no graceful shutdown.
- **Celery — Partial.** Configured at `app/core/celery_app.py` with a Redis broker and one task. No time limits, no acks-late, no retry policy, no prefetch tuning.
- **Redis — Partial.** Declared in `.env` and used as Celery broker/backend. No authentication, no connection health probing, result keys accumulate unbounded.
- **PostgreSQL — Declared but not implemented.** A `postgres:16-alpine` service exists in `docker-compose.yml:10-20`, `DATABASE_URL` is declared in `app/core/config.py:6`, and `asyncpg`/`sqlalchemy` are pinned in `requirements.txt:6-7`. **Zero database code exists.** No models, no tables, no sessions, no migrations.
- **LLM integration — Partial.** One OpenAI-SDK call to a DeepSeek-compatible endpoint at `app/agents/supervisor.py:36-41`. The client is constructed with a hardcoded key literal (audit C-1). No timeout, no `max_tokens`, no retry, no fallback, no schema validation, no provider abstraction.
- **Tests — Not implemented.** No test files, no test directory, no test framework in `requirements.txt`, no CI.
- **Agent orchestration — Not implemented.** `app/agents/supervisor.py` exists as a file and a class name only. Its second method, `generate_final_answer`, has **zero call edges** and is dead code (audit M-1).

### What the audit explicitly could not verify

**NOT VERIFIED — insufficient evidence in repository.** Credential validity, whether secrets were ever committed (no `.git` exists), the intended deployment path, and the exact runtime exception text for the event-loop defect. These remain unknown and are not asserted anywhere in this plan.

---

## Phase Status Table

| Phase | Name                                | Status                      | Main Goal                         |
| ----- | ----------------------------------- | --------------------------- | --------------------------------- |
| 1     | Core Infrastructure & Stabilization | Complete                    | Make current system reliable      |
| 2     | AI Chat MVP                         | Complete                    | Build reliable conversational AI  |
| 3     | Research Engine                     | In Progress — P3.0–P3.9 and P3.9-D complete; **P3.10 closure pass complete (2026-10-13)**: the pipeline runs end to end and delivers a **cited** answer (FR-40), per-run token usage is persisted (FR-9/SR-5/T-25), and the search provider decision is recorded (AD-050). § 13 DoD is met except for items explicitly deferred and enumerated in PHASE-03 § 24.6 — chiefly the measurement-gated deadline value, the rate-limit numbers, and cost-budget enforcement | Web research pipeline |
| 4     | File Intelligence                   | Not Started                 | Document intelligence             |
| 5     | Research Workspace                  | Not Started                 | Persistent research environment   |
| 6     | Agent System                        | Not Started / Skeleton only | Multi-agent research architecture |
| 7     | Premium                             | Not Started                 | Advanced research features        |
| 8     | SaaS                                | Not Started                 | Web platform                      |
| 9     | Monetization                        | Not Started                 | Product/business layer            |

Status vocabulary is defined in [`DEVELOPMENT_RULES.md`](./DEVELOPMENT_RULES.md). "Skeleton only" for Phase 6 reflects that `app/agents/supervisor.py` exists as a naming and file-layout placeholder with no working orchestration.

### Phase documents

| Phase | Document |
|---|---|
| 1 | [`phases/PHASE-01-INFRASTRUCTURE.md`](./phases/PHASE-01-INFRASTRUCTURE.md) |
| 2 | [`phases/PHASE-02-AI-CHAT.md`](./phases/PHASE-02-AI-CHAT.md) |
| 3 | [`phases/PHASE-03-RESEARCH-ENGINE.md`](./phases/PHASE-03-RESEARCH-ENGINE.md) |
| 4 | [`phases/PHASE-04-FILE-INTELLIGENCE.md`](./phases/PHASE-04-FILE-INTELLIGENCE.md) |
| 5 | [`phases/PHASE-05-RESEARCH-WORKSPACE.md`](./phases/PHASE-05-RESEARCH-WORKSPACE.md) |
| 6 | [`phases/PHASE-06-AGENT-SYSTEM.md`](./phases/PHASE-06-AGENT-SYSTEM.md) |
| 7 | [`phases/PHASE-07-PREMIUM.md`](./phases/PHASE-07-PREMIUM.md) |
| 8 | [`phases/PHASE-08-SAAS.md`](./phases/PHASE-08-SAAS.md) |
| 9 | [`phases/PHASE-09-MONETIZATION.md`](./phases/PHASE-09-MONETIZATION.md) |

---

## Dependency Chain

```text
Phase 1 — Core Infrastructure & Stabilization
  ↓  (must be complete: reliable primitives, secrets hygiene, tests, deployable runtime)
Phase 2 — AI Chat MVP
  ↓  (must be complete: persistence layer, conversation memory, LLM service abstraction)
Phase 3 — Research Engine
  ↓  (must be complete: source model, research records, citation representation)
Phase 4 — File Intelligence
  ↓
Phase 5 — Research Workspace
  ↓
Phase 6 — Agent System
  ↓
Phase 7 — Premium
  ↓
Phase 8 — SaaS / Web Platform
  ↓
Phase 9 — Monetization
```

### Rules governing this chain

1. **The chain is a default ordering, not a hard serialization requirement.** Once a phase's dependencies are satisfied, independent work in a later phase may proceed in parallel.
2. **A dependency is satisfied by acceptance criteria, not by starting.** Phase 2 may not begin because Phase 3 is more interesting.
3. **Research functionality must never become a hidden dependency of Phase 1 or Phase 2.** If web search, fetching, or citation logic appears inside infrastructure or chat work, it is a scope violation — see [`DEVELOPMENT_RULES.md`](./DEVELOPMENT_RULES.md) § Phase Discipline.
4. **Parallel work still requires documented prerequisites.** Any exception to the ordering must be recorded in the Architecture Decision Log below with a reason.

---

## Phase 1 — Core Infrastructure & Stabilization

**Purpose:** Make the existing application reliable, secure, testable, and runnable.

**Full specification:** [`phases/PHASE-01-INFRASTRUCTURE.md`](./phases/PHASE-01-INFRASTRUCTURE.md)

This phase covers the existing foundational problems identified by the audit:

- Configuration and secrets management
- Dependency correctness
- Telegram application lifecycle
- Celery reliability
- LLM client configuration
- Error handling across the request path
- Telegram message formatting and length limits
- Health and readiness checks
- Docker and runtime infrastructure
- Logging
- Security baseline
- Foundational tests

**Explicitly excluded from Phase 1:** all web research capability. No search, fetching, extraction, or citation work belongs here.

---

## Phase 2 — AI Chat MVP

**Purpose:** Turn the existing Telegram/LLM vertical slice into a reliable conversational AI assistant.

**Full specification:** [`phases/PHASE-02-AI-CHAT.md`](./phases/PHASE-02-AI-CHAT.md)

### Current implementation (from audit, before this phase)

- A single-turn LLM call that decomposes one user message into a summary and 2–3 sub-questions (`app/agents/supervisor.py:16-44`).
- No conversation memory. `analyze_query` receives exactly one string.
- No user or message persistence of any kind.
- One command (`/start`) and one unfiltered catch-all handler.
- No streaming, no model selection, no fallback, no rate limiting.

### Implemented (see `phases/PHASE-02-AI-CHAT.md` § 14 for the per-requirement record)

- Conversation persistence: `users`, `conversations`, `messages` with Alembic migrations, foreign keys, uniqueness constraints, and indexes on the actual retrieval paths.
- Conversation history retrieval and context construction, with a bounded, configurable, deterministic truncation strategy (AD-022).
- An LLM service abstraction: the application depends on `app/domain/ports.LLMProvider`, and `app/infrastructure/llm.create_llm_client` is the single place a provider is selected (FR-16).
- Provider configuration (endpoint, key, model, provider name, fallback chain) held in settings, not in code.
- `/help`, `/new`, `/conversations`, `/reset` (with confirmation), and explicit unknown-command handling.
- Distinct, non-revealing user-facing error feedback for every failure path.
- Rate limiting extended with a daily allowance inside the existing limiter (AD-025).
- A bounded, tested model fallback chain (AD-024), and token usage recorded per assistant message (FR-30).
- Idempotent turn processing keyed on a derived `turn_id` (AD-017).

### Not implemented in this phase

Streaming, conversation summarization, a web dashboard, and any research capability. The
`analyze_query` decomposition path is retained unused for Phase 3 (AD-026).

---

## Phase 3 — Research Engine

**Purpose:** Transform the AI chat bot into an actual AI research system.

**Full specification:** [`phases/PHASE-03-RESEARCH-ENGINE.md`](./phases/PHASE-03-RESEARCH-ENGINE.md)

**None of the following exists today.** The audit found no web search, no URL fetching, no HTML extraction, no content cleaning, no deduplication, no ranking, no summarization, no citation generation, and no report generation anywhere in the repository.

### Intended pipeline

```text
User Question
    ↓
Research Planning
    ↓
Web Search
    ↓
Source Collection
    ↓
URL Fetching
    ↓
Content Extraction
    ↓
Content Cleaning
    ↓
Deduplication
    ↓
Source Ranking
    ↓
Multi-source Analysis
    ↓
Citation Generation
    ↓
Final Research Answer
```

### Expected capabilities

Web search; source collection; URL fetching; content extraction; cleaning; deduplication; ranking; source summarization; cross-source comparison; citations; research answer generation; and explicit failure handling for unavailable or unreachable sources.

### Provider selection: decided at P3.2 (AD-036), re-selected at P3.10 (AD-050)

**Tavily Search API** is the shipped default. **AD-036** selected Brave Search API on live reachability/error probes against every candidate endpoint, the published API contracts, cost, and architectural fit; **AD-050 supersedes that selection** — not the decision to put search behind a port.

**The reason is budget, and only budget.** A search credential now exists, which discharged the outstanding item the audit left open (re-test Tavily's credential path and, if it works, re-evaluate against Brave on the same criteria). It worked, and on that re-evaluation **Tavily met the required search functionality within this project's free-tier allowance, where Brave Search was not a viable free option at this project's usage.**

**What is *not* claimed.** No comparative quality or latency benchmark was run, so relevance, diversity, authoritative coverage, and Persian-language quality remain **NOT VERIFIED** for both providers; nothing was estimated in their place. Tavily is not claimed to be better on any axis, nor to be free indefinitely — provider pricing is the vendor's own published figure. Both adapters stay registered, so the choice is a `SEARCH_PROVIDER` value rather than a rewrite. The full evidence, including the benchmark that could not be completed and the trade-offs accepted, is in [`audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](./audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md) § K; the re-selection is AD-050.

### Existing partial artifact

`Supervisor.generate_final_answer` (`app/agents/supervisor.py:46-65`) is a two-stage design sketch with zero callers. It is dead code today. It may inform the Phase 3 synthesis stage, but it must not be treated as an existing capability.

### P3.0 Contract Freeze — 2026-10-07

The implementation contract is frozen in [`phases/PHASE-03-RESEARCH-ENGINE.md`](./phases/PHASE-03-RESEARCH-ENGINE.md) § 15: `/research <question>` trigger, pipeline responsibilities, port-based provider boundary (AD-006 remains open and gates P3.2), SSRF and prompt-injection security boundaries, programmatic citation trust chain, five-table persistence model (`research_runs`, `sources`, `source_chunks`, `claims`, `citations`), failure model, Persian-language testing requirement, and the recorded Celery execution-time risk. **No Phase 3 pipeline code exists yet.** P3.1 implemented the persistence foundation only: `app/domain/research/` (pure domain types + repository ports), migration `0002_research` with the five-table schema (`research_runs`, `sources`, `source_chunks`, `claims`, `citations`), research repositories wired into the UnitOfWork, and the `/delete_account` cascade extended to research artifacts (verified against real PostgreSQL; 406 tests green; `alembic check` clean; recorded in the P3.1 status section of [`audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](./audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md) and in the phase document § 14). No search, fetching, extraction, synthesis, or research orchestration has been built. P3.0 froze the implementation contract (§ 15); evidence: the audit and P3.0 record. Decisions: AD-029 … AD-035 below.

**P3.9 — Read-only audit + contract freeze (2026-10-08).** A read-only stage: production code, tests, migrations, dependencies, and configuration are unchanged; the `1833 passed / 20 failed` baseline is unchanged and is not a P3.9 regression. Outcome: **READY WITH PREREQUISITES**. `/research`'s input/output contract is frozen (§ 15.1, AD-029) with two values left explicitly unresolved rather than invented — the inbound question length cap (`PLAN_MAX_QUESTION_CHARS=2000` vs `CHAT_MAX_MESSAGE_CHARS=4000`) and the stricter research rate allowance. Run lifecycle: `ResearchRunStatus` and the database CHECK already carry `partial`, but **no component writes `running` or `partial`**, `ResearchRunRepositoryImpl.set_status` is the only research write that is not ownership-scoped, and cancellation has no representation. The critical finding is that the **`SourceChunk` producer does not exist** — `uow.source_chunks.add` has zero callers anywhere including tests — and a second one was newly found: **`SourceRepository` has no writer for `rank`, `quality_score`, `content_hash`, or `duplicate_of_id`**, so P3.5's ranking output cannot be persisted, which also silently de-ranks the P3.8 synthesis context because it orders and budgets on `sources.rank`. AD-035's *shape* is resolved as AD-043 (one task per run, all stages inside it, on the worker's long-lived loop — chained stage tasks are unavailable because `task_ignore_result=True` and nothing reads results); its *deadline value* stays open, and `task_acks_late=True` + `task_reject_on_worker_lost=True` means a hard-killed run **redelivers and re-executes from the start**. AD-044 freezes the `completed`/`partial`/`failed` decision for the orchestrator. Seven mandatory prerequisites (B1–B7) are listed in the audit § R.18; none needs a schema change except the idempotency key. Account deletion is complete and FROZEN, not a prerequisite. Full evidence: audit § R.

**P3.9-D — research orchestration (2026-10-10).** The pipeline runs end to end. **B7 (idempotency):** migration `0005_research_request_key` adds a nullable `research_runs.request_key` with `UNIQUE (user_id, request_key)` — no new table; the key is sha256 of the Telegram user id and message id (`derive_request_key`), `NULL` when either is absent; run creation is `ON CONFLICT … DO NOTHING` with a typed retryable `DuplicateMessageError`, and the intake service converges a redelivered command onto the existing run (no second run, no second acknowledgement, no second enqueue). Source-level re-execution is refused, not merged: the evidence stage claims each source with `SELECT … FOR UPDATE` under the marker `summary IS NULL` (P3.7 writes summary+claims+citations atomically, so the marker is exact), checked both before any provider spend and inside the persisting transaction (`SourceAlreadyAnalyzedError`). **B5 (deadline + outcomes):** `app/domain/research/outcome.py` holds the latching `CooperativeDeadline` (injected clock; `RESEARCH_DEADLINE_SECONDS` defaults to `None` = unbounded — no number was invented), `RunOutcomeKind`, and `summarize_run` (counts-only). Typed `ResearchDeadlineError`/`ResearchPipelineError`/`ResearchNoEvidenceError` are mapped in `failures.py` (closing B5-P4 and B6-P2). **B5-P2:** `RESEARCH_MAX_SOURCES` (default 5, ceiling 20) bounds fetched and selected candidates; sources beyond the bound are recorded, not silently dropped. **Orchestrator** (AD-043/AD-044): `ResearchOrchestrator.execute` claims the run to `running`, plans, searches, fetches/extracts/chunks, deduplicates and ranks (persisting P3.5 metadata), collects evidence, and synthesizes — per-stage short transactions, providers outside them; degradation tallies decide `completed` vs `partial`, written by synthesis via `terminal_status`, with the terminal summary written by the new status-free `set_summary_note`. **Celery** (AD-043): one task per run, exactly one terminal message, retry classification corrected so `SoftTimeLimitExceeded` never reaches the retry path (B5-C1). **`/research`** (AD-029/AD-046): intake-bounded by `PLAN_MAX_QUESTION_CHARS`, throttled through the dedicated research allowance (AD-047, numbers **BLOCKED**), one acknowledgement, and a compensating `failed` transition if the enqueue itself fails. *(The `2063 passed / 1 failed` figure originally recorded here is superseded — see the P3.10 entry above and audit § V.4.)* **B7 (idempotency):** migration `0005_research_request_key` adds a nullable `research_runs.request_key` with `UNIQUE (user_id, request_key)` — no new table; the key is sha256 of the Telegram user id and message id (`derive_request_key`), `NULL` when either is absent; run creation is `ON CONFLICT … DO NOTHING` with a typed retryable `DuplicateMessageError`, and the intake service converges a redelivered command onto the existing run (no second run, no second acknowledgement, no second enqueue). Source-level re-execution is refused, not merged: the evidence stage claims each source with `SELECT … FOR UPDATE` under the marker `summary IS NULL` (P3.7 writes summary+claims+citations atomically, so the marker is exact), checked both before any provider spend and inside the persisting transaction (`SourceAlreadyAnalyzedError`). **B5 (deadline + outcomes):** `app/domain/research/outcome.py` holds the latching `CooperativeDeadline` (injected clock; `RESEARCH_DEADLINE_SECONDS` defaults to `None` = unbounded — no number was invented), `RunOutcomeKind`, and `summarize_run` (counts-only). Typed `ResearchDeadlineError`/`ResearchPipelineError`/`ResearchNoEvidenceError` are mapped in `failures.py` (closing B5-P4 and B6-P2). **B5-P2:** `RESEARCH_MAX_SOURCES` (default 5, ceiling 20) bounds fetched and selected candidates; sources beyond the bound are recorded, not silently dropped. **Orchestrator** (AD-043/AD-044): `ResearchOrchestrator.execute` claims the run to `running`, plans, searches, fetches/extracts/chunks, deduplicates and ranks (persisting P3.5 metadata), collects evidence, and synthesizes — per-stage short transactions, providers outside them; degradation tallies decide `completed` vs `partial`, written by synthesis via `terminal_status`, with the terminal summary written by the new status-free `set_summary_note` (the lifecycle's same-state refusal, which protects terminal runs, was not relaxed). **Task:** `run_research(run_id, user_id, chat_id)` — one task per run, all stages inside it; retry classification in one place; `SoftTimeLimitExceeded` and `ResearchDeadlineError` are explicitly non-retryable (B5-C1); exactly one terminal message. **Handler:** `/research` parses, validates (`PLAN_MAX_QUESTION_CHARS`, empty → usage text, no state), throttles via `RateLimiter.check_share` (per-user window + daily, deliberately no global in-flight slot so B6-P4's cross-process leak is not inherited), acknowledges once, enqueues; a failed enqueue triggers the compensating transition `queued → failed` ("not started: queuing failed") so no run stays apparently active (no outbox table — the five-table schema is frozen). Two Phase 2 tests that used `/research` as their unknown-command example now use `/doesnotexist`. **Still blocked, unchanged:** the deadline's numeric value and the rate-limit numbers (measurement-gated); B6-P4 itself; token-ledger population. **Not verified against a live provider** — the same measurement gap AD-036 records. Evidence: audit § U; PHASE-03 § 23.

**P3.9-C — B5 / B6 operational decisions (2026-10-09).** A decision-and-documentation stage: production code, tests, migrations, dependencies, Celery configuration, and Telegram handlers are unchanged. Outcome: **B6 resolved; B5 resolved as a policy with its numeric value explicitly BLOCKED.** **AD-045** — a research run's deadline is enforced by a **cooperative, in-coroutine deadline** checked at stage boundaries and before each provider call, because Celery raises the soft limit as a signal that, in this repository's synchronous-task-plus-`run_until_complete` shape, is delivered **in the awaiting frame rather than inside the coroutine** — verified by execution against the repository's own async bridge: the in-flight `SqlAlchemyUnitOfWork.__aexit__` never runs, so nothing commits, rolls back, or closes. The cooperative mechanism is what makes "terminate with what it has" (FR-44) executable, and the existing per-stage transaction discipline already supports it. Celery's soft limit becomes a backstop that must be caught **without** reaching the retry path, since the current `getattr(exc, "retryable", True)` default would re-execute the run. Celery configuration is unchanged and chat's limits are untouched. **The deadline value is BLOCKED** — no measurement exists, `SEARCH_API_KEY` is absent from `.env` so no run can execute, and no bound exists on candidates fetched per run, so per-run cost is `SEARCH_MAX_RESULTS × search steps`. **AD-046** — `/research` intake is bounded by the existing `PLAN_MAX_QUESTION_CHARS` (**2000**); no new setting. Verified: aiogram's `Command` filter imposes no cap (a 5000-char argument arrives intact), Telegram's 4096 governs *outgoing* delivery only, and the planner already refuses above 2000 **before any provider call** — so accepting 4000 would create a run, a queue message, and a worker execution before rejecting an input that should never have created state, the opposite of § 15.9. Token budgeting does **not** discriminate between 2000 and 4000 and this is stated rather than dressed up. **AD-047** — research gets a **dedicated rate-limit allowance** (separate window, daily allowance, and in-flight budget), keyed by the same Telegram user id, enforced at intake, separate from chat's; chat's controls are unchanged and a request is counted at intake exactly as chat counts it. **Rate-limit numbers are BLOCKED** — no cost per run is modelled anywhere (`research_runs.prompt_tokens`/`completion_tokens` exist and **no component writes them**), no run has been measured, and **no document states a spend ceiling per user per day**, which is a product decision rather than a technical one. **A defect was found while gathering this evidence and is reported, not fixed:** the global in-flight counter never returns to zero in the API process, because `check()` runs in the API process while `release()` runs in the Celery worker process against its own container — after 8 lifetime messages the API refuses every user permanently with `global_capacity`. This is pre-existing Phase 2 behaviour, invisible to the tests because they drive handler and task in one process, and it is now prerequisite **B6-P4**. Also newly recorded: `SoftTimeLimitExceeded` is handled nowhere in `app/` or `tests/`, and B7 (idempotency) is now a **prerequisite of B5** rather than a parallel concern, because a hard kill redelivers and re-executes the run while producing no partial status and no terminal message. Full evidence: audit § S.

**P3.8 — Synthesis + Citations (2026-10-08).** A run's verified claims now become a cross-source answer whose every citation resolves to persisted evidence of that run. `app/domain/research/synthesis.py` holds the contract: `CitationEntry`/`CitationRegistry` (the registry is an in-memory projection of the existing `citations` table — `citation_id` **is** `citations.id` — scoped by `claims.run_id`, so a foreign citation is absent rather than filtered), `SynthesisInput`, `SynthesisProposal` (closed schema carrying **identifiers only**: no `url`, `quote`, `source`, `confidence`, or `verified` field exists), `SynthesizedAnswer`, `plan_synthesis_context`, and `verify_synthesis`. **The LLM proposes; it never establishes.** `SynthesizedAnswer` is constructible only through `verify_synthesis` (a module-private token guard that also closes `model_validate`), and `from_payload` re-verifies a stored answer against the registry — so a hand-edited `research_runs.synthesis` row cannot be read back as trustworthy. Rules 1–8 of the trust chain are enforced programmatically: existence, run ownership via the persisted relationship, the `citation → chunk → source → run` chain, evidence-bearing evidence, no fabricated URL (URLs come only from the `sources` row, and **no URL is put in the prompt at all**), no fabricated quote (verified under P3.7's unchanged normalization), no arbitrary source id (a real id outside the shown context is refused as *unshown*), and completeness (every section must cite something that resolves). **Any unresolvable reference refuses the whole answer** rather than being stripped — a deliberate narrowing of § 15.7's "strip and mark", recorded as AD-041. Contradictions are preserved, never reconciled: the model may report that two verified claims disagree, both sides keep all their citations, and `claims.status='contradicted'` is deliberately unused because marking a side *is* silent reconciliation. One provider call per run over a rank-ordered, budget-bounded evidence context; the citation order in the answer is the registry's canonical order, so the model's listing order cannot influence what is stored. Phase 2's `LLMProvider` reused unchanged — no second client, no SDK import, no retry; the adapter has no tool surface. **Migration `0004_research_synthesis`** adds one nullable JSONB column (`research_runs.synthesis`); the five-table model is unchanged and no new table exists. **AD-040** (registry authority), **AD-041** (fail-closed verification and guarded construction), and **AD-042** (result persistence and contradiction policy) recorded. 340 tests, including adversarial prompt-injection coverage in English, Persian, and mixed content, and five defect-injection proofs showing the suite detects each removed invariant. **Not verified against a live provider** — the same measurement gap AD-036 and O.17 record. **No stage yet produces `source_chunks` from extracted text**, so the page-to-answer path is still not exercised end to end and is not claimed. Full record: audit § Q.

**P3.7 — Per-source summarization + claim extraction (2026-10-08).** A selected source's persisted passages now become a summary and a set of claims in which **every accepted claim is born with evidence**. `app/domain/research/evidence.py` holds the contract: `EvidenceChunk`/`SourceContent` (one source's passages), `SourceSummary` (FR-31), `ClaimCandidate`/`ClaimExtraction` (unverified model output), `EvidenceReference`/`VerifiedClaim` (accepted, with a passage), `RejectedClaim` + `EvidenceRejection` (why a candidate was refused), `verify_claims()` (the deterministic decision), and `plan_chunk_windows()` (the token-budget policy). `SourceSummarizer` and `ClaimExtractor` are `runtime_checkable` ports with one adapter each in `app/infrastructure/evidence/` (versioned prompts `source-summarization-v1` and `claim-extraction-v1`). **Verification lives in the service, not behind the port**: the extractor can only return candidates, and `SourceEvidenceService` calls `verify_claims`, so an adapter — the part of the stage closest to untrusted text — has no authority to grant evidence. A claim is accepted only when its passage id is among *this source's* passages and its quote is a literal substring of that stored passage text after an explicitly minimal normalization (NFC + whitespace collapse; no case folding, no transliteration, no similarity scoring, nothing that changes meaning). `MIN_QUOTE_CHARS` exists because a substring test alone is satisfied by `"the"`. The passage set supplied for the source is the **only** authority for a reference: a chunk belonging to another source is rejected exactly as one that does not exist, and the database is never consulted to classify a fabricated id. **AD-038 recorded** for the storage of the passage quote on `citations` and the database-level chunk/source ownership constraint. Migration `0003_research_evidence` adds `sources.summary`, a `NOT NULL` `citations.quote`, and a composite foreign key making a cross-source citation uninsertable — **no new table**; the five-table model of AD-034 is unchanged. Retrieval web content is untrusted data: both system-prompt builders take **numbers only**, content and metadata are delimited data in the user role, and the model has no tool surface (asserted from the adapter's import list and constructor signature). 322 tests. **Not verified against a live provider** — the same measurement gap AD-036 and the P3.6 record. Full record: audit § P. Next stage: **P3.8 — Synthesis + citation construction.**

**P3.6 — Research Planning (2026-10-08).** A user question now becomes a validated, persisted `ResearchPlan`. `app/domain/research/planning.py` holds the contract: `ResearchPlan` (objective, steps, language) and `ResearchStep` (id, kind, question, query, expected source types, dependencies, rationale), with a closed `SourceType` set and a two-value `StepKind`. FR-1's three required elements are explicit rather than a JSON blob. Validation rejects a degenerate plan (no objective, no steps, **or no search step**), duplicate ids, unknown and self dependencies, and any cycle — each with a distinct typed error in the new `PlanningError` family. Dependencies are validated with an iterative Kahn's algorithm (no graph library). `PlanningProvider` is a `runtime_checkable` port; one stateless adapter (`LLMPlanner`) and a versioned, fingerprinted prompt (`research-planning-v1`) sit in `app/infrastructure/planning/`. **Phase 2's `LLMProvider` is reused unchanged** — no second provider abstraction, no new client, no SDK import in the planner, and no retry of its own (the existing bounded retry and fallback chain are reused). The plan is persisted on the existing `research_runs.plan` JSONB column with a lossless, re-validating round-trip verified against real PostgreSQL; **no migration**. Limits are `PLAN_MAX_STEPS`, `PLAN_MAX_QUESTION_CHARS`, `PLAN_MAX_TOKENS`, plus P3.2's `MAX_QUERY_CHARS` reused so a plan cannot carry a query search would reject. **AD-026 resolved: Option B** — a dedicated Phase 3 planner supersedes `Supervisor.analyze_query` for planning (`QueryAnalysis` has no objective, steps, search strategy, expected source types, or dependencies, and widening it would change a schema the chat path depends on); `Supervisor` is retained unchanged and chat behaviour is byte-identical. The planner has no tools at all and the question reaches the model only as delimited user-role data. 204 tests. **Not verified against a live provider** — the same measurement gap AD-036 records. Full record: audit § O. Next stage: **P3.7 — Summarization + claim extraction.**

**P3.5 — Deduplication + Ranking (2026-10-07).** Search results and extracted content now collapse into ordered, deduplicated sources. `app/domain/research/dedup.py` holds the whole policy — deterministic URL canonicalization (scheme/host case, default port, fragment, path and trailing slash, percent-encoding, NFC, sorted query) with an **explicit** tracking-parameter set, exact normalized-content fingerprinting (SHA-256 over NFC + casefold + whitespace collapse), union-find grouping over both identities, deterministic representative selection, and a weighted, fully documented score (`0.30·provider + 0.30·title + 0.20·content + 0.10·freshness + 0.10·quality`) with explicit tie-breaking. `SourceDeduplication` and `RankingPolicy` are `runtime_checkable` ports, so the strategy is replaceable (FR-29); one stateless, I/O-free adapter and a `create_ranking_policy` factory live in `app/infrastructure/ranking/`. Config: `DEDUP_GROUP_BY_URL`, `DEDUP_GROUP_BY_CONTENT`, `RANKING_MIN_QUALITY`. 179 tests. No migration, no new dependency, no schema change — dedup/ranking is a processing layer over P3.1's existing `sources` columns. Determinism is guaranteed and tested: no clock (`reference_time` is an input, not `datetime.now()`), no randomness, no network, and input-order independence; Persian, mixed Persian/English, ZWNJ, and Persian punctuation are supported. **Known limitation: content identity is exact only, so an edited syndicated copy is not detected** — recorded, not silently accepted. Full record: audit § N. Next stage: **P3.6 — Planning.**

**P3.2 — Search port + measured provider selection + one adapter + config (2026-10-07).** Search now exists as a capability the domain can call and a provider can satisfy: `app/domain/research/search.py` (provider-independent `SearchRequest`/`SearchResult`/`SearchResponse`), `SearchProvider` in `app/domain/research/ports.py`, a typed `SearchError` family, `SEARCH_*` configuration, and exactly one adapter (`app/infrastructure/search/brave.py`) behind `create_search_provider`. *(This section is the historical P3.2 record and is preserved as written. A second adapter, `tavily.py`, and the selection change to Tavily arrived later — **AD-050**; the port decision recorded here is unchanged.)* **No search is executed by the product yet** — no service, orchestrator, Celery task, `/research` handler, or research LLM call — and nothing fetches a returned URL. 512 tests pass; the adapter suite runs against a mocked transport and makes no real provider call. Provider: **Brave Search API** (AD-036), on the evidence and with the measurement gap recorded in the audit § K. Next implementation stage: **P3.4 — Extraction + Persian Fixtures.**

**P3.10 — Phase 3 closure (2026-10-13).** Three blockers closed. **FR-40 (citation rendering):** `app/services/research/rendering.py` renders a verified `SynthesizedAnswer` into ready-to-send messages — inline `[n]` markers at the end of each section, then a numbered source list carrying the persisted title, domain, URL, and locator. `ResearchOutcome` gained `messages` and `run_research` sends those parts in order. Numbering is per **source** (two passages of one document are one reference); markers are **section-level**, which is the granularity support was verified at; the renderer verifies and invents nothing, so a citation that did not resolve never reaches it. Chunks are packed on line boundaries to `formatting.telegram_chunk_limit(...)`, the same limit `DeliveryService` applies, so a marker and its entry are never torn apart, nothing is duplicated, and nothing is truncated; all dynamic values are HTML-escaped because the bot's default parse mode is HTML and a page title is untrusted content (SR-3). **FR-9/SR-5/T-25 (token accounting):** `app/services/research/usage.py` adds `RunTokenLedger` and `UsageRecordingLLM`, a pass-through that records only provider-reported usage; the composition root wraps the shared provider for the four research adapters only, so no adapter changed and chat's own accounting is untouched. The orchestrator opens one ledger scope per run and the totals are written through `set_status`'s existing parameters — with the answer on success, on the terminal failure write when there is no answer. Nothing is estimated, and double counting is prevented structurally because `set_status` sets rather than increments while the lifecycle refuses a terminal rewrite. **AD-050 (provider):** `SEARCH_PROVIDER` defaults to `tavily` on a budget rationale — a credential now exists, discharging audit § K.10's open item, and Tavily met the required functionality within this project's free-tier allowance where Brave was not a viable free option. No comparative benchmark was run, so quality and latency stay **NOT VERIFIED** and no claim is made that Tavily is better or free indefinitely; the port decision is unchanged, so both adapters stay registered. **Measured `2221 passed, 0 failed`**, `ruff` clean, `mypy app` clean (96 files), `alembic check` clean. Full record: audit § V, PHASE-03 § 24.

---

## Phase 4 — File Intelligence

**Purpose:** Add document-based research capability.

**Full specification:** [`phases/PHASE-04-FILE-INTELLIGENCE.md`](./phases/PHASE-04-FILE-INTELLIGENCE.md)

**Not implemented today.** The repository contains no upload handling, no download logic, no parsing, and no file storage of any kind.

### Intended scope

Document ingestion for PDF, DOCX, TXT, and Markdown, plus URL-sourced documents where appropriate.

### Expected capabilities

File ingestion; text extraction; document chunking; document Q&A; summarization; information extraction; document comparison; and citations or page references where the source format provides them.

### Retrieval approach is deferred

Embeddings and vector retrieval are described as an option to be *evaluated* during Phase 4, not a committed decision. The concrete embedding provider and any vector store are to be selected at that time based on measured retrieval quality, cost, and operational complexity. See Architecture Decision Log entry `AD-007`.

---

## Phase 5 — Research Workspace

**Purpose:** Provide a persistent research environment.

**Full specification:** [`phases/PHASE-05-RESEARCH-WORKSPACE.md`](./phases/PHASE-05-RESEARCH-WORKSPACE.md)

**Not implemented today.** There is no database code in the repository, so none of the entities below exist.

### Intended scope

Research projects; saved research; conversation history; source history; bookmarks and favorites; tags; research sessions; project organization; and reusable research context.

### Prerequisite

This phase depends entirely on the data layer introduced in Phase 2. If the schema is not designed with workspaces in mind during Phase 2, this phase will require retroactive migration.

---

## Phase 6 — Agent System

**Purpose:** Introduce a modular multi-agent research architecture.

**Full specification:** [`phases/PHASE-06-AGENT-SYSTEM.md`](./phases/PHASE-06-AGENT-SYSTEM.md)

### Current

`Supervisor` exists only as a minimal LLM interaction. One class, one working method, one dead method, no orchestration, no tool registry, no state, no agent loop. The directory `app/agents/` and the class name anticipate an architecture that does not exist.

### Future

A modular research-agent orchestration system. Candidate roles, subject to refinement during the phase:

- Planner Agent
- Search Agent
- Reader / Extraction Agent
- Source Evaluation Agent
- Fact Checking Agent
- Summarization Agent
- Report Generator Agent

This role list is illustrative intent, not a committed design. The concrete decomposition should be driven by the observed needs of the Phase 3 pipeline, not by the list above.

---

## Phase 7 — Premium Research Features

**Purpose:** Deliver differentiated, higher-value research capabilities.

**Full specification:** [`phases/PHASE-07-PREMIUM.md`](./phases/PHASE-07-PREMIUM.md)

**All items below are future targets. None are current features.**

Possible capabilities: Deep Research mode; long-running research jobs; report generation; PDF export; Markdown export; citation styles; charts and data visualization where appropriate; multilingual research; voice input/output if justified; and advanced research controls.

Long-running jobs in this phase have a direct dependency on the Celery reliability work specified in Phase 1. Building long jobs on an unreliable queue would compound the existing defects.

---

## Phase 8 — SaaS / Web Platform

**Purpose:** Extend the product beyond Telegram into a web platform.

**Full specification:** [`phases/PHASE-08-SAAS.md`](./phases/PHASE-08-SAAS.md)

**Nothing in this phase is implemented.** The repository has no authentication, no user accounts, no multi-tenancy, no REST API beyond a single static health route, and no frontend.

### Intended scope

A Next.js dashboard; authentication; user accounts; a research workspace UI; research history; API access; usage limits; billing integration; team and workspace capabilities; a proper REST/API layer; and integrations such as Discord or Slack where appropriate.

### Deferred decisions

The frontend framework beyond the stated Next.js intent, hosting provider, cloud provider, and deployment topology are **not** decided by this document and must be selected during Phase 8. See Architecture Decision Log entry `AD-008`.

---

## Phase 9 — Monetization

**Purpose:** Establish the business model.

**Full specification:** [`phases/PHASE-09-MONETIZATION.md`](./phases/PHASE-09-MONETIZATION.md)

**Nothing in this phase is implemented.**

### Possible business model

A Free tier; a Pro tier; a Team tier; usage limits; research limits; API usage metering; premium reports; and advanced research features.

### No pricing claims

**This document makes no pricing claims and proposes no price points.** Tiers are described structurally only. Pricing, packaging, and entitlement values require market validation and are out of scope for engineering planning. The payment provider is not selected and must be chosen during Phase 9. See Architecture Decision Log entry `AD-009`.

---

## Cross-Phase Principles

These apply to every phase and are not repeated in each document.

1. **Documentation is part of the deliverable.** Code without updated documentation is incomplete work.
2. **Tests gate completion, not just confidence.** A phase is not done because the feature works once; it is done because the failure modes are covered.
3. **Secrets never enter the repository.** No exceptions, no temporary measures, no commented-out placeholders containing real values.
4. **CURRENT and PLANNED are never blurred.** Documentation must state which is which for every capability claim.
5. **Unverified claims are labeled.** If evidence is insufficient, write "NOT VERIFIED — insufficient evidence in repository" rather than guessing.
6. **Provider and vendor choices are reversible.** External services sit behind abstractions so they can be replaced without rewriting the domain.

---

## Architecture Decision Log

Initially populated only with decisions explicitly supported by the repository or the audit. No invented decisions.

| ID | Decision | Reason | Phase | Status |
|---|---|---|---|---|
| AD-001 | Two-process runtime: a FastAPI + aiogram process and a separate Celery worker process, joined by a Redis broker | This is the structure that exists in the repository (`app/main.py`, `app/core/celery_app.py`, `app/tasks/research_task.py`) | 1 | Accepted (existing) |
| AD-002 | Telegram long polling instead of a webhook | This is what is implemented (`app/main.py:22`, `dp.start_polling(bot)`). No webhook endpoint exists | 1 | Accepted (existing) |
| AD-003 | An OpenAI-compatible SDK against a DeepSeek-compatible endpoint as the current LLM integration | This is what is implemented (`app/agents/supervisor.py:7-10`). Recorded as current state, **not** as a long-term recommendation | 1 | **Superseded by AD-028** |
| AD-004 | Redis is the Celery broker **and** result backend | This is the current configuration (`app/core/celery_app.py:6-7`). Flagged by audit M-9 as misconfigured — results are never read and keys accumulate unbounded. Listed here as a record of current state, with remediation required in Phase 1 | 1 | Needs Revision (Phase 1) |
| AD-005 | PostgreSQL is the intended persistence technology | Declared via `docker-compose.yml:10-20`, `DATABASE_URL` in `app/core/config.py:6`, and `asyncpg`/`sqlalchemy` in `requirements.txt:6-7`. The audit confirms the technology choice but confirms **zero** implementation | 2 | Accepted (intent) / Not Implemented |
| AD-006 | Web search will be integrated behind a provider abstraction; the concrete provider is undecided | No search provider exists in the repository. Selecting one prematurely would lock in cost and availability assumptions without requirements | 3 | **Superseded by AD-036** |
| AD-007 | Embeddings and vector retrieval are an option to be evaluated in Phase 4, not a committed decision | No embedding provider or vector store is referenced anywhere in the repository or audit | 4 | Open |
| AD-008 | Web platform, hosting, and cloud provider are undecided | Nothing in the repository specifies a deployment target. Next.js appears only as stated product intent | 8 | Open |
| AD-009 | Payment provider is undecided | No payment integration exists and no pricing has been validated | 9 | Open |
| AD-010 | One long-lived event loop per process, owned by `app/infrastructure/asyncio_runtime.py` and driven by Celery's `worker_process_init` / `worker_process_shutdown` signals | Audit C-3: a module-level `Bot` caches an HTTP session bound to whichever loop created it, and the old task created and closed a loop per invocation, so only the first task per worker could work | 1 | Accepted (implemented) |
| AD-011 | aiogram's `start_polling` runs with `handle_signals=False`; the ASGI server owns process signals and the lifespan owns cleanup | Found during implementation (F-3): aiogram's own SIGTERM handler stops uvicorn's loop, so the process hung on shutdown and the lifespan never completed | 1 | Accepted (implemented) |
| AD-012 | Rate limiting is in-process (sliding window plus a global in-flight cap), not Redis-backed | Phase 1 runs a single API process, so an in-memory counter avoids a Redis round-trip on the hot path. Recorded as a limitation: the counters stop being global if the API tier scales horizontally | 1 | Accepted (implemented) — revisit before horizontal scaling |
| AD-013 | Test dependencies are split into `requirements-dev.txt` | Keeps runtime installs minimal while making the harness explicit. Pinned versions must match `requirements.txt` transitively | 1 | Accepted (implemented) |
| AD-014 | Non-retryable provider failures are classified by HTTP status: only 408, 409, and 429 are retried | Found during implementation (F-5): a provider 402 "Insufficient Balance" was retried three times per task, contradicting FR-4.4 | 1 | Accepted (implemented) |
| AD-015 | `conversations` carries a nullable, unconstrained `project_id` reserved for the Phase 5 project association, and messages reference conversations only | Phase 2 § 6 calls a chat-only schema the single most likely source of future rework. Keying messages to a conversation rather than to a user-and-conversation pair means the Phase 5 association is one additive column. The column is deliberately **not** a foreign key: the `projects` table does not exist, and a dangling reference would be implementing Phase 5 | 2 | Accepted (implemented) |
| AD-016 | The active conversation is derived by query ("the user's most recently updated active conversation"), not stored as a pointer on the user row | A stored pointer would have to be rewritten the moment Phase 5 groups conversations into projects, and would need its own consistency rules. A derived value is always correct and costs one indexed query | 2 | Accepted (implemented) |
| AD-017 | Idempotency is keyed on a `turn_id` **derived** from the platform identifiers (`sha256(chat_id:message_id)`), with `UNIQUE (turn_id, role)` and `UNIQUE (telegram_chat_id, telegram_message_id)` as the authority | Telegram redelivers updates and Celery redelivers tasks. A derived key means a retry recomputes the same value and collides in the database, so no coordination is needed between workers. A random key would make the constraint useless | 2 | Accepted (implemented) |
| AD-018 | A chat turn uses **two** transactions: the user message commits first, the assistant reply second | Phase 2 § 15 requires that a user message must not disappear silently. One transaction would roll the user's text back when the provider fails. The cost is that a half-turn is a legitimate intermediate state, which the retry path is written to expect | 2 | Accepted (implemented) |
| AD-019 | A uniqueness violation aborts the enclosing unit of work rather than being absorbed by a SAVEPOINT | Measured, not assumed: with SQLAlchemy 2.0.36 + asyncpg, rolling a savepoint back while its parent transaction has issued no SQL still leaves the `Session` in a pending-rollback state, and the caller's commit then fails. Letting the violation propagate keeps the contract simple and the caller in control | 2 | Accepted (implemented) |
| AD-020 | Alembic over the already-declared SQLAlchemy, driven by `DATABASE_URL` with an `ALEMBIC_DATABASE_URL` override | Phase 2 § 6 requires a migration tool and names the existing declaration as the natural target. Alembic adds no second ORM or DDL framework. The URL lives in settings so there is one place to change it and no place one can be committed | 2 | Accepted (implemented) |
| AD-021 | Test database isolation is enforced by name: the suite refuses any database whose name does not end in `_test`, and migrations additionally require `ALEMBIC_REQUIRE_TEST_DB=1` | Phase 2 § 23. The check is an explicit switch rather than an inference from the URL, because inferring it would mean a *misspelled* test database silently migrates a real one | 2 | Accepted (implemented) |
| AD-022 | Conversation context is a recent-window truncation: newest `LLM_CONTEXT_MAX_MESSAGES`, then drop oldest until the estimated total fits `LLM_CONTEXT_MAX_TOKENS`. No summarization, no embeddings | Phase 2 § 11 requires a bounded, deterministic strategy. Summarization would add a second model call per turn and a cache to invalidate; embeddings and a vector store are Phase 4 (AD-007). Both limits are configuration (FR-9) | 2 | Accepted (implemented) |
| AD-023 | The system prompt is a versioned constant with a checksum, and the builder that returns it takes no user content | Phase 2 § 6 requires prompt templates to be versioned or checksummed so a change is detectable. Having no parameter through which user content could reach the prompt makes SR-3 structural rather than a review rule | 2 | Accepted (implemented) |
| AD-024 | The model fallback chain is bounded twice: by the number of configured models and by `LLM_FALLBACK_MAX_ATTEMPTS`; only retryable failures advance it | Phase 2 FR-32 and FR-33. Advancing on a non-retryable failure cannot succeed and multiplies cost, which is FR-34 | 2 | Accepted (implemented) |
| AD-025 | The daily request allowance lives in the existing in-process `RateLimiter` rather than in a second limiter | Phase 2 FR-28 needs a daily allowance; two limiters would mean two sets of counters for one decision. It inherits AD-012's in-process limitation, which is recorded rather than hidden | 2 | Accepted (implemented) — revisit with AD-012 |
| AD-026 | `app/services/research.py` was removed and `app/services/chat.py` took its place, keeping the delivery-neutral contract (`execute` / `notify_failure` / `run`) and the typed error-to-message mapping in `app/services/failures.py` | `DEVELOPMENT_RULES.md` § 4 requires dead code to be wired in or removed. Leaving both would have meant two services racing to own the request path. `app/agents/supervisor.py` and `analyze_query` are **kept** because Phase 3 needs the decomposition capability | 2 | Accepted (implemented) |
| AD-028 | The OpenAI-compatible endpoint is Groq (`https://api.groq.com/openai/v1`) with model `qwen/qwen3.8-27b`; `LLM_PROVIDER=openai_compatible` remains the selector, so no provider is locked in | The endpoint is configuration, and the endpoint changed. `llama-3.3-70b-versatile` was retired by the provider, which surfaced as a uniform `403` from an egress proxy and was initially misdiagnosed as a credential failure; from a container the key authenticates (200 on `/models`) and the real error is `404 model_not_found`. `qwen/qwen3.8-27b` was chosen after measuring the available models: the `openai/gpt-oss-*` candidates return HTTP 200 with **empty content** and `finish_reason=length` because they spend the output budget on reasoning tokens. The class is unchanged, so swapping endpoints or vendors is still a settings change | 2 | Accepted (implemented) — supersedes AD-003 |
| AD-027 | The Celery task keeps its Phase 1 name `app.tasks.research_task.process_research` although it now runs the chat flow | `DEVELOPMENT_RULES.md` § 7 forbids renaming an existing application file without an explicit instruction, and a Phase 1 structural test asserts on the literal import. The name is inherited technical debt and is recorded as such | 2 | Accepted (implemented) — rename when authorised |
| AD-029 | `/research <question>` is the Phase 3 research trigger | The Phase 3 specification defines the pipeline but not the Telegram invocation. An explicit command is the smallest, unambiguous, testable surface and matches the existing command architecture; it avoids misrouting chat messages into an expensive pipeline and keeps SR-2 (users supply a question, never a URL) enforceable | 3 | Accepted (contract frozen at P3.0; not yet implemented) |
| AD-030 | The research pipeline is frozen as 22 externally meaningful responsibilities (interpretation → decomposition → search → normalize → validate → fetch → extract → dedup → rank → chunk → evidence → claims → cross-check → cite → synthesize → report → deliver → persist); internal module boundaries may differ | Freezing responsibilities rather than modules lets P3.1–P3.9 choose internal structure without changing the contract, while guaranteeing every spec requirement has a home | 3 | Accepted (contract frozen at P3.0) |
| AD-031 | The SSRF fetch boundary is a security contract: resolve DNS and validate every address before connecting, connect using the validated resolution (closing the rebinding window), revalidate at every redirect hop, allow only HTTP(S), and enforce timeout, size, streaming, decompression, and redirect limits | Fetching untrusted URLs is the phase's highest-risk component (spec § 6, SR-1/SR-6). Frozen at P3.0 so no implementation stage can weaken it unknowingly; implementation at P3.3 | 3 | Accepted (contract frozen at P3.0) |
| AD-032 | Prompt-injection containment: the research model receives no tool surface; fetched content enters prompts only inside delimited regions marked as untrusted data; model outputs are validated structurally before reuse; instructions never originate from fetched content | The spec (SR-3, SR-4) makes untrusted web content the phase's second major risk. Removing any tool/command surface closes the injection-steers-follow-up loop by construction rather than by prompt wording alone | 3 | Accepted (contract frozen at P3.0) |
| AD-033 | Citation integrity is a programmatic trust chain — source → source_chunk → claim → citation — validated against persisted rows, never model self-report | Spec SR-9 makes citation integrity a security property. A citation to content not retrieved in the run must be constructively impossible (FK registry), and claim-to-passage support is verified programmatically | 3 | Accepted (contract frozen at P3.0) |
| AD-034 | Phase 3 persistence is exactly five tables — `research_runs`, `sources`, `source_chunks`, `claims`, `citations`; `ResearchQuery`, `SearchResult`, `FetchResult`, `ResearchReport` are transient domain concepts, not tables; the `/delete_account` cascade must extend to research artifacts; Phase 5 workspace tables are excluded | Mirrors the spec § 6 data model. Four candidate entities collapse into run/source state, avoiding joins with no retrieval path. The deletion cascade is a hard requirement because SR-9/SR-10 break silently without it | 3 | Accepted (contract frozen at P3.0; schema implemented at P3.1) |
| AD-035 | The 120-second Celery task limit is recorded as a Phase 3 execution risk; the research deadline/cancellation model is assigned to the execution stage (P3.9), not changed at P3.0 | The current limits are sized for one chat LLM call, not a multi-stage research run (FR-44 requires terminating with partial state). Changing worker limits at P3.0 would be an unmeasured configuration change | 3 | Accepted (recorded risk; resolution due at P3.9) |
| AD-036 | **Web search provider: Brave Search API**, behind the `SearchProvider` port, selected by `SEARCH_PROVIDER` through `create_search_provider`. Exactly one adapter is implemented; no fallback and no provider switching | Closed AD-006 with evidence: eight candidates evaluated; live reachability/error probes against every candidate's real endpoint (Brave was the only first-party commercial API that answered from the deployment environment with its own structured error); the published API contracts, pricing and retirement records; and architectural fit (title/url/description normalize one-to-one onto `SearchResult`, no vendor SDK needed, an independent index rather than a Google-scraping proxy). Bing was eliminated on a documented retirement (2025-08-11); the keyless HTML candidates were eliminated by measurement (anti-bot challenge, zero results). **Result quality was NOT measured — no usable search credential existed — so the decision rests on contract, reachability, cost and fit, and a credentialed benchmark is the first outstanding evidence item.** Evidence: Phase 3 audit § K | 3 | **Superseded by AD-050** (the provider decision stands; the *selection* changed to Tavily). Implemented at P3.2 |

| AD-037 | **Phase 3 research planning supersedes `Supervisor.analyze_query`; `Supervisor` is retained unchanged.** Planning is a dedicated `PlanningProvider` adapter over Phase 2's existing `LLMProvider`, with its own versioned prompt and its own `ResearchPlan` contract | AD-026 retained `Supervisor.analyze_query` because "Phase 3 needs the decomposition capability". Investigated at P3.6, the need is only *partial*: `QueryAnalysis` holds `summary` and `sub_questions` and has no objective, no steps, no search strategy, no expected source types, and no dependency — the three things FR-1 actually names. Widening a Phase 2 schema the **chat path also depends on**, to serve a Phase 3 requirement, would couple two unrelated paths; its validators are also deliberately permissive (coercing `None` to an empty result), which is right for a chat decomposition and wrong for a plan FR-3 requires to fail clearly. Note `analyze_query` executes nothing, so this decision is about artifact shape and isolation, not about a capability boundary. `Supervisor` and its schema are untouched: chat behaviour is byte-identical and asserted, and `analyze_query` keeps its Phase 1 callers. Evidence: Phase 3 audit § O.10 | 3 | Accepted (implemented at P3.6) |

| AD-038 | **The verified passage is persisted on `citations.quote` (`NOT NULL`), and chunk/source ownership is a composite foreign key.** `citations (chunk_id, source_id) → source_chunks (id, source_id)`; the frozen five-table model is unchanged and no table was added | § 15.7 requires claim-to-passage support to be verifiable against *stored* content, and P3.1's schema had nowhere to keep the passage — `citations.location` is a 255-character human-renderable *locator*, a different thing, and `claims` correctly stays free of evidence fields because AD-033 places the claim→passage link on the citation. The composite FK closes a gap P3.1 left open: with two independent foreign keys the database would have accepted a citation naming one source's `source_id` and another source's `chunk_id`, which is exactly the cross-source evidence link the stage contract forbids. **The textual containment rule (`quote ∈ chunk.text`) is not in this AD because it cannot be expressed in SQL** — it compares one row's text against another's — and is enforced in the application layer immediately before persistence and covered by tests. Evidence: Phase 3 audit § P.2, § P.10 | 3 | Accepted (implemented at P3.7) |

| AD-039 | **Evidence verification is performed by the application layer, not behind the `ClaimExtractor` port, and a passage reference is resolved only against the passage set supplied for the current source** — never against the database | Two reasons. (1) If verification lived inside an adapter, the ability to *accept* a claim would sit in the component closest to untrusted retrieved text, and a replaced or mocked adapter could change what counts as evidence; the port returns `ClaimCandidate` values only, so accepting is not in its signature. (2) Resolving a fabricated id "because it exists" is precisely the behaviour that lets a claim cite evidence from outside the source it claims to describe, and classifying the difference would cost a database round-trip per fabricated id. A cross-source reference is therefore rejected exactly as a nonexistent one is. Cost: the verifier trusts the ids it is given, so the service builds them from a repository query scoped by `source_id` and re-checks ownership — recorded, not assumed. Evidence: Phase 3 audit § P.3, § P.4 | 3 | Accepted (implemented at P3.7) |

| AD-040 | **The citation registry is an in-memory projection of the existing `citations` table, scoped by `claims.run_id`, and it is the *only* authority for what a citation means.** `citation_id` **is** `citations.id`; a `CitationRegistry` is built from one join across `citations → claims → source_chunks → sources`, and it refuses to construct an entry whose quote is not inside its stored passage or whose passage belongs to a different source than the citation names | A second citation table, or a separate id space, would create two identities for one relationship — exactly the ambiguity AD-033 exists to remove. Scoping the *read* by `claims.run_id` means a citation of another run is **absent** rather than filtered, so run ownership (Rule 2) is structural and no later check can forget it. Re-checking containment and the source/chunk link at the registry boundary is defence in depth: neither rule is expressible in SQL (containment compares two rows' text; the link is already a composite FK), so the domain re-derives both rather than assuming the write-time guarantee still holds. Evidence: Phase 3 audit § Q.2, § Q.10 | 3 | Accepted (implemented at P3.8) |

| AD-041 | **The model proposes citation *identifiers*; `verify_synthesis` is the only path to a trusted answer, and any unresolvable reference refuses the whole answer rather than being stripped.** `SynthesizedAnswer` and the three trusted value types are constructible only through that verifier (a module-private token guard), and `from_payload` re-verifies a stored answer against the registry | Three reasons. (1) **The alternative makes the guarantee weaker.** § 15.7's "strip the unresolvable marker and mark the affected claim" is right when markers are a rendering artefact; a *fabricated* citation is an attack signal, and delivering the rest of a proposal that tried to deceive presents it as verified research (SR-9). Every refusal is still reported — the typed error carries the full ordered list of findings, so counts are available (FR-42) — but nothing is delivered and the run is not marked successful. (2) **Guarding construction makes the invariant structural**, not a convention a later caller could route around with `SynthesizedAnswer(...)` or `model_validate(...)`. (3) **Re-verification on read-back is the only defence for content the database cannot constrain**: no SQL can compare a stored quote against a passage, or check a stored URL against its source row, so a hand-edited `research_runs.synthesis` row is refused when read rather than trusted. Evidence: Phase 3 audit § Q.3, § Q.4, § Q.5, § Q.10 | 3 | Accepted (implemented at P3.8) |

| AD-042 | **The verified answer is persisted as one nullable JSONB column on `research_runs`, written with `final_answer` and `status='completed'` in a single transaction; contradictions are recorded as a symmetric relation over real claim ids and `claims.status='contradicted'` is deliberately left unused** | Why a column: a `research_results` table would duplicate the run's identity, ownership, status, and lifecycle — all of which already exist — while adding a join to reach the row AD-034 was written to avoid. § 15.8 explicitly allows transient research concepts to live on the run, and P3.6's `plan` JSONB set the precedent. Why not `final_answer` alone: a text blob cannot answer "which citation backs this section", which is what § 15.7 requires to be verifiable. Why `contradicted` stays unused: marking a losing side is silent reconciliation, which FR-34 forbids; both claims remain `supported` — true, their quotes are in their passages — and the conflict is reported beside them with **both** sides' citations. Why `completed`: this stage cannot know whether earlier stages partially failed, and deciding `partial` here would mean guessing from a stage that did not run; P3.9 owns that. Evidence: Phase 3 audit § Q.7, § Q.10 | 3 | Accepted (implemented at P3.8) |

| AD-043 | **P3.9 research execution is one Celery task per run, with every stage inside it, on the worker's long-lived event loop — not chained stage tasks** | Decided from repository evidence, not preference. Option B (chained stage tasks) is currently unavailable: `task_ignore_result=True` with nothing reading results, no queue/routing design, and no schema column holding per-stage state; it would also turn FR-44's "terminate with what it has" into a distributed-transaction problem the repository has no machinery for. Option C collapses into Option A once the async bridge (`app/infrastructure/asyncio_runtime.py`, AD-010) is accounted for, and every P3.6/P3.7/P3.8 service already closes its read transaction before a provider call — the invariant a single in-task pipeline preserves. Resolves the *shape* of AD-035; the *deadline value* stays open as prerequisite B5. Evidence: Phase 3 audit § R.11 | 3 | Accepted (implemented at P3.9-D) |

| AD-044 | **P3.9 `completed` / `partial` / `failed` is decided by the orchestrator from recorded degradation: `partial` when a verified answer exists under any recorded degradation, `failed` when none does, `completed` only when nothing degraded and no evidence was dropped** | Extends AD-042, which correctly refused to decide `partial` at P3.8 because synthesis sees citations, not fetch outcomes. The orchestrator is the first component that sees both, and the counters it needs already exist — `SynthesisOutcome.evidence_dropped` / `evidence_available`, `DedupRankingResult.excluded_results`, `SourceStatus` + `sources.failure_reason` — so degradation can be recorded in `research_runs.failure_summary` without a new column. A distinct `cancelled` state for FR-44 would be a schema change and is **not** assumed. Evidence: Phase 3 audit § R.10 | 3 | Accepted (implemented at P3.9-D) |

| AD-045 | **A research run's deadline is enforced by a cooperative, in-coroutine deadline in the orchestrator, checked at stage boundaries and before each provider call. Celery's soft limit is a backstop, not the mechanism, and `SoftTimeLimitExceeded` must be caught without reaching the retry path. Celery configuration is unchanged and chat's limits are untouched** | Resolves AD-035's *deadline model*, which AD-043 left open after fixing the *shape*. The deciding evidence is a measured one: Celery raises the soft limit as a POSIX signal handled in the worker process (`billiard/pool.py:227-228`), and because this repository's task body is synchronous and blocks in `loop.run_until_complete()` (`app/infrastructure/asyncio_runtime.py:70-82`), the exception is raised **in the awaiting frame, not inside the coroutine**. Verified by execution against the repository's own async bridge: the in-flight `SqlAlchemyUnitOfWork.__aexit__` (`app/db/unit_of_work.py:54-72`) **never runs**, so there is no commit, no rollback, and no session close, and the coroutine is left `PENDING`; the control case, `asyncio.timeout` — already used by the fetch stage — unwinds correctly. A signal-based deadline therefore cannot persist partial results at all, which is what FR-44 and § 15.11 require. Why the cooperative form is also the *sufficient* one: every research service already closes its read transaction before any provider call and commits its writes separately (`planning.py:79-80`, `chunking.py:194-211`, `evidence.py:257-269`, `synthesis.py:260-264`), so a deadline checked between stages lands on a committed boundary — the property chat cannot offer, since its turn is one transaction. Why the retry path must be excluded: `process_research:92` defaults any exception to `getattr(exc, "retryable", True)`, so an uncaught soft timeout would re-execute the whole run and defeat the deadline. Why no hard-deadline value was chosen: none can be measured here — no run has executed, `SEARCH_API_KEY` is absent from `.env` so `search_enabled` is `False`, and no bound exists on candidates fetched per run, so per-run cost is `SEARCH_MAX_RESULTS × search steps` and any figure would be invented (`DEVELOPMENT_RULES.md` § 6). The value is BLOCKED pending that measurement. **The numeric deadline is not part of this decision.** Evidence: Phase 3 audit § S.2, § S.3, § S.7 | 3 | Accepted (mechanism implemented at P3.9-D; numeric value still BLOCKED pending measurement) |

| AD-046 | **`/research` intake is bounded by the existing `PLAN_MAX_QUESTION_CHARS` (default 2000). No new research-specific limit is introduced** | Both candidates were verified in code rather than assumed: `CHAT_MAX_MESSAGE_CHARS = 4000` (`config.py:87`) governs chat turns only and is enforced at `services/chat.py:140-145`, while `PLAN_MAX_QUESTION_CHARS = 2000` (`config.py:241`) is enforced at `infrastructure/planning/planner.py:112-119`. Four checks, all pointing the same way. (1) **Command parsing imposes no cap of its own**: aiogram splits with `text.split(maxsplit=1)` and passes the whole tail as `args` — verified by execution, a 5000-character argument arrives intact — so the framework neither supports nor opposes either value and the cap must be ours. (2) **Telegram's 4096 is not the binding constraint**: `formatting.TELEGRAM_HARD_LIMIT` and `TELEGRAM_MAX_MESSAGE_LENGTH` govern *outgoing* delivery (`delivery.py:42-43`), and nothing in the repository rejects an inbound message by length. (3) **The planner already refuses above 2000 before any provider call**, so accepting 4000 at intake would create a run row, a queue message, and a worker execution before rejecting an input that could have been refused with no state change — the opposite of § 15.9's "Invalid research request → rejected at intake, **no run created**". (4) **Token budgeting does not discriminate, and this is recorded rather than dressed up**: measured with the repository's own `CharRatioTokenEstimator(4.0)`, a 2000-character question makes the planning prompt ≈1029 estimated tokens and a 4000-character one ≈1529 — both small, and `PLAN_MAX_TOKENS = 1500` is an *output* bound, not an input bound. The decision rests on intake consistency, not on a budget claim. A third number was rejected outright: it would be an arbitrary value with no evidence behind it, and reusing the existing setting means one number governs the question end to end. Evidence: Phase 3 audit § S.5 | 3 | Accepted (implemented at P3.9-D) |

| AD-047 | **Research gets a dedicated rate-limit allowance — a separate sliding window, daily allowance, and in-flight budget — keyed by the same Telegram user id, enforced at intake, separate from and additional to chat's existing controls. Requests are counted at intake exactly as chat counts them. The numeric values are BLOCKED pending measurement** | § 15.1 permits a stricter research allowance "because it is far more expensive", and FR-9/SR-5 require per-run and per-user cost limits the repository does not enforce. Identity is **per Telegram user id, not per chat** — `handlers.py:212` passes `from_user.id`, and `RateLimiter` keys its buckets by it — which also matches § 15.1's ownership rule and SR-10. Counting semantics are inherited deliberately rather than reinvented: `check()` appends to the window and daily buckets at admission (`services/rate_limit.py:83-85`) before the ack is sent, and a later failure does not refund; giving research different semantics would make two surfaces disagree for no evidential gain. A **separate in-flight budget** is required because a research run occupies a worker for minutes while chat's occupies it for one LLM call, and `docker-compose.yml` runs `--concurrency=1` with `worker_prefetch_multiplier=1` on one shared `research` queue. The refusal message reuses the existing tested convention (`RATE_LIMITED_TEXT` with `retry_after_seconds`, `GLOBAL_CAPACITY_TEXT`) — no new user-facing copy. Backend availability is **not applicable**: the limiter is in-process `dict` + `threading.Lock`, imports no Redis, and its real property is fail-open on restart, which is recorded as a known limitation rather than solved. **The numbers are BLOCKED, specifically**: no cost per research run is modelled anywhere — `research_runs.prompt_tokens`/`completion_tokens` exist and `set_status` accepts them, but no component writes them; no run has been measured; the per-run provider call count is bounded only by configuration nobody has fixed in production; and no document states a spend ceiling per user per day, which is a product decision, not a technical one. P3.0's § E.5 deferred exactly this and recorded no value. Evidence: Phase 3 audit § S.6 | 3 | Accepted (implemented at P3.9-D as `RateLimiter.check_share`; numeric values still BLOCKED pending measurement) |

| AD-050 | **The selected web search provider is Tavily Search API.** `SEARCH_PROVIDER` defaults to `tavily`; the `BraveSearchAdapter` remains registered and selectable. **Supersedes the AD-036 selection, not the AD-036 decision to put search behind the `SearchProvider` port** | **The rationale is budget, and nothing more is claimed.** AD-036's selection assumed cost was comparable and that result quality could be measured once a credential existed. A credential now exists, which discharged audit § K.10 item 3 ("re-test Tavily's credential path… If it works there, Tavily is re-evaluated against Brave on the same criteria") — and on that re-evaluation Tavily met the required search functionality within this project's free-tier allowance while **Brave Search was not a viable free option at this project's usage**. That is the whole reason. This decision does **not** claim Tavily is better on quality, latency, or any other axis: no comparative benchmark was run, so those remain **NOT VERIFIED** exactly as AD-036 left them. It also does not claim Tavily is free indefinitely — provider pricing is the vendor's published figure and can change, which is the operational reason a re-evaluation is cheap rather than a rewrite. **Replaceability is unchanged and is what makes this reversible:** re-selecting is a `SEARCH_PROVIDER` value plus, at most, one branch in `create_search_provider`; no change to `app/domain/research/`, the persistence layer, or any stage. Evidence: Phase 3 audit § K.6 (Tavily's earlier loss was an *edge block on the credential from this egress path*, explicitly recorded there as a deployment-environment finding rather than a quality verdict) | 3 | Accepted (implemented; `SEARCH_PROVIDER` default and an explicit Brave override are both asserted in `tests/test_research_search_adapter.py::TestTheSelectedProvider`) |

### How to use this log

- Add a row when a decision is made, not when it is implemented.
- Never delete a row. Supersede it by adding a new row that references the old ID and marking the old one `Superseded`.
- A decision supported only by preference must record the reason for the preference, not an invented justification.

---

## Related Documents

| Document | Purpose |
|---|---|
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Current Architecture & Audit Reference — the source of truth for what exists |
| [`DEVELOPMENT_RULES.md`](./DEVELOPMENT_RULES.md) | Mandatory rules for any Agent or developer working on this repository |
| [`../.agents/skills/research-bot/SKILL.md`](../.agents/skills/research-bot/SKILL.md) | Operational skill briefing for coding Agents |
| [`phases/`](./phases/) | Per-phase specifications |
