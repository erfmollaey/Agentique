# Phase 3 — P3.0 Contract Freeze: Completion Record

**Stage:** P3.0 — Contract Freeze + Documentation Reconciliation
**Date:** 2026-10-07
**Type:** Documentation and contract-freeze record. **This is not an implementation audit.**
**Companion documents:** [`PHASE-03_RESEARCH_ENGINE_AUDIT.md`](./PHASE-03_RESEARCH_ENGINE_AUDIT.md) (read-only gap analysis, the evidence base); [`../phases/PHASE-03-RESEARCH-ENGINE.md`](../phases/PHASE-03-RESEARCH-ENGINE.md) § 15 (the frozen contract).

---

## A. Scope

**P3.0 covers:** freezing the Phase 3 implementation contract (trigger UX, pipeline responsibilities, provider boundary, security boundaries, citation integrity, persistence model, failure model, testing requirements); recording the resulting architectural decisions; reconciling stale Phase 1/Phase 2/roadmap status statements with the verified evidence; and identifying the next implementation stage.

**P3.0 explicitly does not cover:** any Research Engine production code; any application behavior change; the Phase 3 database migration; search provider selection or implementation; HTTP fetching; SSRF enforcement code; extraction; citations; synthesis; research orchestration; Celery configuration changes; Telegram handler changes; new dependencies; new tests. Where a code change appeared necessary, it was recorded as a prerequisite instead of being implemented (see § E).

---

## B. Evidence reviewed

| Evidence | Role |
|---|---|
| [`PHASE-03_RESEARCH_ENGINE_AUDIT.md`](./PHASE-03_RESEARCH_ENGINE_AUDIT.md) | Primary evidence source: read-only gap analysis of the repository against the Phase 3 specification |
| [`../phases/PHASE-03-RESEARCH-ENGINE.md`](../phases/PHASE-03-RESEARCH-ENGINE.md) | Source of truth for Phase 3 requirements (44 FRs, SR-1…SR-12, T-1…T-25) |
| [`../phases/PHASE-02-AI-CHAT.md`](../phases/PHASE-02-AI-CHAT.md) § 14 | Phase 2 completion record: requirement matrix, validation table, real end-to-end run |
| [`../phases/PHASE-01-INFRASTRUCTURE.md`](../phases/PHASE-01-INFRASTRUCTURE.md) § 14 | Phase 1 implementation record and the three former blockers |
| [`../PROJECT_PLAN.md`](../PROJECT_PLAN.md) | Roadmap, maturity table, Architecture Decision Log |
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | Historical pre-Phase-1 audit baseline (preserved) |
| [`../DEVELOPMENT_RULES.md`](../DEVELOPMENT_RULES.md) | Governance; evaluated for required changes (none — see § D.10) |
| [`../../.agents/skills/research-bot/SKILL.md`](../../.agents/skills/research-bot/SKILL.md) | Agent briefing; reading list updated for Phase 3 |
| [`../../README.md`](../../README.md) | Current-state run/security documentation |
| Git history (all commits) | Full-history scan: no secret-shaped value in any tracked file; `.env` never committed |
| `pytest --collect-only` | 370 tests collected (documentation drift check; suite not executed — P3.0 runs no code) |

---

## C. Reconciliation results

| # | Document | Old status | Actual evidence | Corrected status |
|---|---|---|---|---|
| 1 | `docs/PROJECT_PLAN.md` "Current Project Status" | Phase 2 current; "open human action: Phase 1 credential rotation … the key in `.env` is confirmed live" | Rotation completed before Phase 2; CI green; Docker Compose configuration verified | Phase 3 current (P3.0 complete); Phase 1 and 2 COMPLETE and closed |
| 2 | `docs/PROJECT_PLAN.md` phase table | Phase 1 "In Progress"; Phase 3 "Not Started" | As above | Phase 1 "Complete"; Phase 3 "In Progress — P3.0 Contract Freeze complete; P3.1 next" |
| 3 | `docs/PROJECT_PLAN.md` maturity table | "368 tests" | `pytest --collect-only` collects **370** | "370 tests" |
| 4 | `docs/phases/PHASE-01-INFRASTRUCTURE.md` header + § 14 | "In Progress — blocked on manual credential rotation and two environment gates" | All three blockers resolved (rotation before Phase 2; Docker verified in Phase 2 § 14; CI workflow green) | "Complete (reconciled 2026-10-07)"; P0-3, SR-4, P1-10, SR-10 rows marked Implemented; blockers table annotated with resolutions |
| 5 | `docs/phases/PHASE-02-AI-CHAT.md` header + § 14 | "In Progress — blocked on Phase 1 credential rotation"; three items NOT VERIFIED (GitHub Actions, docker build, rotation) | Rotation done before Phase 2; GH Actions green; Docker Compose configuration verified | "Complete (closed; reconciled 2026-10-07)"; table rows annotated Resolved; Telegram delivery remains the single environment-limited item |
| 6 | `docs/phases/PHASE-02-AI-CHAT.md` § 14 "Known limitations" | "No conversation export or deletion API. SR-9 unmet" | Contradicted the same document's SR-9 row (Implemented — `/delete_account confirm`, 25 tests) and acceptance table (Met) | Corrected: deletion implemented; export is Phase 5 scope (FR-31 there) |
| 7 | `docs/phases/PHASE-03-RESEARCH-ENGINE.md` header + § 14 | "Not Started"; "Blocked by: Phase 2" | Phase 2 complete; P3.0 contract freeze done | "In Progress — P3.0 Contract Freeze complete"; blocked by nothing; § 15 contract added |
| 8 | `README.md` "Known limitations" + Security | "Credentials still require manual rotation"; "Outstanding manual action"; claimed keys are recoverable "in this repository's git history" | Rotation completed before Phase 2; full-history scan of every commit finds **zero** secret-shaped values; `.env` never committed; first commit already post-Phase-1 | "Credential exposure — resolved"; git-history claim corrected with the scan evidence; GH Actions/Docker moved from "not verified" to verified; Telegram delivery kept as the remaining unverified item |
| 9 | `.agents/skills/research-bot/SKILL.md` reading list | Pointed agents at Phase 2 as the current implementation record | Phase 3 is active with a frozen contract (§ 15) | Reading list now leads agents to the Phase 3 spec § 15 contract and the Phase 3 audit before writing code |
| 10 | `docs/ARCHITECTURE.md` | Update notes ended at Phase 2 completion | Phase 3 architectural decisions exist (frozen) | Appended "Update — 2026-10-07, Phase 3 P3.0 contract freeze" block; historical audit body untouched |

**Preserved history:** `docs/ARCHITECTURE.md` remains the unmodified pre-Phase-1 defect baseline (changes are dated header notes only). [`PHASE-03_RESEARCH_ENGINE_AUDIT.md`](./PHASE-03_RESEARCH_ENGINE_AUDIT.md) remains the historical read-only audit and was **not** rewritten — including its own list of stale citations, which this record's rows 1–8 resolve. Phase 1 § 14 and Phase 2 § 14 keep their original evidence tables with resolution annotations rather than deletions; pre-reconciliation text is recoverable from git history.

---

## D. Frozen contracts

Full text: [`../phases/PHASE-03-RESEARCH-ENGINE.md`](../phases/PHASE-03-RESEARCH-ENGINE.md) § 15. Decisions: AD-029 … AD-035 in [`../PROJECT_PLAN.md`](../PROJECT_PLAN.md).

1. **Trigger UX (AD-029).** `/research <question>` — required argument, validated before enqueue, owned by exactly one user, associated with the active conversation, exactly one terminal outcome message (completed | partial | failed). Chat behavior unchanged; users supply questions, never URLs (SR-2).
2. **Pipeline (AD-030).** 22 frozen responsibilities from query interpretation through persistence. Internal module boundaries may differ; every responsibility must remain represented and individually testable.
3. **Provider abstraction (AD-006 remains Open).** Port-based architecture with a provider-neutral result model; no provider-specific leakage into domain logic; replacement requires no orchestration change; selection is an explicit gate for P3.0→P3.2 and was deliberately **not** made here.
4. **SSRF boundary (AD-031).** Post-DNS address validation before connecting; connection pinned to the validated resolution (rebinding closure); full revalidation at every redirect hop; HTTP(S) only; connect/read/total timeouts; streaming size cap with decompression-bomb protection; redirect depth limit; content-type allowlist; dedicated client (`trust_env=False`); no credentials outbound; bodies never logged. Declared a security boundary, not an optimization.
5. **Prompt-injection boundary (AD-032).** No tool surface for the research model; fetched content enters prompts only as delimited untrusted data; instructions never originate from fetched content; outputs structurally validated before reuse; pipeline-assigned source identity.
6. **Citation integrity (AD-033).** Programmatic chain `source → source_chunk → claim → citation`; citations reference persisted rows of the same run only; passage support verified programmatically; unsupported claims omitted or marked; mandatory test T-15.
7. **Persistence model (AD-034).** Exactly five tables: `research_runs`, `sources`, `source_chunks`, `claims`, `citations`. `ResearchQuery`, `SearchResult`, `FetchResult`, `ResearchReport` are transient domain concepts, not tables. `/delete_account` cascade extension is a hard requirement. All data user-owned; Phase 5 workspace tables excluded.
8. **Failure model.** Frozen taxonomy (spec § 15.9): intake, search, fetch, extraction, LLM, budget, citation, persistence, delivery, and cancellation categories, each classified retriable / non-retriable / partial-convertible / fatal. Zero-usable-source runs fail honestly; partial runs report counts without internals.
9. **Testing requirements.** Persian-language fixtures mandatory (questions, content, mixed pages, Unicode punctuation, RTL, Persian citations, normalization); § 8 mandatory security tests unchanged (T-4…T-7, T-15, T-18, T-19); fetcher tests run against mocked transport/loopback so the socket guard stays green.
10. **Governance evaluation.** `docs/DEVELOPMENT_RULES.md` required **no** change: phase discipline (§ 1), prompt-injection and untrusted-content rules (§ 3), provider abstraction (§ 8), and documentation honesty (§ 6) already cover the frozen contract. No redundant rules were added.
11. **Celery execution constraint (AD-035).** The 120-second task limit is recorded as a known Phase 3 risk; the deadline/cancellation model is assigned to P3.9; no configuration changed at P3.0.

---

## E. Open prerequisites

Only genuine prerequisites remain after P3.0:

1. **Search provider selection (gates P3.2).** AD-006 remains open by design. Must be a measured decision (requirements, cost, reliability, availability, rate limits), recorded as a new AD superseding AD-006. Not made at P3.0; no provider is assumed anywhere.
2. **P3.1 implementation.** Domain types/ports + migration 0002 + research deletion cascade extension (see § F).
3. **Research execution timeout/deadline strategy (due at P3.9).** The 120-second Celery limit must be reconciled with real research run durations, and cancellation/deadline behavior made explicit, before production-grade execution (FR-44). Deliberately unresolved at P3.0 — recorded, not assumed.
4. **Extraction library choice (due at P3.3/P3.4).** Candidates recorded in the gap analysis (e.g., trafilatura vs readability-lxml); selection happens with the fetch/extraction stages and must be recorded.
5. **Trigger detail deferred to implementation:** the exact stricter rate/cost allowance for `/research` versus chat (FR-9/SR-5) — the contract requires it to exist; values are set when the budget model lands (P3.9).

No unresolved design decision has been turned into a hidden assumption: each is listed here with its owning stage.

---

## F. Next implementation task

**P3.1 — Domain Types / Ports + Migration 0002 + Research Deletion Cascade Extension**

Precisely what P3.1 implements:

1. **`app/domain/research/`** — pure, I/O-free domain types per the frozen contract: `ResearchPlan`, `SearchResult`, `FetchedDocument`, `ExtractedContent`, `SourceRecord`, chunk, claim, citation types, run/source status enums, research error types with `retryable`; and the ports: `SearchProvider`, `SafeFetcher`, `ContentExtractor`, `RankingPolicy`, research repository ports. No adapters.
2. **Migration `0002_research_schema`** — the five frozen tables (`research_runs`, `sources`, `source_chunks`, `claims`, `citations`) with foreign keys, uniqueness constraints, check constraints, and the indexes named in the contract (`(user_id, created_at)` on runs, `(run_id, rank)` and `(content_hash)` on sources, `(source_id, ordinal)` on chunks, `(run_id)` on claims, claim/source indexes on citations). All schema changes via Alembic; `alembic check` must stay clean.
3. **Research deletion cascade extension** — `/delete_account` must provably remove a user's entire research subtree (runs → sources/chunks/claims/citations), with the real-PostgreSQL deletion test extended accordingly, and cross-user isolation tests for the new tables.
4. **Explicitly not in P3.1:** any search provider, fetcher, extractor, ranking, LLM call in the research path, Telegram trigger, or Celery task. No network surface.

Acceptance for P3.1: migration round-trips (empty → head → base → head) on the test database; models/migrations agree; cascade and isolation tests pass against real PostgreSQL; `ruff`, `mypy`, and the full suite green; documentation updated in the same change.
