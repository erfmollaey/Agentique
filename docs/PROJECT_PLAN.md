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

**Current Phase:** Phase 2 — AI Chat MVP
**Status:** COMPLETE, with environmental verification limitations. Telegram delivery, GitHub Actions execution, and the verbatim `docker build` could not be exercised here. **An open human action remains outside this phase: Phase 1 credential rotation (P0-3/SR-4) — the key in `.env` is confirmed live, so the disclosure is real.**

**Current implementation maturity:**

| Area | Status | Change |
|---|---|---|
| Telegram integration | Implemented | Text, `/start`, `/help`, `/new`, `/conversations`, `/reset`; long-polling transport unchanged; replies target the originating chat |
| FastAPI backend | Partial | Lifespan lifecycle, `/health`, `/ready` now probing the broker **and** the database; no API surface beyond health |
| Celery | Partial | Time limits, acks-late, bounded retry, result retention; single-queue, no routing; the task now runs the chat service |
| Redis | Partial | Broker healthy and probed by `/ready`; no authentication |
| PostgreSQL | **Implemented** | Was "Declared but not implemented". Three tables, Alembic migrations, constraints, indexes; `/ready` probes it |
| LLM integration | Implemented | Credentials, endpoint, model, timeout, token limit, validation, usage capture, and a bounded fallback chain are configuration; one provider implementation behind a factory. **Verified against the live provider** — see `AD-028` |
| Conversation persistence | **Implemented** | Was "Not implemented". Users, conversations, and messages persist and survive a restart |
| Web research | Not implemented | Unchanged — Phase 3 |
| File intelligence | Not implemented | Unchanged — Phase 4 |
| Research workspace | Not implemented | Unchanged — Phase 5. A `conversations.project_id` column is reserved for it (AD-015) |
| Agent orchestration | Skeleton only | Unchanged — Phase 6 |
| SaaS | Not implemented | Unchanged — Phase 8 |
| Tests | **Implemented** | Was "Not implemented". **368 tests**, `ruff` and `mypy` clean; database tests run against a real PostgreSQL |

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
| 1     | Core Infrastructure & Stabilization | In Progress                 | Make current system reliable      |
| 2     | AI Chat MVP                         | Complete                    | Build reliable conversational AI  |
| 3     | Research Engine                     | Not Started                 | Web research pipeline             |
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

### Provider selection is deferred

The concrete web search provider is **not** decided by this document and must be selected during Phase 3 based on requirements, cost, reliability, and availability. It must be integrated behind a provider abstraction so the choice remains reversible. See the Architecture Decision Log entry `AD-006`.

### Existing partial artifact

`Supervisor.generate_final_answer` (`app/agents/supervisor.py:46-65`) is a two-stage design sketch with zero callers. It is dead code today. It may inform the Phase 3 synthesis stage, but it must not be treated as an existing capability.

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
| AD-006 | Web search will be integrated behind a provider abstraction; the concrete provider is undecided | No search provider exists in the repository. Selecting one prematurely would lock in cost and availability assumptions without requirements | 3 | Open |
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
