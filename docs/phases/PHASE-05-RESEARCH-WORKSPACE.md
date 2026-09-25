# Phase 5 — Research Workspace

**Status:** Not Started
**Blocked by:** Phase 2 — AI Chat MVP (schema), Phase 3 (research model), Phase 4 (documents)
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md)

> **Nothing in this phase exists today.** The repository contains no database code at all, so none of the entities described here exist.

---

## 1. Objective

Turn a bot that answers questions into a persistent research environment where a user accumulates, organizes, revisits, and reuses research over time.

This is primarily a data-model and retrieval phase. The intelligence already exists by the time it begins; what is missing is structure, persistence across sessions, and organization.

---

## 2. Current State

### Verified absences

| Capability | Status | Evidence |
|---|---|---|
| Database layer | Not Implemented | Zero database code. No models, no tables, no sessions, no migrations |
| `DATABASE_URL` | Declared, unused | `app/core/config.py:6` — never imported anywhere |
| `asyncpg`, `sqlalchemy` | Declared, unused | `requirements.txt:6-7` — never imported |
| PostgreSQL service | Declared, unused | `docker-compose.yml:10-20` — nothing connects to it |
| Research projects | Not Implemented | No model |
| Saved research | Not Implemented | No model |
| Source history | Not Implemented | No model |
| Bookmarks / favorites | Not Implemented | No model |
| Tags | Not Implemented | No model |
| Research sessions | Not Implemented | No model |
| Reusable research context | Not Implemented | No persistence of any kind |

### The consequence

Every message the bot handles today is independent. Nothing is remembered between sessions, nothing is attributable to a project, and nothing can be found again. A user who asks a question on Monday has no way to retrieve the result on Friday, because no result is stored.

### The critical upstream dependency

**This phase has a retroactive-migration risk that must be managed in Phase 2.**

Phase 2 introduces the first schema. If that schema models only `User`, `Conversation`, and `Message` with no notion of a project, a workspace, or a reusable collection, this phase will require a migration across every table, with all the associated data-integrity and index-rebuild risk.

**Action required during Phase 2:** design the schema so research runs, sources, and documents can be associated with an optional project or collection from the start, even if the Phase 2 interface does not yet expose one. This is a data-model decision with no user-facing cost in Phase 2, and significant cost if deferred.

---

## 3. Scope

- Research project model and lifecycle.
- Saved research results associated with a project.
- Source history with per-user provenance.
- Bookmarks and favorites on sources and research runs.
- Tagging with filtering and search.
- Research sessions as a working unit within a project.
- Reusable research context — carrying prior findings forward into new questions.
- Queries and indexes supporting fast retrieval of a user's accumulated research.
- Export of a user's own data, and effective deletion.

---

## 4. Out of Scope

- **New research capability.** Search, fetching, extraction, ranking, and citations are Phase 3. This phase organizes their output.
- **New document capability.** Phase 4.
- **Multi-agent orchestration.** Phase 6.
- **Deep Research mode, report export, citation styles, charts, multilingual research, voice.** Phase 7.
- **Web dashboard, workspace UI, team collaboration, API surface, SaaS, billing.** Phases 8 and 9. A workspace is a **data model** in this phase, not a user interface.
- **Cross-user or organization-level sharing.** That requires the tenancy model from Phase 8.
- **Fixing Phase 1–4 defects.** Report them; do not absorb them.

**Scope note:** building a workspace UI here would be a clear scope violation. The user-facing surface for a workspace is Phase 8.

---

## 5. Functional Requirements

### Projects
- FR-1 A user can create, rename, archive, and delete a research project.
- FR-2 A project belongs to exactly one user.
- FR-3 A project has a title, optional description, creation and update timestamps, and status.
- FR-4 A project can contain research runs, documents, sources, and tags.
- FR-5 Deleting a project applies a defined policy to its contents — cascade delete, or detach and retain. The policy is a documented decision, not an accident.
- FR-6 An archived project is excluded from default listings but retains all data.

### Research runs in a workspace
- FR-7 A research run can be associated with a project.
- FR-8 A research run not in a project remains retrievable through the user's run history.
- FR-9 All sources, chunks, claims, and citations belonging to a run are reachable from the run.
- FR-10 A run's provenance — question, plan, provider, queries, source timestamps — is retained so a result can be explained later.

### Source history
- FR-11 Every source the user has encountered is recorded with its URL, title, domain, and retrieval timestamp.
- FR-12 A source seen in multiple runs is linked to all of them rather than duplicated.
- FR-13 Source content hashes are retained, supporting the Phase 3 deduplication model across runs.

### Bookmarks and favorites
- FR-14 A user can bookmark a source or a research run.
- FR-15 Bookmarks are listed independently of the project they came from.
- FR-16 A bookmark can be removed.

### Tags
- FR-17 A user can create, rename, and delete tags.
- FR-18 Projects, runs, sources, and documents can be tagged.
- FR-19 Tags can be filtered on, and counts are available.
- FR-20 Tag names are unique per user, not globally.

### Research sessions
- FR-21 A research session groups related runs and documents under a working context.
- FR-22 A session can be resumed.
- FR-23 A session's accumulated context can be carried into a subsequent question within that session.

### Reusable research context
- FR-24 Prior findings in a project or session inform subsequent questions.
- FR-25 Reused context is bounded by a token budget, and the bound is configurable.
- FR-26 The user can see which prior research influenced a given answer.
- FR-27 Reuse is on by default for a project and can be disabled.

### Retrieval
- FR-28 A user can search their own research by text across runs, sources, and documents.
- FR-29 Filtering by project, tag, date range, and source domain is supported.
- FR-30 Retrieval is scoped to the requesting user, always.

### Data ownership
- FR-31 A user can export all their data.
- FR-32 A user can delete their account and all associated data.
- FR-33 Deletion is complete and verifiable.

---

## 6. Technical Requirements

### Schema

Building on the Phase 2, 3, and 4 models:

```text
Project        — user id, title, description, status, created_at, updated_at
Session        — project id (nullable), user id, title, status, timestamps
Tag            — user id, name (unique per user)
TagAssignment  — tag id, target type, target id
Bookmark       — user id, target type, target id, created_at
ProjectRun     — project id, run id
ProjectDocument— project id, document id
SourceOccurrence — source id, run id, rank, retrieved_at
```

### Indexes

Every query path in § 5 needs an index. At minimum:

- `Project(user_id, status, updated_at)` — project listing.
- `Session(project_id, created_at)` and `Session(user_id, updated_at)` — session listing and resume.
- `Tag(user_id, name)` unique — tag lookup and uniqueness.
- `TagAssignment(tag_id)` and `TagAssignment(target_type, target_id)` — filtering in both directions.
- `Bookmark(user_id, target_type, target_id)` unique — listing and duplicate prevention.
- `SourceOccurrence(run_id, rank)` and `SourceOccurrence(source_id)` — run assembly and source history.
- Full-text search across runs, sources, and documents, scoped by user.

**Every one of these queries must be verified against its index.** The audit's existing concern about missing indexes applies directly to this phase, where query volume grows with accumulated data.

### Multi-tenancy

Every table is user-scoped from the Phase 2 schema onward. This phase enforces it consistently across polymorphic targets (`TagAssignment`, `Bookmark`), where a missing ownership check is easy to introduce.

### Polymorphic references

`TagAssignment` and `Bookmark` reference targets of different types. Two viable approaches exist — a type column with a loose identifier, or explicit join tables per target type. The choice should be made on the basis of referential integrity guarantees: a loose type column cannot enforce a foreign key, so a deleted target can leave a dangling reference. Explicit join tables preserve integrity at the cost of more tables. **This decision is open and should be recorded when made.**

### Query performance

- Listing and filtering must remain responsive as a user's research grows. N+1 access patterns are not acceptable; relationship loading is explicit.
- Search is implemented against the database rather than by loading a user's entire corpus into memory.

### Migrations

- All changes via migrations, including the association tables and indexes.
- Migration performance is verified against a representative data volume, not an empty database. An index that builds in milliseconds on an empty table can lock a production table for minutes.

---

## 7. Security Requirements

- **SR-1 Ownership is enforced on every read and write.** Every project, session, tag, bookmark, run, source, and document is owned by exactly one user. No access path may rely on the caller supplying a correct identifier without verification.
- **SR-2 Polymorphic targets must not bypass ownership.** Tagging or bookmarking a target the user does not own must fail. This is the highest-risk path in this phase, because polymorphic references are easy to leave unvalidated.
- **SR-3 Queries are always user-scoped.** A missing `user_id` predicate is a data-exposure defect, not a style issue.
- **SR-4 Search is user-scoped.** Full-text search must never return another user's content. A search index that spans users without a filter is a direct disclosure path.
- **SR-5 Deletion is complete.** Deleting a user or a project removes associated data, including derived content and stored files, with no orphaned rows.
- **SR-6 Export contains only the requesting user's data.**
- **SR-7 No authorization by obscurity.** Identifiers being unguessable is not an authorization control.
- **SR-8 Reused context is untrusted.** Persisted research content is attacker-influenced — a user may have saved a page containing injection text. Content reused into a prompt is subject to the same isolation rules as Phase 3 retrieved content.
- **SR-9 No secrets in persisted content or logs.** Persisted content is user data and is not logged in full.
- **SR-10 Bulk operations are bounded.** Project or account deletion over large datasets is chunked or batched rather than executed as one unbounded operation.

---

## 8. Testing Requirements

| ID | Test | Verifies |
|---|---|---|
| T-1 | Project create, rename, archive, and delete lifecycle | FR-1, FR-6 |
| T-2 | A project is retrievable only by its owner | SR-1 |
| T-3 | A run is associated with a project and all its sources are reachable from it | FR-7, FR-9 |
| T-4 | A source seen in two runs is linked, not duplicated | FR-12 |
| T-5 | Bookmarks list independently of the originating project | FR-15 |
| T-6 | Bookmarking the same target twice does not create a duplicate | FR-15, uniqueness |
| T-7 | Tags filter correctly in both directions — by tag to targets, and by target to tags | FR-19 |
| T-8 | Tag names are unique per user and may repeat across users | FR-20 |
| T-9 | **A user cannot tag or bookmark a target they do not own** | SR-2 — mandatory |
| T-10 | A session can be resumed and its context carried into a new question | FR-22, FR-23 |
| T-11 | Reused context respects the token budget | FR-25 |
| T-12 | **Persisted content containing injection text does not alter system behavior when reused** | SR-8 — mandatory |
| T-13 | **Search returns only the requesting user's content** | SR-4 — mandatory |
| T-14 | Filtering by project, tag, date, and domain returns correct results | FR-29 |
| T-15 | Every § 5 query path is verified against its index | TR |
| T-16 | Project deletion applies the documented policy with no orphaned rows | FR-5, SR-5 |
| T-17 | User deletion removes all data, including derived content and stored files | FR-32, SR-5 |
| T-18 | Export contains only the requesting user's data | SR-6 |
| T-19 | The user can see which prior research influenced an answer | FR-26 |
| T-20 | Bulk deletion over a large dataset completes without an unbounded operation | SR-10 |

**Mandatory tests:** T-9, T-12, T-13. Polymorphic ownership, prompt-injection safety in reused content, and search scoping are the defining risks of this phase.

---

## 9. Documentation Requirements

- [ ] Data model documented, including the entity relationship and every index.
- [ ] The polymorphic-reference decision recorded in the Architecture Decision Log with its rationale.
- [ ] The project-deletion policy documented explicitly.
- [ ] Deletion and export semantics documented.
- [ ] Query patterns and their indexes documented.
- [ ] Reused-context behavior and token budgeting documented.
- [ ] **The Phase 2 schema decision documented**, specifically how project and collection association was anticipated — so the reasoning survives.
- [ ] `docs/PROJECT_PLAN.md` maturity table, phase status, and decision log updated.
- [ ] `docs/ARCHITECTURE.md` updated.

---

## 10. Acceptance Criteria

### Projects and organization
- [ ] Projects can be created, renamed, archived, and deleted.
- [ ] Runs and documents can be associated with a project.
- [ ] Sources, bookmarks, and tags are retrievable independently of a project.
- [ ] Tags filter correctly in both directions and are unique per user.
- [ ] Sessions can be created, resumed, and used to carry context forward.

### Reuse
- [ ] Prior research informs subsequent questions within a project or session.
- [ ] Reused context is bounded by a configurable token budget.
- [ ] The user can see which prior research influenced an answer.
- [ ] Reuse can be disabled.

### Retrieval
- [ ] A user can search their own research by text.
- [ ] Filtering by project, tag, date, and domain works.
- [ ] Every documented query path is verified against its index.
- [ ] Query performance is acceptable at a representative accumulated data volume.

### Data ownership
- [ ] A user can export all their data.
- [ ] A user can delete their account and all associated data completely.
- [ ] No orphaned rows or stored files remain after deletion.

### Security
- [ ] A user cannot access another user's project, run, source, document, tag, or bookmark.
- [ ] A user cannot tag or bookmark a target they do not own.
- [ ] Search returns only the requesting user's content.
- [ ] Persisted content containing injection text cannot alter system behavior when reused.
- [ ] Every query is user-scoped.

### Quality
- [ ] All changes are via migrations, verified against a representative data volume.
- [ ] N+1 access patterns are absent.
- [ ] Documentation is updated, including the decision log entries.
- [ ] No user-facing workspace UI was built.
- [ ] No Phase 6–9 functionality was implemented.

---

## 11. Dependencies

### Prerequisites

| Phase | Item | Why this phase needs it |
|---|---|---|
| Phase 2 | Data layer and migrations | The schema this phase extends |
| Phase 2 | **Schema designed with project or collection association in mind** | Avoids a costly retroactive migration across every table |
| Phase 2 | User scoping and ownership enforcement | Every entity here is user-owned |
| Phase 2 | Token budgeting | Bounds reused context |
| Phase 3 | Research run, source, claim, and citation models | The entities being organized |
| Phase 3 | Content-hash deduplication | Supports cross-run source linkage |
| Phase 4 | Document model | Documents participate in projects and tagging |
| Phase 1 | Test harness and Celery reliability | Required for the mandatory tests |

### Infrastructure

- **PostgreSQL** — the primary dependency of this phase. Extended substantially, with full-text search and additional indexes.

### Blocks

- **Phase 6** agents operate over organized research and may write findings back into projects.
- **Phase 7** premium reports are generated from accumulated project research.
- **Phase 8** the web workspace UI requires this data model. This phase is a hard prerequisite for the dashboard's core value.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Phase 2 schema omits project or collection association** | Costly retroactive migration across every table | Design the association into the Phase 2 schema even without exposing it; document the reasoning |
| Polymorphic references without ownership checks | Cross-user data exposure through a single unvalidated path | Mandatory test T-9; validate ownership at the service layer for every polymorphic write |
| Full-text search not scoped to users | Direct disclosure of another user's research | Mandatory test T-13; user predicate mandatory in the search query, verified in review |
| Persisted injection text reused into prompts | Stored content gains durable influence over behavior | Treat reused content exactly like Phase 3 retrieved content; mandatory test T-12 |
| Missing indexes as data accumulates | Progressive degradation to unusable | Index every documented query path; verify with representative volume, not empty tables |
| N+1 patterns in listing and filtering | Query explosion | Explicit relationship loading; verify query counts in tests |
| Polymorphic references without foreign keys | Dangling references after deletion | Prefer explicit join tables where integrity matters; record the decision |
| Deletion implemented as metadata-only | Residual user data, a privacy failure | Mandatory test T-17; verify stored files are removed, not only rows |
| Building a workspace UI in this phase | Scope violation into Phase 8 | This phase is a data model; the interface belongs to Phase 8 |
| Unbounded bulk deletion | Locking or timeout on large datasets | Chunked deletion; mandatory test T-20 |
| Export containing another user's data | Privacy breach | Export filtered by owner; mandatory test T-18 |

---

## 13. Definition of Done

Phase 5 is complete when:

1. Every acceptance criterion in § 10 is checked and evidenced.
2. The three mandatory security tests pass.
3. Every documented query path is verified against its index at representative data volume.
4. Deletion and export are complete and verified.
5. The polymorphic-reference and project-deletion decisions are recorded in the Architecture Decision Log.
6. Documentation is updated, including the Phase 2 schema reasoning.
7. No user-facing workspace UI was built.
8. No Phase 6–9 functionality was implemented.
9. Status is updated in this document and in `docs/PROJECT_PLAN.md`.

---

## 14. Status

**Current status: Not Started**

**Phase status in `docs/PROJECT_PLAN.md`: Not Started.**

**Blocked by:** Phase 2 (schema), Phase 3 (research model), Phase 4 (documents).

No Phase 5 work has been implemented. The repository contains no database code, so none of these entities exist.

**Polymorphic reference strategy: Open.**
**Project deletion policy: Open.**

**Upstream action required:** the Phase 2 schema decision in § 2 is the highest-leverage item in this phase and must be handled during Phase 2, not deferred to this phase.
