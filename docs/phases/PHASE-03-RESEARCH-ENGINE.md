# Phase 3 — Research Engine

**Status:** Not Started
**Blocked by:** Phase 2 — AI Chat MVP
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md)

> **Nothing in this phase exists today.** The audit found no web search, no URL fetching, no HTML extraction, no content cleaning, no deduplication, no ranking, no summarization, no citation generation, and no report generation anywhere in the repository.

---

## 1. Objective

Transform the AI chat bot into an actual AI research system — one that searches the web, reads sources, compares them, and produces an answer with traceable citations.

This is the phase where the product stops being a chatbot and becomes a research assistant. It is also the phase where the security surface expands most sharply, because untrusted external content enters LLM prompts for the first time.

---

## 2. Current State

### The pipeline does not exist

```text
CURRENT — the entire research implementation:

User message
    ↓
app/bot/handlers.py:research_handler()
    ↓
app/tasks/research_task.py:process_research()
    ↓
app/agents/supervisor.py:Supervisor.analyze_query()
    ↓
One LLM call → {"summary": ..., "sub_questions": [...]}
    ↓
Both fields are formatted into a message and sent.

The sub-questions are then DISCARDED. Nothing is searched.
Nothing is fetched. Nothing is cited.
```

### Verified absences

| Capability | Status | Evidence |
|---|---|---|
| Web search | Not Implemented | No client, provider, or configuration anywhere in the repository |
| Source collection | Not Implemented | No source model or table |
| URL fetching | Not Implemented | `httpx` is declared in `requirements.txt:9` and **never imported** |
| HTML extraction | Not Implemented | No parser dependency declared or imported |
| Content cleaning | Not Implemented | No cleaning code |
| Deduplication | Not Implemented | No dedup logic |
| Source ranking | Not Implemented | No ranking code |
| Source summarization | Not Implemented | No summarization code |
| Cross-source analysis | Not Implemented | No analysis code |
| Citation generation | Not Implemented | No citation code or model |
| Report generation | Not Implemented | No report code |

### The one partial artifact

`Supervisor.generate_final_answer` (`app/agents/supervisor.py:46-65`) prompts an LLM to combine sub-question answers into a final response. The graph confirms it has **zero inbound and zero outbound call edges** — it is dead code. It is a two-stage design sketch, not a capability. It may inform this phase's synthesis stage, but it must not be described or treated as existing functionality.

### What Phase 2 provides

This phase assumes Phase 2 delivered: persistence, the data model, the LLM service abstraction, conversation scoping, and token budgeting. The research records defined here extend that schema.

---

## 3. Scope

- Research planning from a user question.
- Web search integration behind a provider abstraction.
- Source collection and persistence.
- Safe URL fetching.
- Content extraction from fetched documents.
- Content cleaning and normalization.
- Source deduplication.
- Source ranking.
- Per-source summarization.
- Cross-source analysis.
- Citation generation with source-to-claim linkage.
- Final research answer assembly.
- Explicit failure handling for unavailable, unreachable, or unusable sources.
- Partial-result behavior when some sources fail.

---

## 4. Out of Scope

- **Document upload and file parsing.** That is Phase 4. This phase reads URLs, not user-supplied files.
- **Research projects, workspaces, saved research, tags, bookmarks.** That is Phase 5. This phase produces research results and persists them; it does not build the organizational surface around them.
- **Multi-agent orchestration.** That is Phase 6. This phase is a single orchestrated pipeline. Agent decomposition comes later.
- **Deep Research mode, long-running jobs, PDF or Markdown export, citation styles, charts, multilingual research, voice.** That is Phase 7.
- **Web dashboard, API surface, SaaS, billing.** Phases 8 and 9.
- **Embedding storage or a vector database.** Not required by the specified pipeline. If retrieval quality measurement later proves it necessary, it is a documented decision, not an assumption.
- **Fixing Phase 1 or Phase 2 defects.** Report them; do not absorb them.
- **Hardcoding a specific search vendor.** See § 6.

---

## 5. Functional Requirements

### Planning
- FR-1 A user question is decomposed into a research plan: sub-questions, search strategies, and expected source types.
- FR-2 The plan is derived from the user's question, not hardcoded.
- FR-3 The plan is validated before execution; a degenerate plan fails clearly rather than producing an empty result.
- FR-4 The plan is persisted so a research run can be inspected and reproduced.

### Search
- FR-5 Web search is invoked through a provider abstraction.
- FR-6 The concrete provider is selected during this phase, not assumed in advance.
- FR-7 Search queries are constructed from the plan and executed with a bounded, configurable result count.
- FR-8 Search failures degrade gracefully: a partial result set is used, and the degradation is surfaced to the user.
- FR-9 Search is rate-limited and cost-accounted per user.

### Source collection
- FR-10 Every candidate source is persisted with its URL, title, snippet, domain, and retrieval timestamp.
- FR-11 Sources are associated with a research run and, where applicable, a conversation.

### URL fetching
- FR-12 Fetching happens through a dedicated, restricted HTTP client — not arbitrary outbound requests.
- FR-13 URL scheme is restricted. Non-HTTP schemes are rejected.
- FR-14 Private, loopback, link-local, and link-mapped internal addresses are rejected, including after DNS resolution, to prevent redirect-based bypass.
- FR-15 Redirect chains are bounded and each hop is revalidated against the restrictions.
- FR-16 Response size is bounded.
- FR-17 Content type is checked before processing.
- FR-18 Every fetch has an explicit, short timeout.
- FR-19 A failed fetch is recorded as a failure against that source and does not abort the run.

### Extraction and cleaning
- FR-20 Main content is extracted from HTML, excluding navigation, advertising, cookie notices, and script or style content.
- FR-21 Extracted text is normalized: whitespace, encoding, and control characters.
- FR-22 Content is chunked into retrieval-sized units with position information retained for citation.
- FR-23 A source that yields no usable content is marked unusable rather than silently included.

### Deduplication
- FR-24 Near-duplicate sources are identified, including syndicated copies of the same article.
- FR-25 Deduplication does not discard distinct sources that share boilerplate.
- FR-26 The surviving representative of a duplicate group is recorded, and the relationship is retained.

### Ranking
- FR-27 Sources are ranked by a documented, configurable strategy.
- FR-28 Ranking considers relevance to the question, source quality, freshness, and content depth.
- FR-29 The ranking strategy is replaceable.
- FR-30 A minimum source-quality threshold is applied; below it, a source is excluded and the exclusion is recorded.

### Analysis and synthesis
- FR-31 Each retained source is summarized independently.
- FR-32 Claims are extracted with an explicit link to the source passage supporting them.
- FR-33 Cross-source comparison identifies agreement, contradiction, and silence.
- FR-34 Contradictions are reported rather than silently reconciled.
- FR-35 The final answer is assembled from the claim set, not generated freehand from the raw pages.

### Citations
- FR-36 Every factual claim in the final answer carries at least one citation.
- FR-37 Each citation resolves to a specific source and, where the format permits, a specific location within it.
- FR-38 Every citation is verifiable — it points to content actually retrieved in this run.
- FR-39 A claim that no source supports is either omitted or explicitly marked as unsupported.
- FR-40 Citation rendering respects Telegram message length limits.

### Failure handling
- FR-41 A run that retrieves no usable sources returns a clear, non-misleading failure message.
- FR-42 A partial run states how many sources failed and why, without exposing internal URLs or stack detail.
- FR-43 A provider failure, a fetch failure, and an extraction failure produce distinguishable outcomes.
- FR-44 A run exceeding its time or cost budget terminates with what it has, and says so.

---

## 6. Technical Requirements

### Provider abstraction — provider undecided

**The concrete web search provider is intentionally not specified in this document.** It must be selected during implementation based on measured requirements, cost, reliability, availability, and rate limits. Whichever is chosen, it sits behind an abstraction.

```text
app/domain/research/        SearchProvider protocol, Source, Claim, Citation types
app/infrastructure/search/  Provider implementation(s) — one file per provider
app/services/research/      Planning, orchestration, ranking policy
app/infrastructure/fetch/   Restricted HTTP client, SSRF controls
app/infrastructure/extract/ Content extraction and chunking
```

The provider implementation must be replaceable without touching orchestration or domain code.

### Fetching security — non-negotiable

Fetching untrusted URLs is the phase's highest-risk component. The restricted client must:

- Resolve the hostname and reject any address in private, loopback, link-local, or otherwise reserved ranges.
- Re-validate **after** DNS resolution and **at every redirect hop**, closing the DNS-rebinding and redirect-bypass paths.
- Allow only HTTP and HTTPS.
- Cap response bytes and time.
- Enforce a redirect limit.
- Never follow a redirect to a disallowed destination.

**This is the SSRF boundary.** It requires tests that would fail against a naive `httpx.get(url)` implementation.

### Data model extensions

Building on the Phase 2 schema:

```text
ResearchRun   — question, plan, status, token/cost totals, timestamps
Source        — run id, url, domain, title, snippet, status, failure reason,
                content hash, rank, timestamps
SourceChunk   — source id, ordinal, text, position metadata
Claim         — run id, text, supporting chunk references, confidence
Citation      — claim id, source id, chunk id, location reference
```

Indexes: `(run_id, rank)` on sources, `(source_id, ordinal)` on chunks, `(run_id)` on claims and citations. A content hash index supports the dedup path.

### Pipeline execution

- The pipeline is a sequence of explicit, individually testable stages.
- Each stage has an input contract, an output contract, and a defined failure mode.
- Stages are independently invocable so a partial run can be resumed or a single stage re-run.
- Long-running stages execute in the Celery worker. The Phase 1 Celery reliability work — time limits, bounded retries, result handling — is a hard prerequisite, not a nice-to-have.

### Prompt safety — the phase's second major risk

This is the phase where untrusted web content first enters prompts. Requirements:

- Retrieved content is **data, never instructions.** It is placed in a clearly delimited region of the prompt that the system prompt describes as quotable evidence, not as direction.
- Retrieved content must never be concatenated into the system instruction.
- A page containing text resembling instructions is the expected adversarial case, not an edge case.
- The system prompt states explicitly that content inside the delimited region is untrusted data and that instructions found there must be ignored.
- Model output is validated as data before it is used to make further calls, closing the loop where injected content steers a follow-up request.

### Determinism and reproducibility

- A research run records the provider used, the queries issued, the sources retrieved, and their content hashes, so a result can be explained after the fact.
- Ranking and selection policies are configuration, so changing them does not require rewriting the pipeline.

---

## 7. Security Requirements

Untrusted external content is introduced in this phase. These requirements are mandatory.

- **SR-1 SSRF prevention.** The fetching client enforces the address, scheme, redirect, size, and timeout restrictions in § 6. Validation occurs after DNS resolution and at every redirect hop. This is the phase's primary security control.
- **SR-2 No arbitrary URL entry from users.** A user supplies a question, not a URL to fetch. If direct URL submission is ever permitted, it routes through the same restricted client and is treated as a separate documented decision.
- **SR-3 Retrieved content is untrusted.** It is data. It never becomes an instruction, a capability, or a tool argument the model can act on.
- **SR-4 Prompt injection defense in depth.** Delimited content regions, an explicit system-level statement that embedded instructions must be ignored, output validation before reuse, and no mechanism by which retrieved text can alter the pipeline's control flow.
- **SR-5 Resource exhaustion controls.** Bounded result counts, bounded response bytes, bounded redirect depth, bounded chunk counts, per-run time and cost budgets, per-user rate limits.
- **SR-6 No server-side request forgery through redirects or DNS.** Covered by SR-1 and explicitly tested.
- **SR-7 External content is never logged in full.** Log URLs, status codes, and sizes. Log bodies only in a controlled, opt-in diagnostic path.
- **SR-8 No secrets in outbound requests.** Provider credentials are never included in a fetched URL, header, or redirect target.
- **SR-9 Citation integrity is a security property.** A citation that does not correspond to retrieved content is a correctness failure and a trust failure. Citations are verified against stored content, not generated freely.
- **SR-10 User isolation.** A research run and its sources belong to exactly one user and are never readable by another.
- **SR-11 Provider credentials are configuration only.** Never source literals, never logs.
- **SR-12 Unavailability is reported honestly.** A degraded result must not be presented as a complete one.

---

## 8. Testing Requirements

| ID | Test | Verifies |
|---|---|---|
| T-1 | The pipeline executes end-to-end against fake search, fetch, and extract providers | Full pipeline |
| T-2 | Planning produces a validated, non-degenerate plan from a real question | FR-1, FR-3 |
| T-3 | Search is invoked through the abstraction; swapping providers requires no orchestration change | FR-5, FR-6 |
| T-4 | **Private and loopback addresses are rejected** | SR-1 |
| T-5 | **A redirect to a private address is rejected** | SR-1, SR-6 |
| T-6 | **DNS rebinding — a hostname resolving to a private address — is rejected** | SR-1 |
| T-7 | Non-HTTP schemes are rejected | FR-13 |
| T-8 | Response size and time limits are enforced | FR-16, FR-18 |
| T-9 | A failed fetch is recorded and does not abort the run | FR-19 |
| T-10 | Extraction removes navigation and script content, keeps article text | FR-20 |
| T-11 | A JavaScript-dependent page is detected and handled rather than producing empty content | FR-23 |
| T-12 | Near-duplicate sources are collapsed; distinct sources are not | FR-24, FR-25 |
| T-13 | Ranking is deterministic and configurable; the quality threshold excludes weak sources | FR-27, FR-30 |
| T-14 | Every claim in the final answer has at least one citation | FR-36 |
| T-15 | **Every citation resolves to content actually retrieved in that run** | FR-38, SR-9 |
| T-16 | An unsupported claim is omitted or explicitly marked | FR-39 |
| T-17 | Contradictory sources are reported, not silently reconciled | FR-34 |
| T-18 | **A page containing prompt-injection text does not alter pipeline behavior** | SR-3, SR-4 |
| T-19 | Retrieved content never appears in the system instruction | SR-4 |
| T-20 | A run with zero usable sources returns a clear failure, not an invented answer | FR-41, SR-12 |
| T-21 | A partial run states how many sources failed | FR-42 |
| T-22 | A run exceeding its time budget terminates and reports partial state | FR-44 |
| T-23 | One user's research run is never accessible by another user | SR-10 |
| T-24 | Full external content is not written to logs | SR-7 |
| T-25 | Per-user rate and cost limits are enforced | SR-5 |

**Mandatory tests:** T-4 through T-7 (SSRF), T-15 (citation integrity), T-18 and T-19 (prompt injection). These are the phase's defining security properties. A suite without them does not satisfy this phase.

---

## 9. Documentation Requirements

- [ ] The research pipeline documented stage by stage, with input, output, and failure contracts.
- [ ] The search provider abstraction documented, including how to add a provider and the criteria used to select the initial one.
- [ ] The SSRF protections documented explicitly, so a future change cannot weaken them unknowingly.
- [ ] Prompt-safety design documented, including how retrieved content is isolated.
- [ ] Data model documented with the entity relationship and every index.
- [ ] Ranking, deduplication, and quality-threshold strategies documented with their rationale and configurability.
- [ ] Citation format documented.
- [ ] Known limitations documented — including which page types cannot be processed.
- [ ] `docs/PROJECT_PLAN.md` maturity table, phase status, and decision log updated.
- [ ] `docs/ARCHITECTURE.md` updated.
- [ ] Operational cost characteristics documented: expected provider calls and tokens per research run.

---

## 10. Acceptance Criteria

### Planning
- [ ] A user question produces a validated research plan.
- [ ] The plan is persisted and reproducible.

### Search
- [ ] Search runs through a provider abstraction.
- [ ] The provider is selected on documented criteria and is replaceable.
- [ ] Search failure degrades gracefully and is surfaced.

### Fetching
- [ ] The fetch client rejects private, loopback, and link-local addresses, including after DNS resolution and at every redirect hop.
- [ ] Non-HTTP schemes are rejected.
- [ ] Size, timeout, and redirect limits are enforced and tested.
- [ ] A failed fetch does not abort the run.

### Extraction
- [ ] Main content is extracted with navigation and scripts removed.
- [ ] Content is chunked with position information retained.
- [ ] A source yielding no usable content is marked unusable.

### Selection
- [ ] Near-duplicate sources are collapsed without discarding distinct sources.
- [ ] Ranking is documented, configurable, and deterministic.
- [ ] A quality threshold excludes weak sources and records the exclusion.

### Analysis and citations
- [ ] Claims are linked to supporting source passages.
- [ ] Cross-source agreement and contradiction are identified.
- [ ] Contradictions are reported, not reconciled silently.
- [ ] Every claim in the final answer carries at least one citation.
- [ ] Every citation resolves to content actually retrieved in that run.
- [ ] Unsupported claims are omitted or explicitly marked.

### Failure handling
- [ ] A run with no usable sources returns a clear, non-misleading failure.
- [ ] A partial run states what failed and how much succeeded.
- [ ] A run exceeding its time or cost budget terminates with partial state reported.
- [ ] A degraded result is never presented as complete.

### Security
- [ ] SSRF tests pass, including redirect and DNS-rebinding cases.
- [ ] A prompt-injection test proves retrieved content cannot alter pipeline behavior.
- [ ] Retrieved content never enters the system instruction.
- [ ] Full external content is not logged.
- [ ] One user's research is never accessible by another user.

### Quality
- [ ] Every stage is independently testable without network access.
- [ ] Rate and cost limits are enforced and tested.
- [ ] Documentation is updated.
- [ ] No Phase 4–9 functionality was implemented.

---

## 11. Dependencies

### Prerequisites

**Phase 2 must be complete.** Specifically required:

| Phase 2 item | Why Phase 3 needs it |
|---|---|
| Data layer and migrations | Research runs, sources, chunks, claims, and citations must persist |
| LLM service abstraction | Every stage that calls a model must go through the abstraction |
| Token budgeting | Multi-stage LLM work needs strict token control |
| Per-user rate and cost limits | Research is far more expensive than chat; unbounded spend is unacceptable |
| User scoping and isolation | Research results must be attributable to exactly one user |
| Error-handling patterns | Every stage has a defined failure mode |
| Conversation scoping | Research runs associate with a conversation |

### Infrastructure

- PostgreSQL, extended with the research schema.
- Redis and the Celery worker, with the Phase 1 reliability settings.
- A search provider — **undecided**, selected during implementation.
- Outbound HTTPS access to search and to fetched sites.

### Blocks

- **Phase 4** reuses the extraction, chunking, and citation machinery for user-supplied documents.
- **Phase 5** organizes research runs into projects and workspaces.
- **Phase 6** decomposes this pipeline into cooperating agents.
- **Phase 7** builds long-running and report-producing modes on this pipeline.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| SSRF through redirects, DNS rebinding, or alternate address forms | Internal network exposure from a public bot | Enforce restrictions after resolution and at every hop; mandatory tests T-4 through T-7 |
| Prompt injection via retrieved page content | Model behavior hijacked by an attacker-controlled page | Delimited untrusted regions, explicit ignore-instructions directive, output validation; mandatory tests T-18, T-19 |
| Citation fabrication — plausible but unbacked claims | Total loss of user trust; defeats the product's purpose | Claims extracted with supporting passages; every citation verified against stored content; mandatory test T-15 |
| Cost explosion from multi-stage LLM work | Unbounded spend per user | Per-stage token limits, per-run budgets, per-user allowances, concurrency caps |
| JavaScript-dependent pages yield empty content | Silent quality degradation presented as completeness | Detect and mark unusable; report coverage honestly |
| Rate limits or blocking at the search provider | Pipeline unavailable | Provider abstraction; documented degradation; multiple-provider option |
| Unbounded page sizes or pathological documents | Memory exhaustion | Byte caps, chunk caps, timeouts |
| Long runs exceeding worker limits | Silent task death | Phase 1 Celery time limits, per-run budgets, partial-result persistence |
| Duplicates and boilerplate dominating results | Low-quality research | Content-hash dedup, ranking, quality thresholds |
| Schema designed without Phase 5 in mind | Costly migration later | Design runs and sources with project/workspace association in mind |
| Search provider hardcoded throughout the codebase | Costly lock-in | Single abstraction point; provider chosen by configuration |

---

## 13. Definition of Done

Phase 3 is complete when:

1. Every acceptance criterion in § 10 is checked and evidenced.
2. A user question produces a cited, multi-source research answer.
3. Every claim carries a citation that resolves to content retrieved in that run.
4. SSRF protections pass all mandatory tests, including redirect and DNS-rebinding cases.
5. Prompt-injection tests prove retrieved content cannot alter pipeline behavior.
6. Every stage is independently testable without network access.
7. Degraded, partial, and zero-source outcomes are reported honestly and tested.
8. The search provider is selected on documented criteria and is replaceable via configuration.
9. Documentation is updated, including SSRF protections and prompt-safety design.
10. No Phase 4–9 functionality was implemented.
11. Status is updated in this document and in `docs/PROJECT_PLAN.md`.

---

## 14. Status

**Current status: Not Started**

**Phase status in `docs/PROJECT_PLAN.md`: Not Started.**

**Blocked by:** Phase 2 — AI Chat MVP.

No Phase 3 work has been implemented. The repository contains no search, fetching, extraction, cleaning, deduplication, ranking, summarization, citation, or reporting code. `Supervisor.generate_final_answer` exists as unwired dead code and is not a capability.

**Provider selection: Open.** Recorded as AD-006 in the Architecture Decision Log. No web search provider has been selected and none is assumed by this document.
