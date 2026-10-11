# Architecture & Technical Audit — Telegram AI Research Bot

> ## ⚠️ HISTORICAL AUDIT SNAPSHOT — PRESERVED UNMODIFIED
>
> **This document describes the repository as it was on 2026-09-25, BEFORE Phase 1
> implementation. It is not a statement of the current state.**
>
> Phase 1 — Core Infrastructure & Stabilization has since resolved the findings
> recorded here: C-1, C-2, C-3, C-4, H-1 through H-7, M-5 through M-10, and S-2.
> Two remained partial (H-8 containerisation, and the CI tail of SR-10) and one
> was blocked on a manual human action (credential rotation, P0-3 / SR-4).
> **All three were subsequently resolved** — rotation completed before Phase 2,
> Docker and CI verified green (reconciled 2026-10-07; see the Phase 3 P3.0
> note below and Phase 1 § 14).
>
> Everything below is retained **verbatim as the defect baseline**. Its line
> numbers, findings, and severity judgements describe the pre-Phase-1 code and
> must not be "corrected" to match the new implementation — that would destroy
> the record of what was wrong and why.
>
> **For the current state, see:**
> - [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) — maturity table and decision log
> - [`phases/PHASE-01-INFRASTRUCTURE.md`](./phases/PHASE-01-INFRASTRUCTURE.md) § 14 — implementation record, validation evidence, and open blockers
> - [`../README.md`](../README.md) — how to run the system
>
> Seven additional defects were found *during* Phase 1 implementation and are
> recorded in the Phase 1 document § 14, not here, because they did not exist
> when this audit was taken.
>
> ### Update — 2026-09-26, after Phase 2 (AI Chat MVP)
>
> Phase 2 has since been implemented. The audit findings below are **still not
> edited**; they remain the pre-Phase-1 baseline. The structural conclusions that
> Phase 2 changed are noted here so a reader is not misled by § 7's "no service
> layer" or § 6's "no database code" when looking at the current code:
>
> - **A service layer now exists** (`app/services/`), with a chat service owning
>   conversation orchestration and a context service owning ordering, budgeting,
>   and truncation. `app/services/research.py` was removed and its delivery-neutral
>   contract moved to `app/services/chat.py` (`AD-026`).
> - **A data layer now exists** (`app/db/`, `app/repositories/`): `users`,
>   `conversations`, `messages`, with Alembic migrations. Audit finding M-2
>   ("no database layer") and M-3 ("no conversation memory") are resolved.
> - **`DATABASE_URL` is now required** and wired into a real async engine. The
>   `/ready` endpoint, which this audit's H-5 finding correctly said did not
>   probe PostgreSQL, now does.
> - **The request path is no longer a query decomposition.** It is a persistent
>   multi-turn chat. `Supervisor.analyze_query` (M-1's dead-code finding) is
>   retained but unwired, for Phase 3.
> - **The single hardcoded provider is now selected by configuration**, behind a
>   factory, with a bounded fallback chain. The endpoint is Groq rather than the
>   DeepSeek-compatible one named in this audit's § 5.
> - **The `bot ↔ tasks` circular dependency** this audit recorded as structural
>   was already broken in Phase 1 and remains broken.
>
> Six further defects were found during Phase 2 and are recorded in
> [`phases/PHASE-02-AI-CHAT.md`](./phases/PHASE-02-AI-CHAT.md) § 14.
>
> ### Update — 2026-09-26, Phase 2 completion pass
>
> Two changes since the note above, both recorded rather than folded into the
> audit text:
>
> - **Account deletion exists (SR-9).** `/delete_account confirm` removes the
>   user row; the foreign keys cascade to conversations and messages. One
>   statement, one transaction. The cascade was verified against real PostgreSQL
>   rather than inferred from the schema, and proven narrow by deleting one of
>   two users and checking the survivor. This is the only capability the audit's
>   "no user accounts" line could be read as contradicting, so it is called out.
> - **The LLM endpoint is Groq, not the DeepSeek-compatible one** named in § 5 of
>   this audit, and the model is `qwen/qwen3.8-27b`. The client class is
>   unchanged, so the "one hardcoded provider" finding is still resolved — the
>   difference is configuration, recorded as `AD-028`.
>
> Two defects found in this pass are recorded in the Phase 2 document, not here:
> every secret scanner in the repository was blind to Groq's `gsk_` key format
> (`P2-7`), and the configured model had been retired by the provider (`P2-8`).
>
> ### Update — 2026-10-07, Phase 3 P3.0 contract freeze
>
> Phases 1 and 2 are complete and closed (credential rotation, CI, and Docker
> verification reconciled; Phase 1 § 14 and Phase 2 § 14 carry the notes).
> Phase 3 is active. P3.0 froze the Research Engine's architectural contract
> **without writing production code** — nothing below is implemented; each
> entry is a frozen decision an implementation stage must satisfy. Full
> contract: [`phases/PHASE-03-RESEARCH-ENGINE.md`](./phases/PHASE-03-RESEARCH-ENGINE.md)
> § 15; decisions AD-029 … AD-035 in [`PROJECT_PLAN.md`](./PROJECT_PLAN.md).
>
> - **Port-based Research Engine.** Domain/application logic will depend on
>   ports (search provider, safe fetcher, content extractor, ranking policy,
>   research repositories) with concrete adapters in `app/infrastructure/` —
>   the same philosophy as the Phase 2 `LLMProvider`. The search provider
>   stays deliberately unselected (AD-006, gates P3.2).
> - **SSRF fetch boundary.** A dedicated restricted fetch client: DNS
>   resolution with every resolved address validated against
>   private/loopback/link-local/reserved ranges before connecting; the
>   connection uses the validated resolution (DNS-rebinding closure);
>   revalidation at every redirect hop; HTTP(S) only; timeout, streaming
>   size, decompression, and redirect limits. A security boundary, not an
>   optimization.
> - **Untrusted-content boundary.** Fetched web content is data, never
>   instructions: no tool surface for the research model, delimited
>   untrusted prompt regions, structurally validated model outputs, and
>   pipeline-assigned source identity. Prompt-injection defense is
>   architectural.
> - **Citation trust chain.** `source → source_chunk → claim → citation`,
>   every link a persisted row, verified programmatically (FK registry plus
>   passage verification) — never model self-report.
> - **Phase 3 persistence model.** Five tables: `research_runs`, `sources`,
>   `source_chunks`, `claims`, `citations`. The `/delete_account` cascade
>   must extend across them. Phase 5 workspace persistence remains out of
>   scope.
> - **Trigger contract.** `/research <question>`; chat behavior unchanged.
> - **Recorded execution risk.** The 120-second Celery task limit is sized
>   for one chat call, not a research run; the deadline/cancellation model
>   is assigned to the execution stage (P3.9) and was deliberately not
>   changed at P3.0.
>
> ### Update — 2026-10-07, Phase 3 P3.1 persistence foundation
>
> P3.1 implemented the Phase 3 persistence foundation and nothing else:
>
> - **Domain:** `app/domain/research/` — pure pydantic types (`ResearchRun`,
>   `Source`, `SourceChunk`, `Claim`, `Citation`) with the frozen status
>   enums, plus runtime-checkable repository ports. No provider ports yet;
>   each arrives with the stage that needs it (§ 15.3).
> - **Ports/UoW:** `app/domain/ports.py` `UnitOfWork` now exposes the five
>   research repositories alongside the Phase 2 ones.
> - **Persistence:** `app/db/models.py` adds the five ORM models;
>   `app/repositories/research.py` implements the ports with SQLAlchemy in
>   the same style as Phase 2 (ownership scoping via `get_for_user`).
> - **Schema:** migration `0002_research` (down_revision `0001_phase2`, with
>   full downgrade) creates the five tables with CHECK constraints on the
>   frozen status sets, uniqueness (`sources(run_id,url)`,
>   `source_chunks(source_id,ordinal)`, `citations(claim_id,source_id,chunk_id)`),
>   FK `ON DELETE CASCADE` everywhere, `SET NULL` on
>   `research_runs.conversation_id` / `sources.duplicate_of_id`, and indexes
>   on the retrieval paths. `alembic check` is clean.
> - **Deletion cascade (AD-034):** `/delete_account` now removes the user's
>   entire research subtree in the same single cascade delete as the Phase 2
>   tree; verified against real PostgreSQL including transactional rollback
>   and cross-user isolation.
> - **Citation integrity at schema level:** FKs make a citation to
>   never-persisted evidence uninsertable; programmatic passage verification
>   remains a later-stage concern (AD-033).
> - **Status:** 406 tests green, ruff/mypy clean, `alembic check` clean.
>   Still not implemented: search, fetch (SSRF), extraction, ranking,
>   planning, synthesis, citations validation, `/research` handler, the
>   Celery research task. Next stage: P3.2 (search port + provider
>   selection, AD-006 gate) — see the P3.2 update below.
>
> ### Update — 2026-10-08, after Phase 3 P3.7 (per-source summarization + evidence-grounded claims)
>
> The P3.6 note below is extended, not corrected. P3.7 added the stage that turns one
> source's persisted passages into a summary and a set of claims, and changed no prior
> decision.
>
> - **Claims are born with evidence, and that is structural, not procedural.**
>   `ClaimExtractor.extract()` returns `ClaimCandidate` values — unverified model output.
>   The *application layer* calls `verify_claims()`, and it is the only thing in the
>   system that can produce a `VerifiedClaim`. Accepting a claim is therefore not in any
>   adapter's signature, and no adapter — the part of this stage closest to untrusted web
>   text — has the authority to grant it (AD-039).
> - **A claim is accepted only on a substring, never on similarity.** The quoted passage
>   must be present in the stored `source_chunks.text` after an explicitly minimal
>   normalization (NFC + whitespace collapse). No embedding, no fuzzy matching, no second
>   model adjudicating the first. The model is never asked to verify itself, and
>   `claims.confidence` is deliberately left `NULL` because nothing here measures it.
> - **A passage reference is resolved only against the current source's passages.** A
>   chunk id belonging to another source is rejected exactly as one that does not exist;
>   the database is never consulted to classify a fabricated id (AD-039).
> - **Retrieved content enters prompts for the first time, as untrusted data.** Both
>   system-prompt builders take *numbers only* — passage count, claim ceiling — so no
>   parameter exists through which a title, a domain, or a character of fetched text could
>   reach the instructions. Content and metadata travel in the user role inside delimiters
>   the system prompt declares to be quotable evidence. The adapter has **no tool
>   surface**: the Phase 2 `LLMProvider` port has no tool slot, and the adapter imports no
>   HTTP client, SDK, browser, or database driver (asserted structurally, AD-032).
> - **The passage quote is persisted, and ownership is a database constraint.**
>   `citations.quote` is `NOT NULL`, and `citations (chunk_id, source_id) →
>   source_chunks (id, source_id)` makes a cross-source citation uninsertable. The
>   textual containment rule cannot be expressed in SQL — it compares one row's text
>   against another's — so it is enforced immediately before persistence and asserted
>   against the stored rows. **No table was added**; the five-table model of AD-034 is
>   unchanged (AD-038, migration `0003_research_evidence`).
> - **A summary is not evidence.** `sources.summary` is a text column with no relation to
>   `citations`, so it cannot be cited. Coverage is bounded and *reported*
>   (`summary_truncated`, `claim_truncated`) rather than silently truncated.
> - **Status:** 322 new tests; `ruff` and `mypy` clean on every P3.7 file; `alembic check`
>   clean. **Full suite: 1492 passed, 21 failed** — all pre-existing and unrelated
>   (P3.3 `_embedded_ipv6` and `FetchHTTPStatusError` defects, a P3.4 dependency-declaration
>   debt, and one environment-dependent P3.3 DNS test). **Live-provider quality is NOT
>   VERIFIED.** Full record:
>   [`audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](./audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md)
>   § P. Next stage: **P3.8 — Synthesis + citation construction.**
>
> ### Update — 2026-10-08, after Phase 3 P3.6 (research planning)
>
> The P3.5 note below is extended, not corrected. P3.6 added the stage before
> search and changed no prior decision.
>
> - **Planning is a validated intermediate representation.** `ResearchPlan`
>   (`app/domain/research/planning.py`) carries what FR-1 names — an objective,
>   steps, and per step a sub-question, a search query, and expected source types —
>   as typed fields, never as an untyped blob. A plan *describes* research; it does
>   not perform it, and it is not evidence.
> - **Non-degeneracy is enforced (FR-3).** Empty objective, no steps, **or no
>   search step** is rejected at construction. A run whose plan legitimately finds
>   nothing is a later stage's outcome (FR-41); a run with no plan is a defect, and
>   the two are deliberately not conflated.
> - **The dependency graph is validated, not assumed.** Unknown references,
>   self-dependencies, and cycles each raise a distinct typed error, checked with an
>   iterative Kahn's algorithm — no graph library, because the project declares none
>   and the requirement is one reachability question over a handful of nodes.
> - **Phase 2's `LLMProvider` is reused unchanged.** The planner is a
>   `PlanningProvider` adapter that calls the existing port; there is no second
>   provider abstraction, no new client, and no SDK import in the planner. Its
>   existing bounded retry and model fallback chain are reused, so the planner adds
>   no retry of its own.
> - **AD-026 resolved — Option B.** A dedicated Phase 3 planner supersedes
>   `Supervisor.analyze_query` for planning; `Supervisor` is retained unchanged.
>   `QueryAnalysis` cannot express FR-1's search strategies or expected source
>   types, and widening a Phase 2 schema the chat path depends on would couple two
>   unrelated paths. Recorded as `AD-037`; evidence in the audit § O.10.
> - **The planner has no tools, so model output cannot cause an action** (AD-032).
>   The question reaches the model only as delimited user-role data, and the system
>   prompt is a constant taking only a step count — the Phase 2 parameterless-builder
>   invariant, kept. This is stage containment, not full injection immunity; the
>   stronger boundary for retrieved web content belongs to P3.8.
> - **Persisted on the existing column.** `research_runs.plan` (JSONB) already
>   existed from P3.1 and was reused; the round-trip is lossless and re-validating,
>   verified against real PostgreSQL. **No migration, no new dependency.**
> - **Status:** 204 new tests; `ruff` and `mypy` clean on every P3.6 file.
>   **Live-provider quality is NOT VERIFIED** — planning was tested against a fake
>   provider only. Full record:
>   [`audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](./audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md)
>   § O. Next stage: **P3.7 — Summarization + claim extraction.**
>
> ### Update — 2026-10-08, after Phase 3 P3.5 (deduplication + ranking)
>
> The P3.2 note below is extended, not corrected. P3.5 added the stage between
> extraction and synthesis, and nothing here disturbs the SSRF boundary.
>
> - **Deterministic processing layer, two ports.** `SourceDeduplication` and
>   `RankingPolicy` are `runtime_checkable` `Protocol`s in
>   `app/domain/research/ports.py`, so the strategy is replaceable (FR-29). One
>   stateless, I/O-free adapter in `app/infrastructure/ranking/` implements both;
>   all the logic lives in `app/domain/research/dedup.py`.
> - **Canonicalization is not validation.** `canonical_url` compares two values.
>   It opens no socket, resolves nothing, and approves nothing for retrieval — the
>   § 15.3 boundary remains P3.3. This is stated explicitly because it is the most
>   natural place for a second, weaker SSRF boundary to grow.
> - **Tracking parameters are an explicit, reviewable set**, not a heuristic.
>   Anything outside it is preserved, because removing a parameter can change what
>   a URL points at.
> - **Identity is canonical URL ∪ exact content fingerprint.** Fingerprints are
>   taken over P3.4's *extracted main content*, which is what keeps two distinct
>   articles that share a nav menu from collapsing (FR-25). Union-find makes the
>   two rules one equivalence. No embeddings, no vectors, no LLM.
> - **Ranking reads no clock.** Freshness is scored against a caller-supplied
>   `reference_time`, so ranking a stored candidate set reproduces its original
>   order. The weights are named constants with documented rationale; only the
>   quality threshold is configurable.
> - **Exclusion is recorded, never silent.** A source below the threshold keeps
>   its entry and carries an `excluded_reason`, because a stage that dropped rows
>   could not report how many it saw (FR-42).
> - **Status:** 179 new tests; `ruff` and `mypy` clean on every P3.5 file. No
>   migration, no new dependency, no schema change. **Known limitation: content
>   identity is exact only** — an edited syndicated copy is not detected.
>   Full record: [`audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](./audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md)
>   § N. Next stage: **P3.6 — Planning.**
>
> ### Update — 2026-10-07, after Phase 3 P3.2 (search port + provider selection)
>
> **Search exists as a contract and one adapter; the pipeline still does not
> exist.** The P3.1 note above is extended, not corrected.
>
> - **SearchPort is provider-independent:** `app/domain/research/ports.py`
>   declares `SearchProvider`, a `runtime_checkable` `Protocol` with one method,
>   `async search(SearchRequest) -> SearchResponse`. The types live in
>   `app/domain/research/search.py` — `SearchRequest` (query, bounded result
>   count, optional language/market, optional relative freshness window,
>   optional safe-search level), `SearchResult` (`url`, `domain`, `rank`,
>   `title`, `snippet`, `published_at`, `relevance`), `SearchResponse`, and
>   `domain_of()`, the single normalization rule.
> - **Exactly one concrete adapter exists:**
>   `app/infrastructure/search/brave.py` (`BraveSearchAdapter`), selected by
>   `app/infrastructure/search/factory.py::create_search_provider` from
>   `SEARCH_PROVIDER`. The provider's endpoint, auth-header name, freshness
>   codes, safe-search codes and result ceiling appear in no other file.
> - **Provider-specific concerns stay inside the adapter:** credential as a
>   per-request header (never a URL, never a client default), `trust_env=False`,
>   `follow_redirects=False`, explicit per-call timeout, status→typed-error
>   mapping via the `SearchError` family in `app/domain/errors.py`, and logging
>   of outcome and counts only — never the credential, the query, or a body.
> - **Replacement is still possible without touching research logic:** a new
>   adapter module plus one branch in the factory. No domain, persistence, or
>   stage code changes; a fake `SearchProvider` satisfying a consumer unchanged
>   is asserted in the suite (T-3).
> - **A search result is not evidence.** `SearchResult` ≠ `Source` ≠
>   `SourceChunk`: a result is a URL and a provider-written snippet for a page
>   nobody in this process has read, and a `Source` is born `PENDING`. Returned
>   URLs are untrusted and unvalidated here — SSRF protection, redirect
>   validation, content-type and size limits are P3.3's boundary (AD-031), and
>   duplicating part of it in the adapter would create a second, weaker one.
> - **Provider selected:** Brave Search API — `AD-036`, superseding `AD-006`.
>   Eight candidates were evaluated with live reachability/error probes and
>   vendor documentation. **Result quality (relevance, diversity, Persian
>   coverage) was NOT measured:** no usable search credential existed in the
>   selection environment, and nothing was estimated in its place. Evidence and
>   trade-offs: [`audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](./audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md)
>   § K.
> - **Status:** 512 tests green (106 new, all transport-mocked; no test calls a
>   real provider), ruff clean, mypy clean, `httpx` promoted from a transitive
>   pin to an explicit one at the version already resolved. No migration was
>   created or altered. Still not implemented: fetch (SSRF), extraction,
>   cleaning, ranking, dedup, planning, synthesis, citations, the research
>   orchestrator, the Celery research task, the `/research` handler, any research
>   LLM call. Next stage: **P3.3 — Fetcher + SSRF Security Boundary.**

**Audit type:** READ-ONLY technical audit
**Repository:** `/Users/erfmollaey/projects/telegram-research-bot`
**Date:** 2026-09-25
**Scope:** Full repository, excluding `venv/` and `__pycache__/`

**Method:** Every non-`venv` file was read directly (8 Python files, `requirements.txt`, `docker-compose.yml`, `.env`, `.python-version`). Findings were cross-checked against the codebase knowledge graph (call edges, dependency edges, route table, coverage report). No files were modified, no packages installed, no migrations run, no configuration changed, no commits made.

**Coverage note:** Index coverage for this repository reported `parse_partial: 0` and `skipped: 0`. All source was additionally read directly, which is stronger evidence than the graph. Findings below cite exact file paths and line numbers.

**Secret handling:** No secret values are reproduced in this document. Secrets are referenced by file path only.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current Architecture](#2-current-architecture)
3. [Implemented Features](#3-implemented-features)
4. [Current Message Flow](#4-current-message-flow)
5. [Security Findings](#5-security-findings)
6. [Bugs & Technical Issues](#6-bugs--technical-issues)
7. [Code Quality Findings](#7-code-quality-findings)
8. [Testing Gaps](#8-testing-gaps)
9. [Roadmap Status](#9-roadmap-status)
10. [Recommended Next Steps](#10-recommended-next-steps)
11. [P0/P1/P2/P3 Priority List](#11-p0p1p2p3-priority-list)
12. [Overall Project Readiness](#12-overall-project-readiness)
13. [What To Build Next](#13-what-to-build-next)
14. [Appendix A — Repository Inventory](#appendix-a--repository-inventory)
15. [Appendix B — Verified Call Graph](#appendix-b--verified-call-graph)

---

## 1. Executive Summary

This project is a **skeleton, not a working research bot**. The entire codebase is 8 Python files totaling roughly 150 statements. What exists is a thin vertical slice: FastAPI process → aiogram long polling → one catch-all handler → Celery `.delay()` → one DeepSeek call that splits a question into sub-questions → text sent back to Telegram.

Of the 9 stated product goals, **exactly one is partially implemented** (receive questions via Telegram and get an LLM response). Web search, source reading, multi-source summarization, citations, report generation, file handling, history, and workspaces **do not exist in any form**. There is no database code, no model layer, no schema layer, and no test of any kind.

Three findings make the system non-functional as written, independent of any missing features:

1. **A live API key is hardcoded in plaintext in `app/agents/supervisor.py:8`**, and it is the wrong key for the endpoint it is sent to.
2. **`app/tasks/research_task.py:15-18` creates and closes a new event loop per task while reusing a module-level `aiogram.Bot` whose HTTP session is bound to the first loop.** Only the first query per worker process can succeed.
3. **`openai` is imported in `app/agents/supervisor.py:3` but is absent from `requirements.txt`.** A clean environment cannot start this application.

The `/health` endpoint returns `{"status": "ok"}` unconditionally, so none of the above is observable from outside the container.

---

## 2. Current Architecture

Actual implementation, derived from the call graph and source:

```
Telegram Bot API
      │  (long polling, getUpdates — no webhook)
      ▼
┌──────────────────────────────────────────────────────────────┐
│ PROCESS 1 — FastAPI + aiogram  (app/main.py)                 │
│                                                              │
│  startup event ──► asyncio.create_task(start_polling)  ──────┼──► dp.start_polling(bot)
│                                                              │
│  Dispatcher  (app/bot/dispatcher.py)                         │
│    Bot(token=settings.BOT_TOKEN, parse_mode=HTML)  ◄── .env  │
│    Dispatcher()                                              │
│                                                              │
│  Handlers  (app/bot/handlers.py)                             │
│    @dp.message(Command("start"))  ─► start_command()         │
│    @dp.message()                   ─► research_handler()     │
│                                        │                     │
│  GET /health ─► {"status":"ok"}  (static, checks nothing)    │
└───────────────────────────┬──────────────────────────────────┘
                            │ process_research.delay()
                            ▼
                    ┌───────────────┐
                    │  Redis        │  redis://localhost:6379/0
                    │  broker       │  (from .env REDIS_URL)
                    └───────┬───────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ PROCESS 2 — Celery worker  (app/core/celery_app.py)          │
│                                                              │
│  Celery("research_bot", broker=REDIS_URL, backend=REDIS_URL) │
│    include=["app.tasks.research_task"]                       │
│                                                              │
│  process_research(user_id, query)   (research_task.py)       │
│    └─► Supervisor()                                         │
│         └─► analyze_query(query) ──► POST api.groq.com       │
│              OpenAI SDK, model="llama-3.3-70b-versatile", JSON mode │
│    └─► asyncio.new_event_loop() ... bot.send_message()  ✗    │
└──────────────────────────────────────────────────────────────┘

  ┌───────────────  NOT CONNECTED  ────────────────┐
  │ PostgreSQL 16 (docker-compose)   DATABASE_URL  │  declared,
  │ redis-py, asyncpg, sqlalchemy, httpx,          │  zero usage
  │ python-dotenv  in requirements.txt             │
  └────────────────────────────────────────────────┘
```

### Entry points

| Entry point | File | Notes |
|---|---|---|
| ASGI application | `app/main.py` | No `if __name__ == "__main__"` runner, no documented start command |
| Celery worker | `app/core/celery_app.py` | No Procfile, no Makefile, no documented start command |

### Process topology

Two processes are required but neither is containerized:

- **Process 1** — FastAPI + aiogram. Serves `/health` and runs the Telegram poller.
- **Process 2** — Celery worker. Performs the LLM call and sends the reply.

Both processes must import `app.core.config`, which instantiates `Settings()` at import time. A missing environment variable therefore crashes process startup rather than producing a configuration error.

### Absent infrastructure

No `Dockerfile`, no `Procfile`, no `Makefile`, no `README`, no `CONTRIBUTING`, no `pyproject.toml`, no CI configuration, no `.dockerignore`, no `.gitignore`, no `.env.example`, no `alembic.ini`, no test directory.

---

## 3. Implemented Features

### 3.1 Telegram — 🟡 partially implemented

| Capability | Status | Evidence |
|---|---|---|
| Bot initialization | ✅ | `app/bot/dispatcher.py:6-9` |
| Long polling | ⚠️ | `app/main.py:19-24`, fire-and-forget task, no restart on failure |
| Webhook | ❌ | Not present. No secret-token validation, no HTTPS endpoint |
| `/start` command | ✅ | `app/bot/handlers.py:9-13` |
| Command handling (general) | ❌ | Only `Command("start")`; any other command falls into the catch-all |
| Text message handling | ⚠️ | `app/bot/handlers.py:15` — no `F.text` filter, so `message.text` is `None` for photos/stickers |
| Reply mechanism | ✅ | `message.answer(...)` at line 18; `bot.send_message(...)` at `research_task.py:17` |
| User identification | ⚠️ | `message.from_user.id` used as `chat_id`; no DB user record, no allowlist, no `from_user` None-guard |
| Conversation/session handling | ❌ | No persistence, no memory, no per-user state anywhere |
| Error handling | ❌ | No `@dp.errors()` handler, no try/except in any handler |
| Markdown/HTML escaping | ❌ | `ParseMode.HTML` set globally but output is `**markdown**`, never escaped or split |

### 3.2 AI / LLM — 🟡 minimal

| Capability | Status | Evidence |
|---|---|---|
| Provider SDK | ⚠️ | `openai` SDK against Groq's OpenAI-compatible endpoint (`supervisor.py:7-10`) |
| Groq integration | ✅ | Configured — `GROQ_API_KEY` with `https://api.groq.com/openai/v1` |
| OpenRouter integration | ❌ | Absent. A key *shaped* like an OpenRouter key is present but sent to the provider |
| Model selection | ❌ | Hardcoded `self.model = "llama-3.3-70b-versatile"` (`supervisor.py:14`) |
| Prompt construction | ⚠️ | F-string prompt, no system role, user text interpolated raw |
| System/user separation | ❌ | Single `{"role": "user"}` message only |
| Conversation context | ❌ | No history, no memory, single-turn |
| Token/context management | ❌ | No `max_tokens`, no truncation, no budgeting |
| Streaming | ❌ | Absent |
| Timeout | ❌ | Absent — SDK default (600s) applies |
| Retry / backoff | ❌ | Absent (SDK internal retries only) |
| Rate-limit (429) handling | ❌ | Absent |
| Fallback models | ❌ | Absent |
| Response validation | ❌ | Bare `json.loads`, then direct `dict` access |

### 3.3 Database — ❌ not implemented

Zero database code exists. Verified by keyword scan across `app/` for `sqlalchemy`, `asyncpg`, `create_engine`, `sessionmaker`, `BaseModel`, `alembic`, `migration` — **no matches** outside unrelated string occurrences.

- `DATABASE_URL` declared in `app/core/config.py:6` and set in `.env` — never imported anywhere.
- `asyncpg`, `sqlalchemy` pinned in `requirements.txt:6-7` — never imported.
- `postgres:16-alpine` service in `docker-compose.yml:10-20` — nothing connects to it.
- No tables, models, relationships, indexes, constraints, or migrations.
- No user, conversation, message, research, or source storage.

### 3.4 Research functionality — ❌ none exists

| Capability | Status |
|---|---|
| Web search | ❌ |
| URL fetching | ❌ |
| HTML extraction | ❌ |
| Content cleaning | ❌ |
| Source ranking | ❌ |
| Source deduplication | ❌ |
| Summarization | ❌ |
| Multi-source analysis | ❌ |
| Citation generation | ❌ |
| Fact checking | ❌ |
| Report generation | ❌ |

`Supervisor.generate_final_answer` (`app/agents/supervisor.py:46-65`) is the only trace of a synthesis stage. It has **0 inbound and 0 outbound call edges** in the graph — verified dead code. Nothing fetches, reads, or cites any source; the sub-questions are generated and then discarded.

### 3.5 Infrastructure — 🟡 partial

| Item | Status | Evidence |
|---|---|---|
| Docker Compose | ⚠️ | Redis + Postgres only; **no bot service, no worker service** |
| Dockerfile | ❌ | Absent |
| Env configuration | ⚠️ | `app/core/config.py`, works but fragile |
| `.env.example` | ❌ | Absent |
| `.gitignore` | ❌ | **Absent** — `.env` and `venv/` are unprotected |
| Secrets management | ❌ | One key hardcoded in source; no rotation, no secret store |
| Logging | ⚠️ | `logging.basicConfig` called twice; f-strings; full user text logged |
| Monitoring | ❌ | Absent |
| Health checks | ⚠️ | `/health` is static; no Compose healthchecks; no dependency probing |
| DB migrations | ❌ | Absent |
| Background jobs | ✅ | Celery + Redis broker |
| Redis / Celery | ⚠️ | Present but unconfigured for reliability (see P0-5) |
| Tests | ❌ | Zero |
| CI / linting | ❌ | No config files |

---

## 4. Current Message Flow

Traced from source and confirmed against graph edges `research_handler → process_research` and `process_research → Supervisor.analyze_query`.

```
1.  Telegram message
2.  app/main.py:15-17         on_startup() → asyncio.create_task(start_polling())
3.  app/main.py:22            await dp.start_polling(bot)      [long polling, no webhook]
4.  app/bot/dispatcher.py:6   bot = Bot(settings.BOT_TOKEN, parse_mode=HTML)
5.  app/bot/handlers.py:9     @dp.message(Command("start")) → start_command()   [/start only]
6.  app/bot/handlers.py:15    @dp.message() → research_handler()                [catch-all, no filter]
      ├─ handlers.py:17       logging.info(f"...: {message.text}")   ← full user text logged
      ├─ handlers.py:18       await message.answer("⏳ Analysing your question…")
                                    ↑ the original pre-Phase-1 string was in Persian; the
                                      finding below is about format, not language
      └─ handlers.py:19-22    process_research.delay(user_id=message.from_user.id, query=message.text)
7.  Redis broker              redis://localhost:6379/0
8.  app/tasks/research_task.py:8   supervisor = Supervisor()        [per-task instantiation]
9.  app/agents/supervisor.py:9     Supervisor.analyze_query(query)
      ├─ supervisor.py:20-34  builds f-string prompt, no system role
      ├─ supervisor.py:36-41  client.chat.completions.create(
      │                            model="llama-3.3-70b-versatile", temperature=0.3,
      │                            response_format={"type":"json_object"})
      │                            → POST https://api.groq.com/openai/v1/chat/completions
      │                            NO timeout, NO max_tokens, NO retry, NO auth fallback
      └─ supervisor.py:43     result = json.loads(...)            ← unguarded
10. research_task.py:12       f-string builds "**Research result:**..."  ← Markdown, sent as HTML
                                    ↑ the original pre-Phase-1 label was in Persian; the defect
                                      is the Markdown-under-HTML mismatch, not the language
11. research_task.py:15-16    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
12. research_task.py:17       loop.run_until_complete(bot.send_message(chat_id=user_id, text=...))
13. research_task.py:18       loop.close()                        ← closes loop, leaks HTTP session
```

### Answers to the specific questions

- **Which function receives it?** `app/bot/handlers.py:research_handler` (registered at line 15).
- **Which service processes it?** None. There is no service layer. `app/tasks/research_task.py:process_research` is the task body and `app/agents/supervisor.py:Supervisor` is the only logic holder.
- **How is the user identified?** `message.from_user.id` (`handlers.py:20`). Not persisted, not validated, no allowlist. This value is then used as `chat_id` at `research_task.py:17` — correct in a private chat, **broken in any group** (see H-1).
- **How is conversation context retrieved?** It isn't. `analyze_query(query)` receives exactly one string. No history, no memory, no DB.
- **How is the LLM called?** `client.chat.completions.create` at `supervisor.py:36-41` using the `openai` SDK against `https://api.groq.com/openai/v1`.
- **How is the response generated?** `json.loads` of the model output into a dict, then string-interpolated into a Persian Markdown template at `research_task.py:12`.
- **How is it sent back?** `bot.send_message(chat_id=user_id, text=final_answer)` at `research_task.py:17`, inside a manually created event loop.
- **What happens on error?** Nothing recoverable. There is exactly **one** `try`/`except` in the entire codebase (`main.py:20-23`, which only guards polling startup). If the provider call raises, Celery marks the task `FAILURE`, no message is sent, and the user's "⏳ analyzing…" message is never resolved. If Redis is down, `process_research.delay()` raises inside the aiogram handler with no reply. The user is left hanging with no error path in either case.

---

## 5. Security Findings

**SECRET FOUND IN FILE: `app/agents/supervisor.py`** — a full API key literal in `api_key=` at line 8. It is `sk-or-v1-`-prefixed (OpenRouter key format) but is sent to the provider's OpenAI-compatible base URL (line 9), whose keys are `sk-`-prefixed. Two distinct problems: the credential is exposed in plaintext source, and it cannot authenticate to the configured endpoint. The correct key exists in `.env` as `GROQ_API_KEY` and is **never read by any code**.

**SECRET FOUND IN FILE: `.env`** — contains a real-format `BOT_TOKEN` and a real-format provider API key. Values were not printed and were not tested for validity.

### Findings

| ID | Severity | Finding | Location |
|---|---|---|---|
| S-1 | CRITICAL | Plaintext API key literal in source, with a key/endpoint mismatch that guarantees authentication failure | `app/agents/supervisor.py:7-10` |
| S-2 | HIGH | No `.gitignore`. Not currently a git repository, so nothing is committed today — but a single `git init && git add .` publishes a live Telegram bot token, an API key, and the entire `venv/`. No `.env.example` documents the required variables | repo root |
| S-3 | MEDIUM | Unauthenticated `/health`. Low information disclosure alone, but it is the only endpoint and has no rate limiting | `app/main.py:11-13` |
| S-4 | MEDIUM | PostgreSQL published with default credentials in plaintext; Redis published with no authentication. Both bind to all interfaces by default. Acceptable for local dev, unsafe on a shared or cloud host | `docker-compose.yml:6-7, 13-17` |
| S-5 | MEDIUM | Prompt injection, unmitigated. Raw user text interpolated into a prompt via f-string, no system message, no delimiter, no instruction to ignore embedded directives. Model output is rendered back to the user. Blast radius is low today (one JSON decomposition call), but the moment web-fetched content enters a prompt this becomes a real injection vector, and the architecture has no defense in place | `app/agents/supervisor.py:23` |

### Attack surface not currently present

No SQL, so no SQL injection surface. No `subprocess` / `os.system` / `eval` / `exec`, so no command injection. No URL fetching, so no SSRF surface *yet*. No deserialization of untrusted data (`json.loads` only). No file-path handling from user input, so no arbitrary file access. No auth layer to bypass.

### Not verified

**NOT VERIFIED — insufficient evidence in repository.** Whether the exposed credentials are currently active cannot be determined without network calls, which are out of scope for a read-only audit. Whether they were ever committed cannot be determined: there is no `.git` directory and therefore no history to inspect.

---

## 6. Bugs & Technical Issues

### 6.1 CRITICAL

#### C-1 · Hardcoded, mismatched API key
**File:** `app/agents/supervisor.py:7-10` (module level)
**Function/class:** module-level `client`

An OpenRouter-format key is hardcoded and pointed at the provider's base URL. The key is in plaintext source, it is not the key configured in `.env`, and it will be rejected by the endpoint.

- **Why it matters:** guaranteed authentication failure on every LLM call, plus credential exposure.
- **Fix:** read `settings.GROQ_API_KEY` (already defined at `config.py:7`), add `openai` to `requirements.txt`, rotate the exposed key, and scrub it from source history if this ever reaches a shared repo.

#### C-2 · `openai` missing from `requirements.txt`
**File:** `requirements.txt:1-10`

`supervisor.py:3` imports `openai`; `requirements.txt` does not list it. It is present in `venv/` (installed ad hoc) but a clean install cannot run this app.

- **Why it matters:** unreproducible environment, guaranteed `ModuleNotFoundError` on deploy.
- **Fix:** add `openai` with a pinned version.

#### C-3 · Event-loop lifecycle bug breaks all but the first query per worker
**File:** `app/tasks/research_task.py:15-18`
**Function/class:** `process_research`

A new event loop is created per task and closed, while `bot` is a module-level singleton (`dispatcher.py:6`). Verified in the installed aiogram that `Bot.session` is a single `AiohttpSession` created at `Bot.__init__` (`aiogram/client/bot.py:288-293`), whose underlying aiohttp `ClientSession` is created **lazily and cached** (`aiogram/client/session/aiohttp.py:133-146` — recreated only if `None` or `.closed`). The session is therefore bound to the first task's loop, which line 18 closes.

- **Consequences:** the first task leaks an unclosed `ClientSession` (leaked sockets, `ResourceWarning`); every subsequent task in the same prefork worker reuses a session bound to a dead loop and fails on loop-bound operations. `bot.session.close()` is never called, and the intended `async with bot` lifecycle (`bot.py:354-361`) is unused.
- **Why it matters:** the product's single core feature works approximately once per worker process.
- **Fix:** create one long-lived loop per worker process via Celery's `worker_process_init` signal (or run an asyncio loop in a dedicated thread/greenlet) and never close it per task; close the session on `worker_process_shutdown`.

#### C-4 · HTML parse mode with unescaped, unsplit LLM output
**Files:** `app/bot/dispatcher.py:8`, `app/tasks/research_task.py:12, 17`
**Function/class:** `process_research`

`dispatcher.py:8` sets `default=DefaultBotProperties(parse_mode=ParseMode.HTML)`, but `research_task.py:12` builds a `**bold**` Markdown string and line 17 sends it with no `parse_mode` override. Two defects: the `**` markers render literally instead of bold, and any `<` or `&` in the model's output causes Telegram to reject the message with `400 can't parse entities`, which — combined with the total absence of error handling (C-5) — means silent message loss. Separately, there is no length guard against Telegram's 4096-character limit and no `max_tokens` cap on generation (`supervisor.py:36-41`), so an over-long summary is equally likely to be rejected.

- **Fix:** pick one parse mode, escape via `html.escape` or send with `parse_mode=None`, and chunk with aiogram's `text.split_text` utility.

### 6.2 HIGH

#### H-1 · `from_user.id` used as `chat_id` breaks group usage
**Files:** `app/bot/handlers.py:20-21`, `app/tasks/research_task.py:17`
**Function/class:** `research_handler`, `process_research`

In a private chat, `message.chat.id` and `message.from_user.id` are equal and the code works. In a group or supergroup, `chat.id` is negative and `from_user.id` is the sender, so the reply is addressed to the user's direct messages and fails with `403 bot can't initiate conversation with a user` for anyone who has not started the bot. Because the acknowledgement at `handlers.py:18` *is* sent to the group correctly, the visible symptom is a bot that says "analyzing…" in the channel and then never answers.

- **Fix:** pass `chat_id=message.chat.id` alongside the user id.

#### H-2 · No error handling anywhere in the request path
**Files:** `app/bot/handlers.py`, `app/tasks/research_task.py`, `app/main.py:19-24`

The only `try`/`except` in the repository is `main.py:20-23`. Consequences: `process_research.delay()` is unguarded (a Redis outage raises into the aiogram handler with no reply and no user-visible error); the task body has no `try/except`, so any LLM, JSON, or Telegram failure ends in a silent `FAILURE` state; the user permanently sees the "⏳ analyzing…" placeholder. There is no `@dp.errors()` handler, no `autoretry_for`, and no failure notification.

- **Fix:** add an aiogram errors handler, wrap the task body, and always send a terminal message to the user.

#### H-3 · No timeouts, no retries, no token limits on LLM calls
**Files:** `app/agents/supervisor.py:36-41, 59-63`, `app/core/celery_app.py:11-17`
**Function/class:** `Supervisor.analyze_query`, `Supervisor.generate_final_answer`

No `timeout`, no `max_tokens`, and no retry policy are passed to the SDK, and `celery_app.py` sets no `task_time_limit` / `task_soft_time_limit` / `task_acks_late` / `task_reject_on_worker_lost` / `broker_connection_retry_on_startup` / prefetch tuning. A hung upstream connection can occupy a prefork worker slot indefinitely, and an unbounded `max_tokens` is an unbounded cost exposure on a public bot endpoint.

- **Fix:** explicit `timeout=` and `max_tokens=` on every call, `task_time_limit` on the task, and bounded `autoretry_for` with backoff.

#### H-4 · Unguarded parsing of untrusted model output
**Files:** `app/agents/supervisor.py:43`, `app/tasks/research_task.py:9, 12`
**Function/class:** `Supervisor.analyze_query`, `process_research`

`json.loads(response.choices[0].message.content)` is called with no `try`, no null-check on `content`, and no schema validation; `research_task.py:12` then indexes `analysis['summary']` and `analysis['sub_questions']` directly. A malformed or truncated response raises `JSONDecodeError`, `TypeError`, or `KeyError` and, per H-2, produces no user-visible outcome.

- **Fix:** a Pydantic response model with defaults, plus validation at the boundary.

#### H-5 · `/health` is a static literal and masks total failure
**File:** `app/main.py:11-13`
**Function/class:** `health_check`

Returns `{"status": "ok", "service": "fastapi"}` unconditionally. It does not check whether the polling task at line 17 is still alive, whether Redis accepts connections, or whether the LLM endpoint is reachable. If polling raises, `main.py:23-24` logs the error and gives up permanently — the bot is dead while health stays green.

- **Fix:** track task liveness and probe Redis; return 503 on failure.

#### H-6 · No graceful shutdown, deprecated startup hook
**File:** `app/main.py:15-18`
**Function/class:** `on_startup`, `start_polling`

`asyncio.create_task(start_polling())` fires without retaining a handle and without registering shutdown; `dp.stop_polling()` is never called, so the poller is not stopped cleanly on SIGTERM and in-flight updates can be lost mid-restart. `@app.on_event("startup")` is deprecated in the pinned FastAPI 0.115.6 in favour of the `lifespan` context manager.

- **Fix:** move to `lifespan`, await startup, and close the dispatcher on shutdown.

#### H-7 · Catch-all handler without a content-type filter
**File:** `app/bot/handlers.py:15`
**Function/class:** `research_handler`

`@dp.message()` is registered with no filter, so it also receives commands other than `/start`, photos, stickers, voice notes, and location payloads. For all non-text messages `message.text` is `None`, and lines 19-22 enqueue a research task with `query=None`, producing an "analyzing…" reply to a photo and a meaningless LLM call.

- **Fix:** add `F.text` and register unknown commands separately.

#### H-8 · Docker Compose cannot run the application
**File:** `docker-compose.yml`

Only `redis` and `postgres` are defined. There is no `bot` service and no `celery worker` service, so the two processes the product actually consists of are not containerized; there is no `Dockerfile` at all. The compose file also omits healthchecks, uses the obsolete top-level `version: '3.8'` key, and does not wire `.env` into the containers.

- **Fix:** add a Dockerfile and `bot` + `worker` services with healthchecks and dependency conditions.

### 6.3 MEDIUM

#### M-1 · Dead code: the entire synthesis stage
**File:** `app/agents/supervisor.py:46-65`
**Function/class:** `Supervisor.generate_final_answer`

Zero call edges in the graph. The task named `process_research` (`research_task.py:7`) performs only question decomposition and never answers the sub-questions it generates. This is the single largest gap between the code's naming and its behaviour.

#### M-2 · No database layer
**Files:** `app/core/config.py:6`, `requirements.txt:6-7`, `docker-compose.yml:10-20`

`DATABASE_URL`, `asyncpg`, `sqlalchemy`, and the `postgres` service are all present and entirely unused. No models, no session management, no migrations, no schema.

#### M-3 · No conversation memory
**File:** `app/agents/supervisor.py:16`
**Function/class:** `Supervisor.analyze_query`

Every message is processed statelessly. `analyze_query` receives one string. A user cannot ask a follow-up question, and there is no history to store.

#### M-4 · Module-level singletons prevent testability
**Files:** `app/agents/supervisor.py:7`, `app/bot/dispatcher.py:6`, `app/tasks/research_task.py:8`

`client` and `bot` are constructed at import time from global settings. There is no dependency injection, no factory, and no seam to substitute a fake, so neither the LLM nor the Telegram transport can be tested without monkeypatching module globals. `Supervisor()` is also re-instantiated per task.

#### M-5 · Unused imports and unused dependencies
**Files:** `app/agents/supervisor.py:1, 4`, `requirements.txt`

`supervisor.py:1` imports `os` and `supervisor.py:4` imports `settings`; both appear exactly once in the file — the import line itself — confirming they are unused. `settings` being unused *is* the direct cause of C-1. In `requirements.txt`, `httpx`, `redis`, `asyncpg`, `sqlalchemy`, and `python-dotenv` have no importing module anywhere in `app/` (the last is also redundant with `pydantic-settings`).

#### M-6 · Configuration fragility
**File:** `app/core/config.py`
**Class:** `Settings`

`env_file = ".env"` at line 10 is resolved by pydantic-settings relative to the process working directory, so the app fails to find its config when launched from anywhere other than the repo root — a guaranteed problem once a container sets a different `WORKDIR`. All four fields are required with no defaults and no validators, so a single missing variable raises `ValidationError` at *import* time of `app.core.config`, which `app/main.py:3` imports during application construction — the process dies with a stack trace instead of a configuration error message. `class Config` is the pydantic-v1 style, still honoured via a deprecation shim in the pinned pydantic-settings 2.7.0.

#### M-7 · Logging is duplicated, unstructured, and leaks user content
**Files:** `app/main.py:7`, `app/bot/handlers.py:7, 17`

`logging.basicConfig(level=logging.INFO)` is called in both modules. All logging uses eager f-string interpolation rather than lazy `%s` args. `handlers.py:17` logs the user's complete message text — user content in logs with no retention policy. There are no correlation or task IDs, no log level discipline, no centralization, and — per H-2 — no logging of task failures at all.

#### M-8 · No rate limiting or cost control
**Files:** `app/bot/handlers.py`, `app/core/celery_app.py:11-17`

No per-user throttle, no global concurrency cap, no queue-depth guard. Any user can enqueue an unbounded number of billable LLM tasks, and there is no flood-control handling for Telegram 429 responses on the send path.

#### M-9 · Celery result backend misused
**File:** `app/core/celery_app.py:6-7`

`backend` points at the same Redis URL as the broker, and `task_ignore_result` is not set. No task result is ever read, so Redis accumulates result keys indefinitely with no TTL.

#### M-10 · Untyped boundary contract
**File:** `app/agents/supervisor.py:16, 46`
**Function/class:** `Supervisor.analyze_query`, `Supervisor.generate_final_answer`

`analyze_query` is annotated `-> dict` and `generate_final_answer` takes a bare `list`. There is no Pydantic model, no schema module, and no validation anywhere — the entire contract between the agent and the task is an unvalidated dictionary. This is inconsistent with the otherwise reasonable use of Pydantic in `config.py`.

### 6.4 LOW

| ID | File | Issue |
|---|---|---|
| L-1 | `.python-version`, `__pycache__/` | Environment drift: `.python-version` pins `3.12.0` and `venv/lib/python3.12` matches, but `__pycache__` contains `cpython-314` artifacts, indicating the code has also been run under 3.14 |
| L-2 | `app/bot/handlers.py:12-13, 18`, `app/tasks/research_task.py:12` | User-facing strings are hardcoded Persian literals. No i18n layer, which will need replacing for the planned SaaS |
| L-3 | `app/main.py:9`, `app/bot/handlers.py:12`, `app/core/celery_app.py:5` | Naming is inconsistent across layers: the FastAPI app is `"Research Bot Core"`, the user-facing greeting says `"DeepVoyager"`, and the Celery app is `"research_bot"` |
| L-4 | `app/agents/supervisor.py:54` | `chr(10)` used inside an f-string to work around nested-quote escaping — a readability smell indicating prompt templates need a proper abstraction |
| L-5 | `app/core/celery_app.py:15` | `timezone="Asia/Tehran"` hardcoded rather than configured |
| L-6 | repo root | No `.dockerignore`, `Makefile`, `README`, `CONTRIBUTING`, `pyproject.toml`, editor/formatter config, or dependency hashes |
| L-7 | `app/bot/dispatcher.py:10` | A `Dispatcher` is constructed that the Celery worker also imports and instantiates (via `research_task.py:3`) but never uses |
| L-8 | `app/bot/handlers.py:11, 17` | `message.from_user.id` accessed without a `None` guard. Low risk for ordinary `Message` updates, but the code has no defensive posture anywhere |

---

## 7. Code Quality Findings

**Project organization.** The directory layout is conventional and, importantly, *correct in intent*: `bot/` for Telegram, `core/` for config and Celery, `agents/` for LLM logic, `tasks/` for background work, with a thin `main.py`. The problem is that the layering is nominal rather than real — there is no service layer, so `research_task.py` composes the agent, formats the response, manages an event loop, and talks to Telegram all in 18 lines. `app/bot/` should arguably be `app/telegram/`, but that is cosmetic next to the missing middle tier.

**Separation of concerns.** Poor in the places that matter. Presentation formatting (`research_task.py:12`), transport lifecycle (`research_task.py:15-18`), and LLM invocation (`supervisor.py`) are interleaved in one function. A formatting change and a transport change require editing the same 4 lines.

**SOLID.** SRP is violated in `process_research` and, more seriously, in AiohttpSession lifecycle management inside it. OCP is nominally satisfied by the `Supervisor` class but defeated by the hardcoded `self.model` (`supervisor.py:14`) — adding a provider requires editing the class rather than extending it. DIP is not attempted: no module depends on an abstraction, everything reaches for a module-level global. LSP and ISP have no applicable surface.

**Type safety.** Partial and inconsistent. Annotations exist on the `Settings` fields and on `Supervisor` method signatures, but the most important boundary — the LLM response — is `dict`. There is no `py.typed`, no mypy config, no runtime validation, and no CI gate. Type hints here are documentation, not enforcement.

**Naming.** Reasonable at the local level (`analyze_query`, `process_research`, `on_startup`) but inconsistent at the product level (L-3), and actively misleading in one place: `process_research` performs no research (M-1).

**Duplication.** Minimal, which is a genuine positive — there is very little code to duplicate. The one real instance is the duplicated `logging.basicConfig` (M-7).

**Dependency management.** Weak. Ten pinned direct dependencies, of which five are entirely unused (M-5) and one required at runtime is missing (C-2). `requirements.txt` is a flat list with no separation of direct/transitive/dev dependencies, no hashes, and no lockfile. The venv contains packages installed outside the declared set, which is how C-2 went unnoticed.

**Configuration management.** A single `Settings` class is the right shape, but as noted in M-6 it is CWD-dependent, has no defaults or validation, uses the legacy `class Config` form, and is bypassed entirely by the one place that actually needs a secret (C-1). Two competing sources of truth exist for the provider key: `.env` and a source literal, and the code uses the wrong one.

**Logging.** See M-7. The one real gap is that the background task — where essentially all failure modes live — produces no logs at all.

**Testability.** Effectively zero. Module-level singletons (M-4) mean both external boundaries must be monkeypatched; there are no seams, no fixtures, and no test runner in the dependency set.

**Maintainability.** Low, and the limiting factor is not style but that the system cannot currently complete a request. Beyond that, the three genuine debt items are: a task named for a feature that does not exist (M-1), an entire provisioned data tier that is never touched (M-2), and secrets/config handled in two places at once (C-1, M-6).

---

## 8. Testing Gaps

**Existing tests: none.** No `test_` file, no `tests/` directory, no `conftest.py`, and no test framework in `requirements.txt` (no `pytest`, `pytest-asyncio`, `respx`, `fakeredis`, or `testcontainers`). Confirmed by keyword scan for `pytest`, `unittest`, and `test_` across `app/`: no matches.

| Area | Test coverage | Gap |
|---|---|---|
| Telegram handlers | none | `start_command`, `research_handler` completely untested |
| Dispatcher / bot construction | none | Parse-mode and token wiring untested |
| Celery task | none | `process_research` untested, including the event-loop defect in C-3 |
| LLM / prompt | none | `analyze_query` and `generate_final_answer` untested |
| Config loading | none | No verification that required vars resolve |
| Database | none | No layer to test |
| Health endpoint | none | Trivial but unverified |
| Docker / compose | none | No build or smoke test |

### Critical functionality with no tests

In rough order of blast radius:

1. `app/tasks/research_task.py:process_research` — the entire request-completion path, including the event-loop lifecycle flagged as C-3. This is the single highest-value test in the project, and it is a pure unit test with a fake `bot` and a fake LLM client.
2. `app/agents/supervisor.py:Supervisor.analyze_query` — LLM response parsing, including malformed JSON, missing keys, and null content (H-4).
3. `app/bot/handlers.py:research_handler` — filter behaviour, `from_user` handling, and the group `chat_id` bug (H-1, H-7).
4. `app/core/config.py:Settings` — that all four variables resolve, guarding against the CWD and missing-var failure modes in M-6.
5. Message formatting and the 4096/HTML constraints (C-4) — a pure function once extracted.
6. An end-to-end smoke test asserting that a synthetic update produces exactly one Telegram reply.

There is no CI configuration, so nothing prevents regressions even after tests are added.

---

## 9. Roadmap Status

| Phase | Status | Completed | Partial | Blockers / dependencies |
|---|---|---|---|---|
| **1 — Core Infrastructure** | 🟡 Partial | FastAPI app, aiogram polling, Celery + Redis, config module, compose for Redis/Postgres | Logging (basic), health endpoint (non-functional) | C-1, C-2, C-3, C-4, H-5, H-6, H-8, M-6, M-7; no Dockerfile, no tests, no CI, no migrations |
| **2 — AI Chat MVP** | ❌ Not started | One LLM call returns text to Telegram | — | No chat semantics, no context/memory, no streaming, no model selection, no fallback, no rate limiting, no `/help`, no error UX. Stateless single-turn decomposition is not a chat MVP |
| **3 — Research Engine** | ❌ Not started | `generate_final_answer` stub exists but is unwired (M-1) | — | No search, no fetch, no extraction, no cleaning, no ranking, no dedup, no summarization, no citations, no reports. Blocked on Phase 2 hardening and the Phase 1 data tier |
| **4 — File Intelligence** | ❌ Not started | — | — | No download, parsing, or upload handling of any kind. Blocked on Phase 3 pipeline and an object storage decision |
| **5 — Research Workspace** | ❌ Not started | — | — | No database layer at all (M-2). Blocked on Phase 1 migrations and Phase 2/3 schemas |
| **6 — Agent System** | 🟡 Sketched | Naming and file placement (`app/agents/supervisor.py`) | A two-stage Supervisor design is *implied* by the code | No orchestration, no tool registry, no planner/executor split, no agent loop, no state machine. Stage 2 is dead code. Blocked on Phase 3 tools to orchestrate |
| **7 — Premium Features** | ❌ Not started | — | — | No user accounts, no tiers, no quotas. Blocked on Phase 5 |
| **8 — SaaS / Web Dashboard** | ❌ Not started | — | — | No auth, no multi-tenancy, no REST API (one health route), no frontend, no i18n. Blocked on Phases 1–5 |
| **9 — Monetization** | ❌ Not started | — | — | No billing, no payment integration, no entitlement model. Blocked on Phase 7 |

**Summary:** the repository has reached roughly the end of Phase 1 *in intent* and about 5% of Phase 2. Phases 3–9 have no implementation, and the Phase 6 naming is aspirational rather than functional.

---

## 10. Recommended Next Steps

Sequenced by dependency, not by severity alone.

1. **Make the current path actually work, then prove it with a test.** Fix C-1 through C-4 and H-1/H-2 as a single change, and add one unit test for `process_research` that runs the task twice in the same process — that test alone would have caught the event-loop defect. Until this is done, no feature work can be validated, because there is no reliable signal that a request completes.

2. **Establish the data tier and the configuration contract.** Add SQLAlchemy async models plus Alembic for users, conversations, and messages (M-2), wire `DATABASE_URL` into a real session factory, add health checks that probe Redis and Postgres (H-5), and add a Dockerfile plus `bot`/`worker` compose services (H-8). This unblocks Phase 5 and makes every later feature persistable.

3. **Turn the skeleton into an actual chat MVP before any research work.** Add a real service layer with dependency injection, conversation memory, `/help` and error-feedback commands, `F.text` filtering, per-user rate limiting, and model selection with a fallback (Phase 2). This produces the first genuinely testable, user-facing capability — and only after it is solid does wiring search/fetch/summarize/cite into the existing `generate_final_answer` stub represent progress rather than more scaffolding.

---

## 11. P0/P1/P2/P3 Priority List

### P0 — Critical: security and non-functional

| # | Task | Why | Files | Depends on |
|---|---|---|---|---|
| P0-1 | Remove the hardcoded key; read `settings.GROQ_API_KEY`; add `openai` to `requirements.txt`; **rotate the exposed key and the `.env` bot token** | C-1, C-2 — plaintext credential in source, wrong key for the endpoint, and the app cannot start in a clean environment | `app/agents/supervisor.py:1-10`, `requirements.txt` | — |
| P0-2 | Add `.gitignore` (`.env`, `venv/`, `__pycache__/`) and `.env.example` | One `git init && git add .` publishes a live bot token, an API key, and the whole venv | repo root | — |
| P0-3 | Fix the event-loop lifecycle: one long-lived loop per worker process; close the aiogram session on shutdown | C-3 — the core feature succeeds roughly once per worker process and leaks sockets | `app/tasks/research_task.py:15-18`, `app/bot/dispatcher.py:6` | P0-1 |
| P0-4 | Resolve the parse-mode conflict: one parse mode, `html.escape` or `parse_mode=None`, and `text.split_text` for the 4096 limit | C-4 — malformed or over-long LLM output silently drops every reply | `app/bot/dispatcher.py:8`, `app/tasks/research_task.py:12,17` | — |
| P0-5 | Add error handling across the request path: `@dp.errors()`, `try/except` in the task, and a terminal user-facing failure message; add `timeout`, `max_tokens`, and bounded `autoretry_for` with `task_time_limit` | H-2, H-3 — every failure mode is currently silent and the user is left hanging; unbounded tokens are unbounded cost | `app/bot/handlers.py`, `app/tasks/research_task.py`, `app/agents/supervisor.py:36,59`, `app/core/celery_app.py:11-17` | P0-3 |

### P1 — Architecture and correctness

| # | Task | Why | Files | Depends on |
|---|---|---|---|---|
| P1-1 | Pass `chat_id=message.chat.id`; add `F.text` filter and a `from_user` guard | H-1, H-7, L-8 — the bot answers into the void in any group and mis-handles every non-text message | `app/bot/handlers.py:15-22` | — |
| P1-2 | Make `/health` real: track the polling task, probe Redis and Postgres, return 503 on failure; move startup to `lifespan` and add graceful shutdown | H-5, H-6 — a dead bot currently reports healthy; shutdown loses in-flight updates | `app/main.py:11-24` | — |
| P1-3 | Introduce a service layer with dependency injection; stop constructing `bot` and `client` at import time | M-4 — nothing is testable, and configuration has two competing sources of truth | `app/bot/dispatcher.py`, `app/agents/supervisor.py:7`, `app/tasks/research_task.py:8` | P0-5 |
| P1-4 | Add a Pydantic schema for the LLM response and validate at the boundary; remove the dead `generate_final_answer` or wire it in | H-4, M-1, M-10 — the agent/task contract is an unvalidated dict, and the synthesis stage does not exist | `app/agents/supervisor.py:16,43,46-65`, `app/tasks/research_task.py:9,12` | P1-3 |
| P1-5 | Add per-user rate limiting, Celery concurrency and prefetch caps, `task_ignore_result`, and flood-control handling | M-8, M-9 — unbounded billable work from any single user; unbounded Redis growth | `app/bot/handlers.py`, `app/core/celery_app.py:11-17` | P0-5 |
| P1-6 | Add Dockerfile plus `bot` and `worker` compose services with healthchecks; stop publishing Redis/Postgres with default credentials | H-8 — the application cannot be deployed as written | `docker-compose.yml`, new `Dockerfile` | P1-2 |
| P1-7 | Clean dependencies: remove the five unused packages, prune unused imports | M-5 — `requirements.txt` misrepresents the system and hid the missing `openai` | `requirements.txt`, `app/agents/supervisor.py:1,4` | P0-1 |

### P2 — MVP requirements (Phase 2)

| # | Task | Why | Files | Depends on |
|---|---|---|---|---|
| P2-1 | Add the data tier: SQLAlchemy async models + Alembic for users, conversations, messages; wire `DATABASE_URL` into a session factory | M-2 — a fully provisioned but entirely unused Postgres; blocks every later phase | new `app/db/`, new `alembic/` | P1-3 |
| P2-2 | Implement conversation memory and multi-turn context with token budgeting and truncation | M-3 and Phase 2's core requirement — the bot is currently stateless | `app/agents/`, new service layer | P2-1 |
| P2-3 | Add `/help`, unknown-command handling, and user feedback on task state (queued / running / failed) | H-2, H-7 — basic UX is missing entirely | `app/bot/handlers.py` | P1-1, P0-5 |
| P2-4 | Add model selection with a fallback chain, plus streaming for long answers | Phase 2 requirements; absent today | `app/agents/supervisor.py:14` | P1-3 |
| P2-5 | Fix configuration robustness: absolute `env_file` path, defaults, validators, `model_config`, and fail-fast with a readable message | M-6 — CWD-dependent config that crashes at import time | `app/core/config.py` | — |
| P2-6 | Consolidate logging: single `basicConfig`, lazy `%s` args, stop logging full user text, add task/correlation IDs, log task failures | M-7 — the background task, where all failures live, logs nothing | `app/main.py:7`, `app/bot/handlers.py:7,17`, `app/tasks/research_task.py` | P1-3 |
| P2-7 | Stand up the test harness: `pytest`, `pytest-asyncio`, `respx`, `fakeredis`; first test is `process_research` executed twice in one process | No tests exist; this is the regression net for every fix above | new `tests/`, `requirements.txt` | P0-3, P1-3 |
| P2-8 | Add CI running lint, type-check, and tests | Nothing prevents regressions | new CI config | P2-7 |

### P3 — Future improvements

| # | Task | Why | Files | Depends on |
|---|---|---|---|---|
| P3-1 | Build the research pipeline: web search → URL fetch → HTML extraction → cleaning → dedup → ranking | Phase 3; no primitive exists yet | new `app/research/` | P2-7 |
| P3-2 | Wire multi-source synthesis and citation generation into the existing `generate_final_answer` stub | Closes the largest naming/behaviour gap in the codebase | `app/agents/supervisor.py:46-65` | P3-1 |
| P3-3 | Structured report generation with persisted research and source records | Phase 3 deliverable; requires the schema from P2-1 | new `app/research/`, `app/db/models` | P2-1, P3-2 |
| P3-4 | File/document ingestion (PDF, DOCX) with size and type limits | Phase 4 | new `app/documents/` | P2-1, P3-1 |
| P3-5 | i18n layer replacing hardcoded Persian strings | Required before any SaaS surface | `app/bot/handlers.py:12,18`, `app/tasks/research_task.py:12` | P2-3 |
| P3-6 | Structured logging, metrics, and tracing | No observability beyond stdout | new | P2-6 |
| P3-7 | Agent orchestration: tool registry and planner/executor loop over the research tools | Phase 6; the `Supervisor` naming currently implies a capability that does not exist | `app/agents/` | P3-2 |
| P3-8 | `pyproject.toml`, formatter/linter config, `Makefile`, `README`, `.dockerignore`; remove obsolete compose `version` key and reconcile naming | General maintainability debt | repo root, `docker-compose.yml:1` | P2-8 |
| P3-9 | Multi-tenancy, auth, REST API, and web dashboard | Phases 7–8 | new | P2-1, P2-5, P3-5 |

---

## 12. Overall Project Readiness

| Area | Status | Evidence | Problems |
|---|---|---|---|
| Telegram | ⚠️ | `dispatcher.py`, `handlers.py`, `main.py` | Polling only; no error handler; wrong `chat_id` in groups; no content filter; parse-mode conflict; no graceful shutdown; no rate limiting; no session handling |
| Backend | ⚠️ | `main.py` (24 lines), `celery_app.py` | No service layer; no DI; module-level singletons; broken worker async model; `/health` non-functional; deprecated startup hook |
| Database | ❌ | `config.py:6`, `requirements.txt:6-7`, `docker-compose.yml:10-20` | Zero code. No models, tables, migrations, sessions, or usage of any kind |
| LLM | ⚠️ | `supervisor.py` | Hardcoded wrong key; missing dependency; no timeout/retries/`max_tokens`; no system role; no schema validation; no model selection or fallback; no streaming; no context management |
| Web Search | ❌ | — | Nothing exists. No client, no provider, no configuration |
| Research Engine | ❌ | `supervisor.py:46-65` (dead code) | Only an unwired stub. No fetch, extraction, cleaning, dedup, ranking, summarization, citation, or reporting |
| File Processing | ❌ | — | Nothing exists |
| Authentication | ❌ | — | No auth layer, no user records, no allowlist, no tenant isolation |
| Error Handling | ❌ | `main.py:20-23` (sole instance) | One bare `except` in the whole codebase. No handler-level protection, no task-level protection, no user-facing failure path, no 429 handling |
| Testing | ❌ | — | Zero tests, zero test framework, no CI |
| Docker | 🟡 | `docker-compose.yml` | Redis + Postgres only; no Dockerfile; no app or worker service; no healthchecks; default credentials; obsolete `version` key |
| Logging | ⚠️ | `main.py:7`, `handlers.py:7,17` | Duplicated init; f-strings; full user text logged; no correlation IDs; background task logs nothing on failure; no centralization |
| **Overall** | **❌ Not operational** | 8 Python files, ~150 statements | Core request path fails after the first message per worker; no data layer; no tests; secrets in source |

### Readiness against the stated goal

The project can demonstrate "receive a Telegram message and call an LLM" once — after that it breaks (C-3), and it cannot do so at all in a clean environment (C-2) or without exposing a credential (C-1). Of the nine product goals, one is partially present, one is a dead stub, and seven are entirely absent. This is a credible Phase 1 scaffold with a Phase 2 sketch in it, and it is roughly two to three weeks of focused work from a demonstrable AI chat MVP — but that work is entirely Phase 1 hardening and Phase 2 basics. Nothing in Phases 3–9 has been started.

### Explicitly NOT VERIFIED

**NOT VERIFIED — insufficient evidence in repository.** The following could not be determined from the codebase and are stated as unknowns rather than findings:

- Whether the credentials in `.env` and `app/agents/supervisor.py:8` are currently active. No network calls were performed.
- Whether secrets were ever committed. No `.git` directory exists, so there is no history to inspect.
- The intended deployment path. No Dockerfile, CI config, or run documentation exists.
- The exact runtime exception text in C-3. The mechanism is derived from the installed aiogram source at `aiogram/client/session/aiohttp.py:133-146` and `aiogram/client/bot.py:288-293`, but the code was not executed.

---

## 13. What To Build Next

**1. Make one request survive end-to-end, and lock it with a test.**
Fix the five P0s as a single change: move the key into `settings`, add `openai` to `requirements.txt`, rotate the exposed credentials, add `.gitignore`/`.env.example`, replace the per-task event loop in `research_task.py:15-18` with one long-lived loop per worker process, and resolve the HTML/Markdown and 4096-character conflict. Then write the project's first test — call `process_research` twice in the same process and assert both produce a reply. That single test is what proves the core path works, and it would have caught the defect that currently makes the bot unusable.

**2. Add the data tier and real health checks.**
The Postgres container, `DATABASE_URL`, and `asyncpg`/`sqlalchemy` are already provisioned and completely unused. Introduce SQLAlchemy async models plus Alembic for users, conversations, and messages, and make `/health` actually probe Redis, Postgres, and the polling task so the system can report its own state. This unblocks conversation memory and every downstream phase.

**3. Extract a service layer and turn the skeleton into a real chat MVP.**
Stop building `bot` and the LLM client at import time; inject them instead. On top of that, add conversation memory with token budgeting, `/help`, a real error-feedback path, and model selection with a fallback. Only after this is solid is it worth wiring the search/fetch/summarize/cite pipeline into the existing `generate_final_answer` stub — otherwise the research engine will inherit every structural problem now present in the task layer.

---

## Appendix A — Repository Inventory

Complete file listing, excluding `venv/` and `__pycache__/`:

```
.env                          6 lines    secrets: BOT_TOKEN, REDIS_URL, DATABASE_URL, GROQ_API_KEY
.python-version               1 line     3.12.0
docker-compose.yml           23 lines    redis + postgres only
requirements.txt             10 lines    10 pinned deps; openai MISSING; 5 unused
app/__init__.py               0 lines
app/main.py                  24 lines    FastAPI app, /health, polling bootstrap
app/agents/__init__.py        0 lines
app/agents/supervisor.py     65 lines    LLM client (hardcoded key), Supervisor class
app/bot/__init__.py           0 lines
app/bot/dispatcher.py        10 lines    Bot + Dispatcher singletons
app/bot/handlers.py          22 lines    /start + catch-all message handler
app/core/__init__.py          0 lines
app/core/celery_app.py       17 lines    Celery app, broker/backend config
app/core/config.py           12 lines    Settings (4 required vars)
app/tasks/__init__.py         0 lines
app/tasks/research_task.py   18 lines    Celery task, event loop, Telegram send
```

**Totals:** 12 Python files, 7 containing real code, ~150 statements.

**Absent:** `Dockerfile`, `docker-compose` app/worker services, `.gitignore`, `.env.example`, `.dockerignore`, `README`, `CONTRIBUTING`, `Makefile`, `Procfile`, `pyproject.toml`, `alembic.ini`, CI config, any `tests/` directory, any database module, any schema module, any research module.

**Dependency status (`requirements.txt`):**

| Package | Used in `app/`? |
|---|---|
| `fastapi==0.115.6` | ✅ `app/main.py` |
| `uvicorn[standard]==0.34.0` | ⚠️ runtime only, never imported |
| `aiogram==3.17.0` | ✅ `app/bot/` |
| `celery==5.4.0` | ✅ `app/core/celery_app.py`, `app/tasks/` |
| `redis==5.2.0` | ❌ unused (broker accessed via Celery/kombu) |
| `asyncpg==0.30.0` | ❌ unused |
| `sqlalchemy==2.0.36` | ❌ unused |
| `python-dotenv==1.0.1` | ❌ unused (redundant with `pydantic-settings`) |
| `httpx==0.28.1` | ❌ unused |
| `pydantic-settings==2.7.0` | ✅ `app/core/config.py` |
| **`openai`** | **✅ imported at `app/agents/supervisor.py:3` — MISSING from requirements.txt** |

---

## Appendix B — Verified Call Graph

Extracted from the knowledge graph. Total: 80 nodes, 194 edges, 12 Python files, 1 YAML file. Coverage report: `parse_partial: 0`, `skipped: 0`.

### Call edges

```
app.main.on_startup                    ──►  app.main.start_polling
app.tasks.research_task.process_research ──► app.agents.supervisor.Supervisor
app.tasks.research_task.process_research ──► app.agents.supervisor.Supervisor.analyze_query
app.core.config                        ──►  app.core.config.Settings
app.main                               ──►  builtins.dict.get          [health_check]
```

`Supervisor.generate_final_answer` — **0 inbound, 0 outbound. Dead code.**

Note: the edge `research_handler → process_research` is not represented in the graph because `.delay()` is a dynamically resolved attribute call. The edge is confirmed by direct source reading of `app/bot/handlers.py:19-22`.

### Declared dependency edges

```
requirements.txt ──► fastapi, uvicorn, aiogram, celery, redis,
                     asyncpg, sqlalchemy, python-dotenv, httpx, pydantic-settings
```

### Routes

| Method | Path | Handler |
|---|---|---|
| GET | `/health` | `app.main.health_check` |

There are no other HTTP routes. There is no webhook endpoint.

### Clusters detected

| Cluster | Members | Cohesion | Representative nodes |
|---|---|---|---|
| `app` | 3 | 1.0 | `process_research`, `Supervisor`, `analyze_query` |
| `app` | 2 | 1.0 | `on_startup`, `start_polling` |

The graph's own clustering confirms the architecture is two disconnected islands — the Telegram/FastAPI process and the Celery/agent process — joined only by a Redis queue. There is no shared service layer, which is the structural root cause of most findings in section 6.

---

### Update — 2026-10-07, after Phase 3 P3.3 (Fetcher + SSRF Security Boundary)
* Phase 3 completed the SSRF boundary for untrusted URL fetching. The implementation provides a provider-independent fetch port (`SafeFetcher`) with a security boundary that:
    * Resolves hostname and validates every returned IPv4/IPv6 address against explicit blocklists (RFC1918, loopback, link-local, multicast, reserved, documentation ranges in both families)
    * Prevents DNS rebinding by connecting to the validated IP address literal while preserving hostname for Host header and TLS SNI
    * Re-validates after every redirect hop, rejecting any hop that fails the SSRF policy
    * Enforces response size limits on the decoded stream (not Content-Length) with compression ratio protection
    * Uses explicit connect/read/total timeouts
    * Validates content-type and content-encoding against an allowlist
    * Provides a typed error taxonomy where SSRF violations are never retryable
    * Never leaks provider credentials or authorization headers to fetched destinations
* The actual network connection is handled by `_PinnedTransport` which pins the connect target to the validated IP address, eliminating the DNS-rebinding TOCTOU window
* All provider-independent contracts live in `app/domain/research/`: `FetchRequest` and `FetchedSource` (raw bytes + metadata, no extraction or text decoding)

---

*End of audit. This document was produced under read-only constraints: no files in the application were modified, created, or deleted; no packages were installed; no migrations were run; no configuration or environment variables were changed; no commits were made. The only filesystem change is the creation of this `docs/` directory and this file.*

---

## Update — 2026-10-07 (Phase 3 P3.4 — Extraction + Persian Fixtures)

P3.4 completed: extraction domain (`app/domain/research/extraction.py` — `ExtractedContent`, `ExtractionStatus`, `ContentExtraction` protocol, typed error family), infrastructure adapter (`app/infrastructure/extract/` — `TrafilaturaExtractor` with ZWNJ-preserving extraction, NFC normalization, content-type dispatch, fixture coverage for English/Persian/mixed/adversarial), config (`EXTRACT_*` settings), tests (`tests/test_research_extraction.py` — 21 passed). No P3.5+ functionality added. No new migration.

---

## Update — 2026-10-08 (Phase 3 P3.8 — Synthesis + Citations)

P3.8 complete. Six new modules and one migration; the five-table research schema is
unchanged.

**Domain** — `app/domain/research/synthesis.py` holds the whole contract:
`CitationEntry` (one persisted citation resolved through its chain), `CitationRegistry`
(the authority for what a citation means, scoped by `claims.run_id`),
`SynthesisContext`/`plan_synthesis_context` (the deterministic evidence budget),
`SynthesisInput`, `SynthesisProposal` (the model's output), `VerifiedCitation` /
`VerifiedSection` / `VerifiedContradiction` / `SynthesizedAnswer` (the trusted result),
and `verify_synthesis` — the only path from a proposal to an answer.

**Trust boundary.** The model proposes citation *identifiers*; it never establishes that
one is valid. The proposal schema is closed and carries no `url`, `quote`, `source`,
`confidence`, or `verified` field at either level, and unknown keys are refused. Every
field of every trusted citation is copied from a `CitationEntry` the repository built
from persisted rows. `SynthesizedAnswer` and the three trusted value types are
constructible only through `verify_synthesis` (a module-private token guard that closes
`Model(...)` and `model_validate(...)`), and `SynthesizedAnswer.from_payload` re-verifies
a stored answer against the registry, so a hand-edited `research_runs.synthesis` row
cannot be read back as a trustworthy answer. Any unresolvable reference refuses the whole
answer rather than being stripped — AD-041, a deliberate narrowing of § 15.7.

**Contradictions are preserved, never reconciled.** A model-reported conflict keeps both
sides with all their citations. `claims.status='contradicted'` is deliberately unused:
marking a losing side *is* silent reconciliation (FR-34).

**Ports and adapters** — `SynthesisProvider` (runtime-checkable, one method) with one
adapter, `LLMSynthesizer`, over Phase 2's existing `LLMProvider`. No second LLM client,
no SDK import, no retry, no tool surface. The prompt
(`app/infrastructure/synthesis/prompts.py`, `cross-source-synthesis-v1`) takes only
three numbers in its system message; untrusted evidence is the user-role message inside
`<<<RESEARCH_EVIDENCE>>>` delimiters, and **no source URL is placed in the prompt** —
the URL reaches the answer from the `sources` row in the application.

**Application** — `SynthesisService.synthesize_run` owns the ordering: read the run
(ownership first), read the citation registry, bound the evidence, call the provider,
verify, then persist. It holds no session across the model call and holds no fetcher,
searcher, or browser.

**Persistence** — `research_runs.synthesis` (JSONB, nullable) plus `final_answer` and
`status='completed'`, written in one transaction; `research_runs.final_answer` carries
plain prose with no citation markers, because rendering is presentation. `alembic check`
reports no drift at head `0004_research_synthesis`.

**Verification** — 340 targeted tests, including adversarial prompt-injection coverage in
English, Persian, and mixed content, and five defect-injection proofs. Full suite
1833 passed / 20 failed *(figure at P3.8 — see the 2026-10-09 update below for the current baseline)*: the 20 are pre-existing P3.3/P3.4 defects, unchanged.
`make verify` still fails on those. **The repository is not green and is not claimed to
be.** Not verified against a live provider.

---

## Update — 2026-10-08 (Phase 3 P3.9 — Read-only audit + contract freeze)

**No code changed.** P3.9 was a read-only stage. No orchestration, no `/research`,
no chunk producer, no migration, no Celery configuration change, no dependency
change. Production code, tests, and schema are unchanged, and the failed baseline
(`1833 passed / 20 failed`, `ruff` 2, `mypy` 4 — all in the two P3.3 files) is
carried forward unchanged. Status: **READY WITH PREREQUISITES**.

**Canonical audit.** `docs/audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md` § R is the
single Phase 3 audit's P3.9 section, appended. The legacy
`PHASE-03_P3-0_CONTRACT_FREEZE_AUDIT.md` remains as history; no second Phase 3
audit was created. Reconciliation of that legacy file's open items is in § R.20.

**Decisions recorded** — AD-043 (research execution is one Celery task per run with
every stage inside it, on the worker's long-lived loop; chained stage tasks are
unavailable because `task_ignore_result=True` and nothing reads results) and
AD-044 (the orchestrator owns `completed` / `partial` / `failed`, extending
AD-042 which correctly refused to decide `partial` from synthesis). AD-035's
deadline *value* remains open.

**Gaps found by the audit, all prerequisites for P3.9 implementation** (audit
§ R.6, § R.9, § R.5, § R.18):

1. **No `SourceChunk` producer.** `uow.source_chunks.add` has zero callers in
   `app/` *and* `tests/`. `app/services/formatting.py` splits outgoing Telegram
   messages and `plan_chunk_windows` packs already-persisted passages into prompt
   windows — neither writes `source_chunks`. This is the blocker that keeps the
   page-to-answer path unexercised.
2. **`SourceRepository` cannot persist P3.5's output.** No writer exists for
   `rank`, `quality_score`, `content_hash`, or `duplicate_of_id`. The consequence
   is not cosmetic: `list_citable_for_run` orders by `sources.rank` and
   `plan_synthesis_context` budgets on that order, so losing `rank` silently
   de-ranks the evidence offered to the model. `tests/research_support.py` writes
   `rank` with a raw `UPDATE` and says so in a comment.
3. **No run-state writers.** Nothing writes `running` or `partial`, and
   `ResearchRunRepositoryImpl.set_status` is the only research write that is not
   ownership-scoped.
4. **No research composition root.** `build_container` wires `ChatService`,
   `DeliveryService`, `RateLimiter`, and the `Database` only. Every research stage
   factory is called exclusively from `tests/`.
5. **Execution-model risk.** Inherited limits are 100 s soft / 120 s hard. With
   `task_acks_late=True` and `task_reject_on_worker_lost=True`, a task killed by
   the hard limit is **redelivered and re-executed from the start**, so
   idempotency is load-bearing. `SEARCH_API_KEY` is absent from `.env`, so
   `Settings.search_enabled` is `False` here — a configuration state, not a defect.

**Account deletion is complete and frozen** (audit § R.16): `users → research_runs →
{sources, claims} → {source_chunks, citations}` are all `ON DELETE CASCADE`, removed
in one `DELETE FROM users` inside one unit of work, tested against real PostgreSQL
with a cross-user isolation assertion. Not a P3.9 prerequisite.

---

## Update — 2026-10-09 (Phase 3 P3.9-C — B5 / B6 operational decisions)

**No code changed.** P3.9-C was a decision-and-documentation stage. No
orchestration, no `/research` route, no rate limiter, no timeout handling, no
Celery configuration change, no migration, no schema change, no dependency
change, no test change. Production code and tests are byte-identical to the
P3.9-B state.

**Decisions recorded** — AD-045 (deadline mechanism), AD-046 (`/research`
question length), AD-047 (research rate-limit policy). The Architecture Decision
Log in `PROJECT_PLAN.md` is the authority; the evidence is canonical audit § S.

**AD-045 — the deadline is cooperative, not signal-based.** This is the one with
architectural consequence, because it changes how a research task may be written.
Celery raises the soft time limit as a POSIX signal handled in the worker process
(`billiard/pool.py:227-228`). This repository's task body is **synchronous** and
blocks in `loop.run_until_complete()` (`app/infrastructure/asyncio_runtime.py:70-82`),
so the exception is raised **in the awaiting frame, not inside the coroutine**.
Verified by execution against the repository's own async bridge: on a soft
timeout the in-flight `SqlAlchemyUnitOfWork.__aexit__`
(`app/db/unit_of_work.py:54-72`) never runs — no commit, no rollback, no session
close — and the coroutine is left `PENDING`. The control case (`asyncio.timeout`,
already used by the fetch stage) unwinds correctly.

Consequences for anyone implementing P3.9:

- The deadline must be **in-coroutine**, checked at stage boundaries and before
  provider calls. This is the only form in which "terminate with what it has"
  (FR-44) is actually executable.
- `SoftTimeLimitExceeded` must be caught **and must not reach the retry path** —
  `research_task.py:92` defaults any exception to `getattr(exc, "retryable", True)`,
  so an uncaught soft timeout would re-execute the whole run.
- The existing per-stage transaction discipline already supports this: planning,
  chunking, evidence, and synthesis each close their read transaction before any
  provider call and commit separately. Chat cannot offer the same property, since
  its turn is one transaction.
- **The numeric deadline is BLOCKED.** No measurement exists; `SEARCH_API_KEY` is
  absent so no run can execute; and no bound exists on candidates fetched per run.
- **B7 (idempotency) becomes a prerequisite of B5**, not a parallel concern: with
  `task_acks_late` + `task_reject_on_worker_lost`, a hard kill redelivers and
  re-executes, and produces no partial status and no terminal message at all.

**AD-046 — `/research` question length is the existing 2000.** `PLAN_MAX_QUESTION_CHARS`
(`config.py:241`), enforced at `planner.py:112-119` before any provider call. aiogram's
`Command` filter imposes no cap of its own (a 5000-character argument arrives intact),
and Telegram's 4096 governs *outgoing* delivery only. Accepting 4000 at intake would
create a run, a queue message, and a worker execution before rejecting an input —
the opposite of § 15.9's "no run created". Token budgeting was measured and does
**not** discriminate between the two values; that is recorded rather than dressed up.

**AD-047 — research gets a dedicated rate-limit allowance**, separate from chat's,
keyed by the same Telegram user id. **Numeric values BLOCKED.** Separately, and as a
finding rather than a decision: **the global in-flight counter never returns to zero
in the API process.** `check()` runs in the API process (`handlers.py:226`) and
`release()` in the Celery worker process (`research_task.py:111`) against its own
container — `docker-compose.yml` runs them as separate services. Verified by
execution: after 8 lifetime messages the API refuses every user permanently with
`global_capacity`. This is **pre-existing Phase 2 behaviour**, invisible to the tests
because they drive handler and task in one process, and it is **reported, not fixed** —
recorded as prerequisite B6-P4.

**Baseline.** `pytest -q`: **1959 passed / 22 failed** (1981 collected); `ruff`: 4
errors; `mypy app`: 12 errors. The `1833 / 20` figures recorded at P3.9 were true at
that date; the difference is attributable to P3.9-A and P3.9-B adding test modules and
code. **P3.9-C introduced nothing new to fix.** The repository is not green and is
not claimed to be.

---

## Update — 2026-10-09 (Phase 3 P3.9 stabilization — defects fixed, suite green)

**No new phase work.** A bug-fix/stabilization pass over the P3.9-A/P3.9-B working tree;
no P3.9-D orchestration, `/research` handler, Celery research task, deadline flow, or
rate limiter was added. No frozen P3.9-C decision was changed.

**The recorded baseline was stale, and is corrected here.** The measured pre-fix
baseline was **4 failed / 1977 passed / 1 error** (1981 collected; `ruff` 2, `mypy app`
9). The 18 `_embedded_ipv6` failures the P3.9/P3.9-C notes attributed to P3.3 do not
reproduce: the name no longer exists anywhere in `app/` or `tests/`, so those notes
described a tree in which the P3.3 fix had not yet landed.

**Defects fixed** (all reproduced, all in the Phase 3 audit § T):

- `ResearchRunLifecycle` defined `describe` twice — the `@staticmethod` at `:265`
  shadowed the transition-description method. Renamed to `describe_transition`;
  `TransitionErrorKind` converted to `enum.StrEnum` so its members are typed, not bare
  `str` (`app/domain/research/lifecycle.py`).
- `BoundAsyncStream(response.stream, ...)` in the fetcher could not be type-narrowed
  from the transport's `SyncByteStream | AsyncByteStream`; added an
  `isinstance(..., httpx.AsyncByteStream)` assertion — no unchecked cast
  (`app/infrastructure/fetch/client.py`).
- `AppContainer.research_uow_factory` annotated `callable | None`; corrected to
  `Callable[[], Any] | None` (`app/bot/container.py`).
- `FetchHTTPStatusError` required `status_code`, breaking the convention that every
  `ResearchError` subclass is constructible from one positional message; it is now
  optional, with `retryable` false when absent and `for_status` remaining the typed
  factory (`app/domain/errors.py`).
- `idna` and `trafilatura` were imported but undeclared; both declared (already
  installed, nothing added or upgraded) (`requirements.txt`).
- Two test defects: an unused unpacked variable (`RUF059`), and a synthesis security
  test that still expected `queued` after the P3.9-A fixture began seeding `running`.

**The one behavioural change: `set_status` now locks the row it validates.** The method
documents an atomic, ownership-scoped read-then-write, but the read used `session.get`
with no lock, so under READ COMMITTED two concurrent transitions both read the old status
and both wrote. The read is now `select(...).with_for_update()`. Ownership and the
lifecycle policy are unchanged. This is what makes the concurrency test's "exactly one
wins" assertion true; verified both ways (the test fails at `assert accepted is False`
when the lock is removed). The test was also rewritten to run synchronously on the
process's long-lived loop — calling `run_on_process_loop` from inside a `pytest-asyncio`
async test raises `RuntimeError` — and to pass the internal user id rather than the
Telegram id.

**Status.** `pytest -q`: **1981 passed** (1981 collected); `ruff check app tests` clean;
`mypy app` clean; `alembic check`: no new upgrade operations (no schema change, no new
migration); `make verify` passes. **The repository is green.** Full record: canonical
audit § T. Next stage: **P3.9 orchestration + `/research` trigger.**

---

## Update — 2026-10-10 (Phase 3 P3.9-D — research orchestration implemented)

**The research path now runs end to end.** `ResearchOrchestrator`
(`app/services/research/orchestrator.py`) is the pipeline; one Celery task per run
(`app/tasks/research_pipeline_task.py`, `run_research(run_id, user_id, chat_id)`)
executes it on the worker's long-lived event loop (AD-043 — all stages inside one
task, not chained stage tasks); the `/research` handler
(`app/bot/handlers.py`) parses, validates, throttles, acknowledges once, and
enqueues. The container builder gained `_build_research_pipeline`, which composes
the orchestrator from the existing services and the Phase 2 `LLMProvider` — no
second client, no new dependency.

**Control flow.** Handler → `ResearchIntakeService.accept` (derived request key,
`ON CONFLICT`-idempotent run creation, B7) → one acknowledgement →
`run_research.delay` → orchestrator: claim (`running`, ownership-scoped) → plan →
search → fetch/extract/chunk bounded by `RESEARCH_MAX_SOURCES` → dedup/rank with
P3.5 metadata persisted → per-source evidence with a `SELECT … FOR UPDATE` claim
guard against re-analysis → synthesis (`completed` or `partial`) → terminal
summary (`set_summary_note`, status-free) → **one** terminal message. Every stage
commits in its own short unit of work; providers are called outside transactions.

**Deadline and outcome model (AD-044, AD-045).** `app/domain/research/outcome.py`
holds `RunOutcomeKind`, the latching `CooperativeDeadline` (injected clock;
`RESEARCH_DEADLINE_SECONDS=None` means unbounded — no invented number), and
`summarize_run` (counts-only). `ResearchDeadlineError` and the no-evidence and
pipeline errors are typed and mapped in `failures.py`. The Celery soft limit stays
a backstop and is caught **non-retryably** (B5-C1): the default
`getattr(exc, "retryable", True)` would re-execute a run that already spent its
budget.

**Schema: one migration.** `0005_research_request_key` adds a nullable
`research_runs.request_key` (`String(32)`) and `UNIQUE (user_id, request_key)`.
No new table; the five-table model (AD-034) is unchanged.

**Rate limiting (AD-047).** Research is admitted through
`RateLimiter.check_share` — per-user window and daily allowance, no global
in-flight slot, so the documented cross-process leak (B6-P4) is not inherited.
Chat's controls are untouched. The **numbers remain BLOCKED**; so does the
deadline value.

**Status.** `pytest -q`: **2063 passed / 1 failed** (2064 collected); the failure
is the SSRF system-resolver test and is environmental — this machine's DNS
wildcard-resolves every name to `198.18.0.0/15` — not a regression. `ruff` and
`mypy app` clean; `alembic check`: no new upgrade operations. Full record:
canonical audit § U; phase record: PHASE-03 § 23. Nothing is committed.

---

## AD-048 — DNS resolution for the fetch boundary

**Status:** `FetchBlockedDestinationError` on every fetch, with search, planning
and evidence working. Not a policy bug — the boundary was correct and was
reporting a true fact. Full record:
[`adr/000048-fetch-dns-over-https.md`](./adr/000048-fetch-dns-over-https.md).

**AD-048 — the resolver is configurable; the policy is not.** The host's DNS
rewrites every UDP/53 answer into `198.18.0.0/15` (RFC 2544 benchmarking — a
fake-IP layer such as Tailscale MagicDNS), so a real public domain resolves to a
non-routable address and the SSRF boundary refuses it, correctly. Rather than
remove the blocklist entry — which would also defend against a real attack, and
which would not work on a host without a matching transparent proxy — resolution
moves behind a setting. `FETCH_DNS_MODE=system` (default) uses the platform
`getaddrinfo` exactly as before; `doh` resolves over HTTPS, which is not
rewritten on those networks, so the answers are real and pass the policy on
their own merits. `ssrf.py` is untouched: the choice is which resolver runs,
never which addresses are permitted. `FETCH_DNS_SERVERS` is an ordered list with
at least two operators, and falls back only when an endpoint cannot be reached or
understood — an answer of "no records" is used as given, because a masked NXDOMAIN
is not something a deployment can reason about.

**The stated limit.** If a future environment also rewrites HTTPS, DoH will not
help. The fix then belongs on the host, in the DNS interception, and must not be
found by loosening the SSRF policy.

---

## AD-049 — AI-provider 403 is an edge block; do not retry it

**Status:** a `/research` run failed with `LLMServiceError(retryable=False)`
from a `403`. Neither obvious reading survives: a deliberately-bogus key
produces a byte-identical response, so the credential was never examined. Full
record:
[`adr/000049-ai-provider-403-is-an-edge-block.md`](./adr/000049-ai-provider-403-is-an-edge-block.md).

**AD-049 — no code change, and here is why.** The refusal carries
`server: cloudflare` and a `cf-ray` header against a genuine `api.groq.com`
certificate, and `GET /v1/models` fails identically. The block is at the edge,
before Groq's authentication. `app/infrastructure/llm.py` already treats 403 as
non-transient, and 429/408/409 are already retried with `retry_backoff` **and**
`retry_jitter` at the task level, and again by the SDK's own `max_retries`.
Adding backoff for a 403 would turn a fast honest failure into a slow one that
spends the run's budget to report the same thing. Both branches the brief
anticipated — quota and transient — are already handled and tested.
