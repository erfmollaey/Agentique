# Phase 7 — Premium Research Features

**Status:** Not Started
**Blocked by:** Phase 3 — Research Engine, Phase 6 — Agent System
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md)

> **Every capability in this document is a future target. None is a current feature.**
> The repository contains no export, no report generation, no long-running job system, and no user preferences.

---

## 1. Objective

Deliver differentiated, higher-value research capabilities that justify a paid tier: long-running deep research, structured report generation, export in multiple formats, and presentation options that a chat reply cannot provide.

The phase exists because the Phase 3 pipeline answers a question in a single conversational exchange. Premium value comes from depth, duration, structure, and portability.

---

## 2. Current State

### Verified absences

| Capability | Status | Evidence |
|---|---|---|
| Deep Research mode | Not Implemented | No such command, mode, or code path |
| Long-running research jobs | Not Implemented | No job model, no progress tracking |
| Report generation | Not Implemented | No report code, no report model |
| PDF export | Not Implemented | No dependency declared or imported |
| Markdown export | Not Implemented | No export code |
| Citation styles | Not Implemented | Citations exist only as an unimplemented Phase 3 concept |
| Charts / visualization | Not Implemented | No charting dependency |
| Multilingual research | Not Implemented | No locale handling; UI strings are hardcoded English literals (audit L-2, still unaddressed) |
| Voice input/output | Not Implemented | No voice handling |
| Advanced research controls | Not Implemented | No user preferences model |

### The infrastructure this phase depends on

Three things must exist before any of this is buildable:

1. **The research pipeline** — Phase 3. Premium modes are configurations over a working pipeline, not a substitute for one.
2. **An orchestration layer** — Phase 6. Deep Research is a long, multi-step, resumable job. A single linear pipeline cannot express it.
3. **Reliable long-running execution** — Phase 1. The audit records the Celery configuration as lacking time limits, acks-late, worker-loss rejection, and bounded retries (audit H-3, M-9). **Building long jobs on that foundation would compound the existing defects.** A job that runs for twenty minutes requires a worker that survives worker loss and reports partial progress.

### Note on the existing base pipeline

`Supervisor.generate_final_answer` (`app/agents/supervisor.py:46-65`) has zero callers. It is not a report generator and must not be treated as a starting implementation of one.

---

## 3. Scope

- Deep Research mode: extended, multi-stage, higher-budget research.
- Long-running research jobs with progress reporting and resumability.
- Structured report generation with a defined document structure.
- Export to PDF and Markdown.
- Configurable citation styles.
- Charts and data visualization where the research produces plottable data.
- Multilingual research and multilingual output.
- Voice input and output, if justified by measurement.
- Advanced research controls exposed to the user.

---

## 4. Out of Scope

- **New research sources or capabilities.** Phase 3. This phase composes and presents existing capabilities.
- **New document capability.** Phase 4.
- **New workspace or organizational capability.** Phase 5.
- **New agent architecture.** Phase 6.
- **Web dashboard, workspace UI, team collaboration, API surface, SaaS, billing, subscription enforcement.** Phases 8 and 9. This phase builds the capabilities; Phase 9 decides who may access them.
- **Fixing Phase 1–6 defects.** Report them; do not absorb them.

**Scope note:** entitlement enforcement is Phase 9. This phase may expose premium capabilities without gating them, provided that is recorded as a deliberate intermediate state rather than an oversight.

---

## 5. Functional Requirements

### Deep Research mode
- FR-1 A user can invoke an extended research mode distinct from a standard query.
- FR-2 Deep Research operates at a higher, explicitly configured budget: more sources, more stages, more time.
- FR-3 Deep Research produces a more thorough result than a standard query, and the difference is measurable.
- FR-4 Deep Research reports progress while running.
- FR-5 Deep Research is cancellable by the user.
- FR-6 Deep Research is subject to the same safety controls — SSRF restrictions, prompt isolation, rate limits — with no relaxation.

### Long-running jobs
- FR-7 A long-running job is a first-class persisted entity with status, progress, and results.
- FR-8 A job survives worker restart and can be resumed.
- FR-9 A job reports progress incrementally to the user.
- FR-10 A job that exceeds its budget terminates with partial results and an honest description of what was not completed.
- FR-11 A user can cancel a running job.
- FR-12 A user can list their jobs and their statuses.
- FR-13 Job state is persisted, so a completed job is inspectable after the fact.

### Report generation
- FR-14 A research result can be rendered as a structured report with defined sections — executive summary, findings, analysis, sources, limitations.
- FR-15 A report's claims carry the same verifiable citations as the inline answer.
- FR-16 A report states its own limitations, including failed sources and coverage gaps.
- FR-17 A report is generated from the claim set, not generated freehand.
- FR-18 A report can be generated from an accumulated project rather than a single query.

### Export
- FR-19 A report can be exported to Markdown.
- FR-20 A report can be exported to PDF.
- FR-21 Exported citations resolve to the same sources as the in-product citations.
- FR-22 Export respects the maximum message size for delivery through Telegram, with a documented alternative delivery mechanism for large files.
- FR-23 Export includes generation metadata — date, sources consulted, and pipeline configuration.

### Citation styles
- FR-24 Citations can be rendered in more than one style.
- FR-25 The style is a user or project preference, not a hardcoded format.
- FR-26 Every style preserves source-to-claim linkage; a style never breaks traceability.

### Visualization
- FR-27 Charts are generated where research produces plottable data — distributions, comparisons, timelines.
- FR-28 A chart is derived from persisted data, not invented.
- FR-29 A chart is never generated when the underlying data does not support it.
- FR-30 Chart generation cannot misrepresent the data.

### Multilingual research
- FR-31 Research can be performed and returned in languages other than the bot's default.
- FR-32 The research language is configurable per request.
- FR-33 Sources in other languages are handled without loss of fidelity.
- FR-34 User-facing strings are externalized so they are translatable without editing code.

### Voice
- FR-35 A user can dictate a question by voice if justified by measurement.
- FR-36 The bot can return an audio response if justified by measurement.
- FR-37 Voice content is handled under the same untrusted-input rules as text.

### Advanced controls
- FR-38 A user can set research depth, source count, source-domain restrictions, and time budget.
- FR-39 Controls are validated and bounded.
- FR-40 Controls persist as user or project preferences.

---

## 6. Technical Requirements

### Job model

```text
Job        — user id, type, status, progress, budget, started_at, completed_at
JobStep    — job id, ordinal, type, status, input, output, error
```

Jobs extend the Phase 3 run model rather than replacing it. A standard query is a short job; Deep Research is a long one. One model, two configurations, avoids duplicating the run concept.

### Resumability

- Job state is checkpointed per step, so a worker restart does not discard completed work.
- Resumption requires the Phase 1 Celery reliability work: `task_acks_late`, `task_reject_on_worker_lost`, bounded retries, and explicit time limits. Without these, a long job's completion state cannot be trusted.
- Step execution is idempotent, so a retried step does not duplicate work or double-charge cost.

### Report generation

- A report is a rendering of the persisted claim and citation set. It is not an independent generation pass that could introduce uncited assertions.
- The report structure is defined as data, so sections can evolve without rewriting the generator.
- Reports are stored as entities and can be re-rendered in a different format or citation style without regenerating the research.

### Export

- PDF and Markdown renderers sit behind a common export interface.
- The concrete PDF library is **undecided** and should be selected during implementation based on output quality, font and script coverage (Latin plus any script required by the multilingual targets), resource cost, and licensing. The bot's user-facing language is **English**; the product is not Persian-first.
- **Non-Latin script rendering must be verified explicitly before multilingual output ships.** A PDF library that mishandles right-to-left text would break any RTL locale added later. English-only rendering is the current requirement; RTL becomes mandatory only when an RTL locale is offered. This is a known risk, not a hypothetical one.
- Large reports exceed Telegram's file size limits. Delivery mechanism is a documented decision.

### Visualization

- Charts are generated from persisted structured data through a charting interface.
- The concrete library is **undecided** and should be selected on output quality, export compatibility, and licensing.
- A chart is never produced when the data does not support one. Fabricating a visual to fill space is a correctness failure.

### Internationalization

- All user-facing strings are externalized from code. The audit records hardcoded literals at `app/bot/handlers.py` and `app/services/formatting.py` (audit L-2); they are English today and remain unaddressed. This phase is the first point at which that debt must actually be paid, because multilingual output requires it.
- Language affects prompt construction, not only output formatting. A research request in one language may need to search in another.

### Cost

- Premium modes are expensive by design. Per-run and per-user budgets are mandatory and configurable.
- A long job must not be able to exhaust a worker's time limit without reporting partial state.
- Cost is attributed per job and per step.

---

## 7. Security Requirements

- **SR-1 No security relaxation for premium modes.** SSRF restrictions, prompt isolation, rate limits, and budget caps apply identically to standard and Deep Research. Premium access is not a security exemption.
- **SR-2 Job isolation.** A job belongs to one user. A user may list, inspect, and cancel only their own jobs.
- **SR-3 Export contains only the requesting user's content.** A report never includes another user's research, even when generated from a shared project — a shared project does not exist before Phase 8.
- **SR-4 Voice content is untrusted input**, treated exactly as text. Transcription output enters prompts with the same isolation rules.
- **SR-5 Export files are private.** Delivered files are not publicly accessible. Any retrieval mechanism is scoped to the owning user and short-lived.
- **SR-6 Export must not create a path traversal vector.** A title or filename is never used unvalidated to construct a file path.
- **SR-7 Chart data comes only from persisted, verified data.** Never from model-generated numbers. A hallucinated figure rendered as a chart is materially deceptive in a way that text is not.
- **SR-8 Long jobs cannot be used for denial of service.** Per-user job concurrency is capped; a user cannot queue unbounded long jobs.
- **SR-9 Cancellation is authoritative.** A cancelled job stops consuming budget. Cancellation that does not halt execution is not cancellation.
- **SR-10 Generated documents carry provenance.** A report states what it is, when it was generated, and from which sources — so a document that leaves the system remains interpretable.
- **SR-11 No secrets in reports or exports.** Configuration detail and credentials are never rendered into user-facing documents.

---

## 8. Testing Requirements

| ID | Test | Verifies |
|---|---|---|
| T-1 | Deep Research produces a measurably more thorough result than a standard query | FR-3 |
| T-2 | Deep Research reports progress incrementally | FR-4 |
| T-3 | A job survives worker restart and resumes from its last completed step | FR-8 |
| T-4 | A retried step does not duplicate work or double-charge cost | Idempotency |
| T-5 | A job exceeding its budget terminates with partial results described honestly | FR-10 |
| T-6 | Cancellation halts execution and stops budget consumption | FR-11, SR-9 |
| T-7 | A user can list their own jobs and only their own | FR-12, SR-2 |
| T-8 | A report renders with all defined sections | FR-14 |
| T-9 | **Every claim in a report carries a verifiable citation** | FR-15 — mandatory |
| T-10 | A report states its limitations and coverage gaps | FR-16 |
| T-11 | A report is rendered from the persisted claim set, not regenerated | FR-17 |
| T-12 | **Every script the product claims to support renders correctly in PDF export** | Script-coverage risk — mandatory |
| T-13 | Markdown export preserves citations and structure | FR-19 |
| T-14 | Exported citations resolve to the same sources as in-product citations | FR-21 |
| T-15 | **An oversized report uses the documented alternative delivery mechanism** | FR-22 |
| T-16 | Every citation style preserves source-to-claim linkage | FR-26 — mandatory |
| T-17 | **A chart is never generated from model-generated numbers** | SR-7 — mandatory |
| T-18 | No chart is produced when the data does not support one | FR-29 |
| T-19 | Research and results work in a language other than the default | FR-31 |
| T-20 | **No user-facing string remains hardcoded in source** | FR-34 — mandatory |
| T-21 | Voice transcription output is treated as untrusted input | SR-4 |
| T-22 | **Deep Research applies the same SSRF restrictions as standard research** | SR-1 — mandatory |
| T-23 | Per-user job concurrency is capped | SR-8 |
| T-24 | Export contains only the requesting user's content | SR-3 — mandatory |
| T-25 | A report containing a traversal sequence in its title cannot write outside the export root | SR-6 |
| T-26 | Reports contain no configuration detail or credentials | SR-11 |
| T-27 | Advanced controls are validated and bounded | FR-39 |

**Mandatory tests:** T-9, T-12, T-16, T-17, T-20, T-22, T-24. Citation integrity in reports, RTL rendering, style traceability, chart data provenance, i18n completeness, security parity, and export isolation are the defining risks of this phase.

---

## 9. Documentation Requirements

- [ ] Deep Research mode documented, including how it differs from a standard query.
- [ ] The job model and lifecycle documented, including resumption and cancellation.
- [ ] Budget defaults documented, and how an operator changes them.
- [ ] The report structure documented.
- [ ] Export formats documented, including the alternative delivery mechanism for large files.
- [ ] **PDF library selection and the script-rendering verification result recorded in the Architecture Decision Log.** English is the current language; RTL correctness becomes a requirement the moment an RTL locale is offered.
- [ ] Citation styles documented.
- [ ] Visualization rules documented, including when a chart is not produced.
- [ ] The i18n approach documented, including how strings are externalized.
- [ ] The voice decision recorded — including the measurement that justified or rejected it.
- [ ] `docs/PROJECT_PLAN.md` maturity table, phase status, and decision log updated.
- [ ] `docs/ARCHITECTURE.md` updated.

---

## 10. Acceptance Criteria

### Deep Research and jobs
- [ ] A user can invoke an extended research mode at a higher configured budget.
- [ ] Deep Research measurably outperforms a standard query on the evaluation set.
- [ ] Long jobs survive worker restart and resume.
- [ ] Jobs report progress incrementally.
- [ ] A job exceeding its budget terminates with partial results described honestly.
- [ ] A user can cancel a running job, and cancellation halts budget consumption.
- [ ] Per-user job concurrency is capped.

### Reports
- [ ] A research result renders as a structured report with defined sections.
- [ ] Every claim in a report carries a verifiable citation.
- [ ] A report states its limitations and coverage gaps.
- [ ] A report renders from the persisted claim set rather than an independent generation pass.
- [ ] A report can be generated from an accumulated project.

### Export
- [ ] Markdown export works and preserves citations and structure.
- [ ] PDF export works, including correct rendering for every script the product claims to support.
- [ ] Exported citations resolve to the same sources as in-product citations.
- [ ] Oversized reports use a documented alternative delivery mechanism.
- [ ] Exported files are private to the owning user.

### Citations, charts, language, voice
- [ ] More than one citation style is supported, and every style preserves traceability.
- [ ] Charts are generated only from persisted data, and never when the data does not support one.
- [ ] Research works in languages other than the default.
- [ ] No user-facing string remains hardcoded in source.
- [ ] Voice input and output are implemented or formally rejected with a recorded measurement.

### Controls
- [ ] A user can set research depth, source count, domain restrictions, and time budget.
- [ ] Controls are validated, bounded, and persisted as preferences.

### Security
- [ ] Deep Research applies the identical security controls as standard research.
- [ ] A user can access only their own jobs and exports.
- [ ] Export content is restricted to the requesting user.
- [ ] Reports contain no credentials or internal configuration.

### Quality
- [ ] The seven mandatory tests pass.
- [ ] Documentation is updated, including the library selections.
- [ ] Entitlement enforcement is recorded as deliberately deferred to Phase 9, or implemented.
- [ ] No Phase 8–9 functionality was implemented.

---

## 11. Dependencies

### Prerequisites

| Phase | Item | Why this phase needs it |
|---|---|---|
| Phase 1 | Celery reliability — time limits, acks-late, worker-loss rejection, bounded retries | **Hard prerequisite.** Long jobs on an unreliable queue compound existing defects |
| Phase 3 | Working research pipeline | Premium modes are configurations over it, not replacements |
| Phase 3 | Citation and claim model | Reports and exports render from it |
| Phase 3 | Prompt isolation and SSRF controls | Reused without relaxation |
| Phase 5 | Job and project persistence | Jobs persist; reports generate from projects |
| Phase 6 | Orchestration layer | Deep Research is a long multi-step resumable job |
| Phase 2 | Token budgeting and cost accounting | Premium modes are expensive; attribution is required |

### Infrastructure

- PostgreSQL, extended with job, job-step, and report entities.
- Redis and the Celery worker, configured for long-running work.
- File delivery for exported reports.
- **PDF library, charting library: undecided.**

### Blocks

- **Phase 8** the web dashboard presents reports, exports, and job status.
- **Phase 9** these capabilities are the substance of the Pro and Team tiers. This phase defines *what* is premium; Phase 9 defines *who gets it* and *at what price*.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Long jobs on the current Celery configuration** | Silent job death, lost work, double-charging | Phase 1 reliability work is a hard prerequisite; resumable checkpointing; idempotent steps |
| **PDF library mishandling non-Latin or RTL text** | Reports break for any locale added later | Explicit script-rendering test before multilingual output ships; library selection is a recorded decision |
| Charts generated from model-invented numbers | Materially deceptive output | Charts derive only from persisted structured data; mandatory test T-17 |
| Deep Research becoming an SSRF or injection bypass | Reopening the phase's highest risks | Identical controls; mandatory test T-22 |
| Cost explosion from long, multi-stage runs | Unbounded spend | Per-run and per-user budgets, job concurrency caps, cancellation that actually halts spend |
| Hardcoded strings never externalized | Multilingual output impossible | Mandatory test T-20 makes the debt visible and blocking |
| Citation style breaking traceability | Users cannot verify claims | Styles are renderings only; mandatory test T-16 |
| Reports regenerated rather than rendered from stored claims | Uncited assertions reappear in premium output | Reports render from the persisted claim set; mandatory test T-9 |
| Oversized reports undeliverable through Telegram | Feature unusable for large research | Documented alternative delivery mechanism; mandatory test T-15 |
| Entitlements assumed but never enforced | Cost exposure, or a gate built in the wrong phase | Entitlement enforcement is explicitly Phase 9; any intermediate state is recorded, not assumed |
| Long jobs blocking workers | Queue starvation for ordinary chat | Separate queue or worker pool for long jobs; per-user concurrency caps |
| Voice implemented speculatively | Wasted effort on an unvalidated feature | FR-35, FR-36 require justification by measurement; formal rejection is an acceptable outcome |

---

## 13. Definition of Done

Phase 7 is complete when:

1. Every acceptance criterion in § 10 is checked and evidenced.
2. The seven mandatory tests pass.
3. A user can run a Deep Research job that survives restart, reports progress, can be cancelled, and terminates honestly at its budget.
4. A report renders with verifiable citations and stated limitations.
5. PDF export renders every claimed script correctly — verified, not assumed.
6. No user-facing string remains hardcoded.
7. Every library selection is recorded in the Architecture Decision Log with its justification, including any feature formally rejected.
8. Entitlement enforcement is implemented or its deferral to Phase 9 is explicitly recorded.
9. No Phase 8–9 functionality was implemented.
10. Status is updated in this document and in `docs/PROJECT_PLAN.md`.

---

## 14. Status

**Current status: Not Started**

**Phase status in `docs/PROJECT_PLAN.md`: Not Started.**

**Blocked by:** Phase 3 — Research Engine, Phase 6 — Agent System. Additionally gated on Phase 1 Celery reliability work.

No Phase 7 work has been implemented. The repository has no long-running jobs, no report generation, no export, no citation styles, no charts, no internationalization, and no voice handling. The user-facing strings remain hardcoded English literals.

**PDF library: Open.**
**Charting library: Open.**
**Voice input/output: Open** — to be implemented or formally rejected on measured evidence.
