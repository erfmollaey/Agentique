# Phase 3 — Research Engine

**Status:** In Progress — P3.0 Contract Freeze complete (2026-10-07); P3.1, P3.2, P3.3, P3.5, P3.6 implemented; P3.7 implemented (2026-10-08); P3.8 implemented (2026-10-08); P3.9 read-only audit + contract freeze (2026-10-08); P3.9-A/B/C/D (2026-10-09 → 2026-10-10); **P3.10 closure pass complete (2026-10-13)** — citation rendering (FR-40), per-run token accounting (FR-9/SR-5/T-25), and the Tavily provider decision (AD-050). § 13 DoD is **met except for items named as deferred in § 24.6**; the phase is not marked Complete, and the remaining gaps are enumerated there rather than left implied.
**Active Phase:** Yes
**Blocked by:** Nothing — Phase 2 is complete and closed
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md); [`../audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](../audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md) (read-only gap analysis + every stage record, § A–§ V)
**Implementation contract:** § 15 (frozen at P3.0) and § 15.13 (P3.1 scope).

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

> **Status of this section:** the requirements below are the original phase
> specification and are unchanged. § 15 is the frozen implementation contract
> that governs where it and this section disagree, and § 14 records what has
> actually been built. FR-6's provider selection was completed at P3.2 (§ 14).

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

### Provider abstraction — provider decided at P3.2, re-selected at P3.10 (supersedes the original "undecided")

**The concrete web search provider was intentionally not specified in this document.** It was selected during implementation on measured requirements, cost, reliability, availability, and rate limits. **AD-036** chose **Brave Search API** (superseding AD-006), with the full evidence in [`../audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](../audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md) § K. Result quality was **not** measured — no usable credential was available — and that gap was recorded rather than glossed.

> **Re-selected (P3.10, AD-050): the shipped default is Tavily Search API.** A search
> credential now exists, which discharged the outstanding item § K.10 left open —
> re-test Tavily's credential path and, if it works, re-evaluate it against Brave on
> the same criteria. It worked, and on that re-evaluation Tavily met the required
> search functionality **within this project's free-tier allowance, where Brave Search
> was not a viable free option at this project's usage**. That budget rationale is the
> whole of the reason. No comparative quality or latency benchmark was run, so those
> axes remain **NOT VERIFIED** as § K left them, and no claim is made that Tavily is
> better on any of them or that it is free indefinitely.
>
> **What did not change is the decision itself:** search runs behind the
> `SearchProvider` port, `SEARCH_PROVIDER` selects the adapter, and both the Tavily
> and the Brave adapter are registered. Re-selecting is a configuration value.

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

Each item names where it is documented and, where it is not fully met, says so.

- [x] The research pipeline documented stage by stage, with input, output, and failure contracts. *(§ 15.2 freezes the 22 responsibilities; the failure taxonomy per category is § 15.9; `ResearchOrchestrator.execute` documents its eight stages in order with what each commits and what it may raise, and each stage service documents its own input/output contract.)*
- [x] The search provider abstraction documented, including how to add a provider and the criteria used to select the initial one. *(§ 6 states the layout; audit § K.2 records the criteria and § K.1/K.5 the candidates; AD-036 and now **AD-050** record the selection and its rationale. "Add a module, add one branch to `create_search_provider`" is stated there and asserted by the swap test.)*
- [x] The SSRF protections documented explicitly, so a future change cannot weaken them unknowingly. *(§ 15.5 freezes the contract; audit § M.1–M.5 documents the exact flow, the rebinding closure, the attack-class table, the limits, and the full error taxonomy.)*
- [x] Prompt-safety design documented, including how retrieved content is isolated. *(§ 15.6 freezes the containment model; audit § C.2 expands it into seven enforceable rules; AD-032 carries the rationale.)*
- [x] Data model documented with the entity relationship and every index. *(§ 15.8 freezes the five tables; audit § E proposes them with ownership, cascades, uniqueness, and the reasoning for what is *not* a table; audit § J.2 lists every index as built.)*
- [x] Ranking, deduplication, and quality-threshold strategies documented with their rationale and configurability. *(§ 16.1–16.4 and audit § N: canonicalization policy, the weighted score, tie-breaking, determinism guarantees, the three deliberate judgement calls, and the exact-identity limitation.)*
- [x] Citation format documented. *(§ 24.1 — marker placement, per-source numbering, the source-list line format, escaping, and the mapping from marker to source.)*
- [ ] Known limitations documented — including which page types cannot be processed. *(Stage limitations are recorded individually and honestly — § 16.4 (near-duplicates are exact only), § 18.4 (containment is architectural, coverage bounded), § 19.5 (the answer's prose is model text; `contradicted` deliberately unused), § 23.4. **The box stays unticked**: there is no single consolidated statement of which page types cannot be processed, and that list should come from measurement the phase has not done.)*
- [x] `docs/PROJECT_PLAN.md` maturity table, phase status, and decision log updated. *(Status line, maturity table, phase table, and AD-050 added.)*
- [x] `docs/ARCHITECTURE.md` updated. *(A dated update section recording the three closures and the corrected baseline.)*
- [ ] Operational cost characteristics documented: expected provider calls and tokens per research run. *(**The mechanism now exists and the section can be written from measurement** — § 24.3 records exactly how many provider calls a run makes by construction, and § 24.2 records that the run now persists the tokens those calls reported. **The box stays unticked because no run has been executed against live providers**, so there is no measured figure and none is invented.)*

---

## 10. Acceptance Criteria

### Planning
- [x] A user question produces a validated research plan. *(P3.6 — `ResearchPlan`
      produced from a question through the `LLMProvider`, with degeneracy,
      dependency, and limit validation. No user-facing trigger yet: the question
      reaches planning through `PlanningService`, which the P3.9 orchestrator will
      call from `/research`.)*
- [x] The plan is persisted and reproducible. *(P3.6 — `research_runs.plan` (JSONB,
      reused from P3.1) with a lossless, re-validating round-trip verified against
      real PostgreSQL.)*

### Search
- [x] Search runs through a provider abstraction. *(P3.2 — `SearchProvider` protocol,
      one adapter per vendor, and a single selection point in
      `create_search_provider`; the domain imports no provider module, asserted from
      the module's AST.)*
- [x] The provider is selected on documented criteria and is replaceable. *(AD-036 made
      the selection; **AD-050 re-selected it to Tavily** on a budget rationale — see § 6.
      Replaceability is unchanged and asserted in both directions: the default builds
      the Tavily adapter, an explicit `SEARCH_PROVIDER="brave"` builds the Brave one,
      and a fake satisfies a consumer unchanged (T-3).)*
- [x] Search failure degrades gracefully and is surfaced. *(P3.9-D —
      `ResearchOrchestrator._search_step` catches `SearchNoResultsError` and records
      the step as done rather than propagating, so the run continues on the partial
      result set; every other `SearchError` propagates to the bounded retry policy and
      is classified distinctly from a fetch or extraction failure, FR-43.)*

### Fetching
- [x] The fetch client rejects private, loopback, and link-local addresses, including after DNS resolution and at every redirect hop. *(P3.3 — the SSRF boundary in `infrastructure/fetch/ssrf.py` is the single place an address is judged; validation runs on every resolved address, and the request is pinned to the validated IP so no second resolution can occur. 128 SSRF tests, including DNS-rebinding, redirect-to-private, IPv4-mapped IPv6, and downgrade cases.)*
- [x] Non-HTTP schemes are rejected. *(P3.3 — scheme allowlist; userinfo and ambiguous numeric host forms (`0x7f000001`, `2130706433`, `127.1`, `0177.0.0.1`) are refused before resolution.)*
- [x] Size, timeout, and redirect limits are enforced and tested. *(P3.3 — the decoded
      stream is capped while streaming, the compression ratio is bounded separately,
      and connect/read/total deadlines are enforced with `asyncio.timeout`.)*
- [x] A failed fetch does not abort the run. *(P3.9-D — each source's outcome is
      recorded per source and the pipeline continues; the typed `FetchError` family
      keeps a fetch failure distinguishable from a provider or extraction failure.)*

### Extraction
- [x] Main content is extracted with navigation and scripts removed. *(P3.4, with
      Persian fixtures per § 15.10.)*
- [x] Content is chunked with position information retained. *(P3.4 extraction plus the
      P3.9-A chunk producer, wired into the orchestrator: `source_chunks` rows carry
      contiguous ordinals and char offsets verified against the stored text.)*
- [x] A source yielding no usable content is marked unusable. *(P3.4 marks it; P3.9-D
      records it per source and counts it toward the degradation tally that decides
      `partial` over `completed`, AD-044.)*

### Selection
- [x] Near-duplicate sources are collapsed without discarding distinct sources. *(P3.5 —
      fingerprints are taken over extracted main content, never the raw page, so a
      shared banner cannot merge two real articles. **Limitation, carried forward and
      not hidden:** identity is *exact*, so a truncated or re-worded syndicated copy is
      not detected — § 16.4.)*
- [x] Ranking is documented, configurable, and deterministic. *(P3.5 — five named
      signals with stated weights; no clock, no randomness, input-order independent,
      asserted from the module's AST. Only the threshold is configurable, and § 16.3
      records why the weights deliberately are not.)*
- [x] A quality threshold excludes weak sources and records the exclusion. *(P3.5 —
      a below-threshold entry stays in the output carrying an `excluded_reason` naming
      the measured quality and the threshold; it is never silently dropped.)*

### Analysis and citations
- [x] Claims are linked to supporting source passages. *(P3.7 — every accepted
      claim carries a `source_chunk_id` and a quote that is verified against the
      stored passage text before anything is persisted; a candidate that cannot be
      verified is rejected with a recorded reason and never becomes a `Claim`.)*
- [x] Cross-source agreement and contradiction are identified. *(P3.8 — one
      synthesis call over the run's whole bounded evidence set; agreement is several
      citations on one section, silence is a source that contributes none.)*
- [x] Contradictions are reported, not reconciled silently. *(P3.8 — a reported
      conflict keeps **both** sides with **all** their citations, and
      `claims.status='contradicted'` is deliberately unused, because marking a side
      reconciles it silently.)*
- [x] Every claim in the final answer carries at least one citation. *(P3.8 — a
      section with no citation that resolves cannot be built, and
      `SynthesizedAnswer` cannot be constructed at all without one. No semantic truth
      detection is attempted; see § 19.3.)*
- [x] Every citation resolves to content actually retrieved in that run. *(P3.7 makes
      this structurally possible for claims; **P3.8 closes it at answer level** — the
      registry is scoped by `claims.run_id`, so a foreign citation is absent rather
      than filtered, and the passage, quote, URL, and location of every citation are
      read from persisted rows.)*
- [ ] Unsupported claims are omitted or explicitly marked. *(Partial — an
      unsupported *citation* is refused. Marking a section whose prose the evidence
      does not support requires deciding that the prose follows from the passage,
      which no contract here can do. Audit § Q.13 item 2. **This box stays unticked**:
      half a criterion is not the criterion, and the missing half is not a gap this
      phase can close honestly.)*

### Failure handling
- [x] A run with no usable sources returns a clear, non-misleading failure. *(P3.9-D —
      synthesis refuses before any model call when the run holds no citable evidence,
      and the task delivers an explicit notice that says what happened to the
      **question**, never an invented answer and never an internal URL or stage name.)*
- [x] A partial run states what failed and how much succeeded. *(P3.9-D — degradation
      tallies decide `partial` over `completed` (AD-044) and `failure_summary` carries
      counts only, via `summarize_run`; § U.2 records that no hostname, URL, or
      identifier reaches it.)*
- [ ] A run exceeding its time or cost budget terminates with partial state reported. *(Behaviour
      is built and tested with an injected clock — the deadline latches, is checked at
      every stage boundary and before each provider call, a deadline-expired run can
      only land on `partial` or `failed`, and the stopped-early notice is asserted. **The
      box stays unticked because no deadline *value* exists:** `RESEARCH_DEADLINE_SECONDS`
      is `None`, because no run has been measured and inventing a number is forbidden
      (AD-045; audit § U.4.1). Cost accounting now exists (§ 24) but **no cost budget is
      enforced** — that stays deferred under AD-047 rather than ticked.)*
- [x] A degraded result is never presented as complete. *(P3.9-D — `completed` requires
      no recorded degradation at all; any failed fetch, empty extraction, below-threshold
      exclusion, or unreached candidate makes the run `partial`, and synthesis's
      `evidence_offered` / `evidence_available` counters make a truncated context
      reportable rather than presentable as exhaustive.)*

### Security
- [x] SSRF tests pass, including redirect and DNS-rebinding cases. *(P3.3 — T-4…T-7 and
      the extended cases in audit § C.1. Mandatory gate; green.)*
- [x] A prompt-injection test proves retrieved content cannot alter pipeline behavior. *(P3.8 —
      T-18, English, Persian, and mixed-language hostile pages, asserted through the
      database: a hostile passage produces no fabricated citation. Mandatory gate.)*
- [x] Retrieved content never enters the system instruction. *(T-19 — every research
      system-prompt builder takes numbers only, so no code path can place fetched text
      in the instructions; the orchestrator calls the existing services unchanged, so
      P3.9-D added no new injection surface.)*
- [x] Full external content is not logged. *(Fetch and search adapters log URL, status,
      byte counts, and timings only; the redaction filter and log-hygiene tests cover
      it, and the terminal summary carries counts only.)*
- [x] One user's research is never accessible by another user. *(T-23 — every research
      read and write is ownership-scoped, including the new `set_summary_note` and the
      source-claim guard, and account deletion removes the whole subtree against real
      PostgreSQL.)*

### Quality
- [x] Every stage is independently testable without network access. *(The suite's
      autouse socket guard stays green; search, fetch, ranking, planning, evidence,
      synthesis, rendering, and the orchestrator all run against fakes or a real
      local database.)*
- [ ] Rate and cost limits are enforced and tested. *(Half met and the box says so.
      **Rate:** the dedicated research allowance is enforced and tested
      (`RateLimiter.check_share` — a per-user window and daily allowance separate from
      chat's, with the global in-flight slot deliberately not taken so research does
      not inherit B6-P4's cross-process leak). **Cost:** a run now records the usage its
      own calls reported (§ 24), so the ledger is populated for the first time — but
      **no cost limit is enforced**, because no per-user spend ceiling has been decided
      and choosing one now would be an invented operational limit (AD-047, audit
      § U.4.2). The numbers remain BLOCKED pending a measured run.)*
- [ ] Documentation is updated. *(Substantially delivered — § 24 records citation
      format, chunking behaviour, token accounting, and the provider decision, and the
      authoritative documents no longer contradict the code. **The box stays unticked**
      because two § 9 items below are still open: the per-run cost characteristics now
      have a mechanism but no measured figures, and the consolidated "which page types
      cannot be processed" list is incomplete.)*
- [x] No Phase 4–9 functionality was implemented. *(Verified: no upload or file
      parsing, no workspace/project tables, no agent loop or tool surface, no API
      surface beyond health, no embeddings or vector store. The P3.10 additions are a
      pure renderer, a usage accumulator, and configuration wiring.)*

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

**Current status: In Progress — P3.0 Contract Freeze (2026-10-07); P3.1, P3.2, P3.3, P3.5, P3.6, P3.7, P3.8 implemented; P3.9 read-only audit + contract freeze complete (2026-10-08); P3.9-A prerequisites B1/B2 complete (2026-10-09) — SourceChunk production + SourceRepository ranking persistence; P3.9-B B3 composition-root + B4 lifecycle partial (2026-10-09) — container wiring verified, ownership-scoped lifecycle implemented, documented omissions recorded; **P3.9-C B5/B6 operational decisions complete (2026-10-09)** — B6 resolved (AD-046 question length, AD-047 dedicated research rate allowance), B5 resolved as a policy (AD-045 cooperative in-run deadline) with its numeric value explicitly **BLOCKED** pending a measured run; **P3.9 stabilization pass complete (2026-10-09)** — four reproducible defects fixed, `set_status` row lock added, repository green (`1981 passed`; `ruff`/`mypy` clean; `alembic check` clean). Implementation of P3.9 orchestration + `/research` trigger next.**

**Phase status in `docs/PROJECT_PLAN.md`: In Progress — P3.0 … P3.9 (audit + contract freeze) complete; B6 resolved; B5 policy resolved with its value blocked; stabilization pass green; P3.9 implementation next.**

**Phase 3 is NOT complete.** § 13 requires every acceptance criterion in § 10 to be
evidenced, and several remain unticked: the SSRF criteria (the P3.3 defects in
audit § Q.14 keep the fetch tests red), partial/degraded/budget-exhausted run
behaviour, FR-40 citation rendering, and the whole of P3.9.

**Blocked by:** Nothing. Phase 2 is complete and closed. The Phase 1 items that were open when this document was written (credential rotation, CI provider, Docker verification) were reconciled at P3.0.

The Phase 3 production code was seeded at P3.1 with the persistence foundation. P3.2 added the search provider port and a Brave Search API adapter (the Tavily adapter and the selection change to Tavily arrived later — AD-050, § 6). P3.3 delivered the SSRF-safe URL fetcher — the security boundary that makes research-derived URLs safe to fetch. No code in the research pipeline may reach a URL without passing through this boundary.

### P3.1 — Domain types / ports + migration 0002 + research deletion cascade (2026-10-07)

**Implemented** (foundation only; no pipeline stage exists yet):

- `app/domain/research/types.py` — pure pydantic domain types: `ResearchRun`, `Source`, `SourceChunk`, `Claim`, `Citation`, with the frozen status enums `ResearchRunStatus`, `SourceStatus`, `ClaimStatus` and validation of identity/status invariants. No SQLAlchemy, FastAPI, aiogram, Celery, Redis, HTTP, or provider SDK imports (verified by test and by module inspection).
- `app/domain/research/ports.py` — runtime-checkable protocols for the five persistence repositories (`ResearchRunRepository`, `SourceRepository`, `SourceChunkRepository`, `ClaimRepository`, `CitationRepository`). Provider ports (search/fetch/extract/rank/synthesize) are deliberately **not** created at P3.1; each arrives with the stage that needs it (P3.2 onward), per § 15.3.
- `app/domain/ports.py` — the `UnitOfWork` protocol extended with the five research repositories; `app/domain/errors.py` gained `ResearchRunNotFoundError`.
- `app/db/models.py` — the five ORM models (`ResearchRunModel`, `SourceModel`, `SourceChunkModel`, `ClaimModel`, `CitationModel`) with CHECK constraints on the closed lifecycle sets, uniqueness (`sources (run_id, url)`, `source_chunks (source_id, ordinal)`, `citations (claim_id, source_id, chunk_id)`), FK `ON DELETE CASCADE` throughout plus `SET NULL` on `research_runs.conversation_id` and `sources.duplicate_of_id`, and indexes on the retrieval paths (§ 6, AD-034).
- `app/db/migrations/versions/0002_research_schema.py` — migration `0002_research` (rev id `0002_research`, down_revision `0001_phase2`), with a full `downgrade`. `alembic check` reports no drift; the migration round-trips on a scratch database in the test suite.
- `app/repositories/research.py` + `SqlAlchemyUnitOfWork` — concrete implementations of the five ports; every read path that can reach user data takes the owning user (`get_for_user`) per SR-10.
- `/delete_account` cascade extension: `UserRepositoryImpl.delete` is unchanged in shape; the new FK cascades remove all research artifacts in the same single delete statement. Transactional semantics are inherited from the Phase 2 UnitOfWork; a mid-deletion failure rolls back the whole tree (tested).

**Tests added:** `tests/test_research_domain.py` (domain types constructible without infrastructure; invalid statuses rejected; ports are runtime-checkable), `tests/test_research_persistence.py` (ownership scoping, FK integrity, uniqueness, closed status sets, plan JSONB persistence, ORM cascade parity), `tests/test_data_deletion.py` (research tree removal on account deletion, cross-user isolation of research artifacts, transactional rollback of the research tree). Migration tests updated for head `0002_research` and the eight-table schema.

**Verification (2026-10-07, actual output):** full suite **406 passed**; `ruff check app tests` clean; `mypy app` clean (49 source files); `alembic check` — no new upgrade operations; migration round-trip green.

**Not done at P3.1:** search provider selection (AD-006, P3.2 gate), fetcher (P3.3), extraction (P3.4), dedup/ranking (P3.5), planning (P3.6), summarization/claims (P3.7), synthesis/citations (P3.8), orchestrator (P3.9), `/research` handler. Persian fixtures begin at the stage that needs them (P3.4 onward, per § 15.10).

**Next implementation stage:** P3.2 — search port + provider selection (AD supersedes AD-006) + one adapter + config.

### P3.2 — Search port + measured provider selection + one adapter + config (2026-10-07)

**Implemented** (search stage only; no pipeline stage exists yet):

- `app/domain/research/search.py` — the provider-independent contract: `SearchRequest` (query, bounded result count, optional language/market, optional relative freshness window, optional safe-search level), `SearchResult` (`url`, `domain`, `rank`, `title`, `snippet`, `published_at`, `relevance`), `SearchResponse` (query, provider, ordered results, retrieval time, `is_empty`, `domains`), the `SearchFreshness` and `SafeSearchLevel` vocabularies, and `domain_of()` — the single normalization rule, in the domain so every provider inherits it identically. No HTTP, no provider import, no `Source`/`SourceChunk` concept, no fetching, ranking, or dedup.
- `app/domain/research/ports.py` — `SearchProvider`, a `runtime_checkable` `Protocol` with one method: `async search(SearchRequest) -> SearchResponse`. An empty result set is signalled by the terminal `SearchNoResultsError`, not by an empty response, so "found nothing" is never confused with "the provider failed".
- `app/domain/errors.py` — the `SearchError` family, one branch per outcome § 15.9 lists: `SearchRequestInvalidError`, `SearchAuthenticationError`, `SearchRateLimitError`, `SearchTimeoutError`, `SearchProviderError`, `SearchResponseInvalidError`, `SearchNoResultsError`, each with the `retryable` flag § 15.9 requires (rate limit, timeout, provider failure retryable; invalid request, rejected credentials, contract violation, and no-results terminal).
- `app/core/config.py` + `.env.example` — `SEARCH_PROVIDER` (`brave`), `SEARCH_API_KEY` (`SecretStr`, optional so a chat-only deployment still boots), `SEARCH_MAX_RESULTS` (≤20), `SEARCH_TIMEOUT_SECONDS`, `SEARCH_LANGUAGE`, `SEARCH_REGION`, `SEARCH_SAFE_SEARCH`, and `Settings.search_enabled`. `httpx` promoted from a transitive pin to an explicit one (the version already resolved by the openai SDK; no new version enters the tree).
- `app/infrastructure/search/brave.py` — the one adapter. Auth as a per-request header (never a URL, never a client default), `trust_env=False`, `follow_redirects=False` so a redirect can never carry the token elsewhere, an explicit per-call timeout, status→typed-error mapping, freshness/safe-search translation, and logging of outcome and counts only — never the credential, the query, or a response body. Malformed payloads are a contract violation; an HTML error page served with 200 is never reported as "found nothing".
- `app/infrastructure/search/factory.py` — `create_search_provider`, the single selection point mirroring `create_llm_client`. An unrecognised `SEARCH_PROVIDER` raises rather than silently defaulting; a missing credential is a named `SearchAuthenticationError`, not an unauthenticated request.

**Provider selection (AD-036, superseding AD-006; re-selected by AD-050 — see § 6).** Eight candidates were evaluated against API characteristics, operational suitability, and architectural compatibility, with live reachability/error probes against every candidate's real endpoint and a live seven-query benchmark against the only keyless candidate available. **Result quality was not measured** — no usable search credential existed in the environment, and none was invented. Full candidates, criteria, per-endpoint probe results, the decision, the trade-offs, and the outstanding evidence are in [`../audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](../audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md) § K.

> **Superseded in part (2026-10-13, AD-050):** this section's evaluation stands as
> written and is preserved. The **selection** it reached — Brave — was changed to
> **Tavily** on a budget rationale recorded in `PROJECT_PLAN.md` AD-050 and § 6 above.
> § K.6's own record of why Tavily lost is the reason the re-evaluation was possible:
> its credential was refused by an *edge block* from this egress path, which § K
> explicitly called a deployment-environment finding and not a quality verdict.

**Tests added:** `tests/test_research_search_domain.py` (request validation, result normalization, the `SearchResult` ≠ `Source` ≠ `SourceChunk` distinction, empty-response behaviour, and structural proof that the domain imports no HTTP or provider module) and `tests/test_research_search_adapter.py` (success, multiple results, capping, Persian round-trip, empty response, malformed response, timeout, auth failure, rate limit, unexpected error, credential handling, log hygiene, the T-3 provider-swap guarantee, and configuration behaviour). Every adapter case runs through a mocked httpx transport; **no test calls a real provider**.

**Verification (2026-10-07, actual output):** targeted search tests **106 passed**; full suite **512 passed** (was 406); `ruff check app tests` clean; `mypy app` clean (53 source files). No migration was created or altered at this stage.

**Known limitations of P3.2:** search result quality is unverified; the SSRF-safe fetcher is not built, so returned URLs are untrusted and unvalidated; no source is persisted yet; there is no search service, orchestrator, Celery task, `/research` handler, or research LLM call; cost is bounded per query but not accounted per run; exactly one adapter exists and there is no fallback.

**Not done at P3.2:** fetcher and SSRF boundary (P3.3), extraction (P3.4), dedup/ranking (P3.5), planning (P3.6), summarization/claims (P3.7), synthesis/citations (P3.8), orchestrator, Celery task, `/research` handler.

**Next implementation stage:** P3.3 — Fetcher + SSRF Security Boundary (mandatory tests T-4…T-8).

---

### P3.3 — Fetcher + SSRF Security Boundary (2026-10-07)

**Implemented** (fetch boundary only; no pipeline stage exists yet):

- `app/domain/research/fetch.py` — the provider-independent contract: `FetchRequest` (untrusted URL), `FetchedSource` (final URL, status code, content type, content encoding, charset, body bytes, redirect chain, fetch timing). The body is raw bytes, not text; decoding and extraction belong to P3.4. `FetchedSource` ≠ `Source` ≠ `SourceChunk` — this distinction is load-bearing: a successful fetch proves reachability, not content quality.
- `app/domain/research/ports.py` — `SafeFetcher` protocol, the single port for retrieval of one untrusted URL. One method: `async fetch(FetchRequest) -> FetchedSource`. Raising a typed `FetchError` subclass for every failure category (§ 13). Deliberately no "fetch without checks" alternative.
- `app/infrastructure/fetch/ssrf.py` — the **authoritative SSRF boundary** (§ 15.5, AD-031). Single module where an address is judged connectable:
  - `validate_url()`: structural validation — forbidden characters, scheme allowlist (HTTP/HTTPS only), no credentials, valid port, hostname normalization (lowercase, IDNA/punycode, root dot stripped), rejects ambiguous numeric host forms (`0x7f000001`, `2130706433`, `127.1`, `0177.0.0.1`).
  - `resolve_and_validate()`: resolves hostname, validates **every** returned address. Fails closed on DNS failure or empty answer. If any address violates policy, the destination is rejected — no "pick the safe one".
  - `AddressPolicy` (`classify_address`): explicit blocklists for IPv4 and IPv6 covering loopback, RFC1918, carrier-grade NAT, link-local, multicast, documentation/test ranges, deprecated anycast relays, benchmarking, reserved, and IPv6 unique-local, link-local, site-local, IPv4-mapped/compatible/6to4/NAT64/Teredo with embedded IPv4 re-validated.
  - `SsrfPolicy` assembles allowed schemes + resolver; single constructed instance enforces identical policy per fetch.
  - `AddressResolver` protocol for test substitution (scripted resolver, no network).
- `app/infrastructure/fetch/transport.py` — DNS-rebinding closure (§ 15.5, AD-031). `pin_request()` rewrites the request URL to the **validated IP literal**, sets `Host` header to the hostname, and sets `sni_hostname` for TLS verification against the hostname. The validated address is the one connected to; no second DNS resolution occurs. `_PinnedTransport` is private to the package — no bypass API exists.
- `app/infrastructure/fetch/client.py` — `RestrictedFetcher` implements `SafeFetcher`:
  - Per-hop validation: every redirect re-runs the full SSRF boundary (scheme + DNS + address policy).
  - Redirect handling by hand (no implicit following): max redirects configurable, loop detection via visit set, HTTPS→HTTP downgrade rejected.
  - Response size: streaming byte cap on **decoded** body (not `Content-Length`), enforced before each chunk is appended. Compression ratio bounded (decompressed/wire bytes).
  - Timeouts: connect, read, and total deadline (applied around send + body read) via `asyncio.timeout`.
  - Content type allowlist from configuration (default `text/html`, `text/plain`); unsupported encodings rejected.
  - Error taxonomy: typed `FetchError` subclasses with explicit `retryable` flags — SSRF violations **never retryable**.
  - Logging: only redacted URLs (scheme, host, port, path; never query or userinfo), status, byte counts, hop counts, elapsed time.
  - Client isolation: dedicated transport, `trust_env=False` (no proxy leakage), no cookie jar, no auth flow, no provider credentials ever attached.
- `app/core/config.py` + `.env.example` — fetch configuration: `FETCH_ALLOWED_SCHEMES` (subset of `http,https`), `FETCH_ALLOWED_CONTENT_TYPES`, `FETCH_CONNECT_TIMEOUT_SECONDS`, `FETCH_READ_TIMEOUT_SECONDS`, `FETCH_TOTAL_TIMEOUT_SECONDS`, `FETCH_MAX_RESPONSE_BYTES`, `FETCH_MAX_COMPRESSION_RATIO`, `FETCH_MAX_REDIRECTS`, `FETCH_MAX_CONNECTIONS`, `FETCH_MAX_KEEPALIVE_CONNECTIONS`, `FETCH_KEEPALIVE_EXPIRY_SECONDS`, `FETCH_USER_AGENT`.

**Tests added:** 
- `tests/test_research_fetch_domain.py` — request/semantic validation, URL trimming, frozen models, redirect chain normalization, body-is-bytes, credentials stripped from stored URLs.
- `tests/test_research_fetch_client.py` — successful fetch, redirect chain, size limit, compression ratio limit, timeout, content-type rejection, encoding rejection, credential leakage proof, log redaction proof, HTTPS→HTTP downgrade rejection.
- `tests/test_research_fetch_ssrf.py` — 87 security tests: IP range rejection (IPv4 RFC1918, loopback, link-local, multicast, unspecified, reserved; IPv6 ULA, link-local, multicast, documentation, deprecated, embedded IPv4), DNS multi-answer rejection when any answer is private, DNS failure fails closed, IP literal bypasses DNS, blocked IP literal rejected without DNS, scheme allowlist, ambiguous numeric host rejection, credential rejection, resolver receives normalized hostname/port, system resolver contract.

**Verification (2026-10-07, actual output):** targeted fetch tests **130+ passed**; full suite **512+ passed**; `ruff check app tests` clean; `mypy app` clean (53+ source files). No migration required at this stage.

**Known limitations of P3.3:** the fetcher returns raw bytes; HTML extraction, cleaning, and chunking are P3.4; no source persistence yet (P3.1 schema is ready); no orchestration, Celery task, or `/research` handler.

**Not done at P3.3:** extraction (P3.4), dedup/ranking (P3.5), planning (P3.6), summarization/claims (P3.7), synthesis/citations (P3.8), orchestrator, Celery task, `/research` handler.

**Next implementation stage:** P3.4 — Extraction + Persian Fixtures.

## 15. P3.0 — Contract Freeze (2026-10-07)

This section is the frozen implementation contract for Phase 3. It was produced by the P3.0 stage, which modified documentation only — no production code, tests, migrations, dependencies, or configuration. It freezes *what* must be built and the boundaries it must respect; it does not prescribe internal module layout beyond the boundaries stated here and in § 6. Decisions are recorded as AD-029 … AD-035 in `docs/PROJECT_PLAN.md`. Where this section and an earlier section of this document disagree, this section governs.

### 15.1 Research trigger UX — `/research <question>` (AD-029)

The initial Telegram surface for research is one command. Not implemented yet; this is the contract the implementation must satisfy.

- **Format:** `/research <question>`. The question is everything after the command, whitespace-collapsed.
- **Required argument:** a missing or empty question is rejected with usage guidance; it is never enqueued and never treated as chat text.
- **Validation:** the question is validated before any work is enqueued — non-empty after stripping, within the configured inbound length cap. Invalid input produces a clear, non-revealing message and no state change.
- **Ownership boundary:** a research run belongs to exactly one Telegram user and is associated with that user's active conversation at the time of the command (where a conversation exists). One user's runs, sources, and results are never readable or listable by another user (SR-10). A user supplies a **question**, never a URL to fetch (SR-2).
- **Lifecycle:** accepted → queued → running → completed | partial | failed. The user receives an immediate acknowledgement and exactly one terminal outcome message per run: a cited answer, an honest partial answer stating what failed, or a clear failure notice. Intake is subject to the existing rate limiting; research may carry a stricter allowance than chat because it is far more expensive (FR-9, SR-5).
- Chat behavior is unchanged: plain text messages continue to the Phase 2 chat flow. The command surface and the chat surface must not route into each other.

### 15.2 Pipeline responsibilities (AD-030)

The conceptual pipeline is frozen as the following 22 externally meaningful responsibilities, executed as an ordered, staged pipeline:

1. Query interpretation
2. Query decomposition
3. Search provider
4. Search result normalization
5. URL validation
6. HTTP fetching
7. Redirect validation
8. Content-type validation
9. Size/time enforcement
10. HTML extraction and cleaning
11. Metadata extraction
12. Deduplication
13. Ranking / source selection
14. Passage/chunk creation
15. Evidence extraction
16. Claim extraction
17. Cross-checking / evidence comparison
18. Citation construction
19. Synthesis
20. Report generation
21. Telegram delivery
22. Persistence

The implementation may use different internal module boundaries, but every responsibility must remain represented, individually testable, and mapped to the functional requirements in § 5. Stages have explicit input contracts, output contracts, and failure modes (§ 6 "Pipeline execution"), and are independently invocable so a partial run can be resumed (FR-19, FR-44).

### 15.3 Domain boundaries — ports, not providers

Domain and application logic must not depend on a concrete search engine, HTTP client, HTML parser, or ranking implementation. Each boundary below is a port (a `Protocol`), with concrete adapters in `app/infrastructure/`, mirroring the Phase 2 `LLMProvider` philosophy (a single factory selection point; swapping an adapter requires no orchestration change):

- Query interpretation / planning
- Search (`SearchProvider`)
- Fetching (`SafeFetcher` — the § 15.5 boundary)
- Extraction (`ContentExtractor`)
- Evidence/chunking
- Claim extraction
- Synthesis
- Citation validation
- Research persistence (repository ports; SQL stays in `app/db` + `app/repositories`)
- Delivery (existing Telegram delivery boundary)

### 15.4 Search provider contract — selection made at P3.2 (AD-006 closed by AD-036)

*This section was frozen at P3.0 with selection open. The gate it named is now discharged; the recorded outcome is AD-036, with evidence in the Phase 3 audit § K, re-selected by AD-050 (§ 6). The requirements below were not relaxed.*

- Provider selection was **not** made at P3.0. It was an explicit prerequisite/gate for **P3.2**, decided on measured requirements, cost, reliability, availability, and rate limits, and recorded as a decision superseding AD-006. **Discharged 2026-10-07: Brave Search API (AD-036). Re-selected 2026-10-13: Tavily Search API (AD-050)** — the port decision, which is what this bullet requires, is unchanged.
- The architecture depends on a provider port with a provider-neutral result model (title, URL, snippet, rank, provider, retrieval timestamp). **Implemented as `SearchProvider` + `SearchRequest`/`SearchResult`/`SearchResponse`.**
- No provider-specific type, field, or behavior leaks into domain logic; provider replacement requires no change to Research Engine orchestration (tested by a swap test, mirroring Phase 2 T-6). **Enforced structurally and tested.**
- Multiple providers and provider fallback remain options the abstraction permits; they are not built at the outset. **Still one adapter, by design.**

### 15.5 SSRF security contract (AD-031)

Fetching untrusted URLs is this phase's highest-risk component. The following boundary is a **security boundary, not an optional optimization**. Implementation belongs to P3.3; the contract is frozen now.

**URL validation, before connecting:**

- Resolve the hostname (DNS) and inspect **every** resolved address.
- Reject private/internal/link-local/loopback/reserved destinations per the security policy: RFC1918, `127.0.0.0/8`, `169.254.0.0/16`, `::1`, `fe80::/10`, `fc00::/7`, unspecified, multicast, reserved ranges, and IPv4-mapped IPv6 (the embedded v4 re-validated).
- Re-validate **at every redirect hop**: each redirect target re-runs the full validation (scheme, DNS, address class).
- Prevent DNS-rebinding bypasses by connecting using the **validated resolution/IP strategy** (connect to the validated address; TLS SNI and `Host` keep the hostname) rather than resolving again at connect time.

**Resource limits (all configurable):**

- Request timeout (connect, read, and a total deadline).
- Maximum response size, enforced while **streaming** — never read-then-measure; `Content-Length` is advisory only.
- Decompression-bomb protection: the cap applies to the decoded stream, with a bounded compression ratio.
- Redirect depth limit.

**Content validation:**

- Scheme restricted to HTTP/HTTPS only, unless a later documented decision approves otherwise.
- Content type validated from the response before processing; only allowlisted types (at minimum `text/html`, `text/plain`) are processed; everything else is a recorded per-source failure.
- URLs with userinfo, non-HTTP schemes, and unparseable forms are rejected outright.

Operational constraints: a dedicated fetch client (never shared with provider clients, `trust_env=False`, no cross-fetch cookies); provider credentials never appear in any fetched URL, header, or redirect target (SR-8); response bodies are never logged (SR-7).

### 15.6 Prompt-injection security contract (AD-032)

The Research Engine treats all fetched web content as **untrusted data**. Prompt-injection defense is part of the architecture, not a prompt-wording afterthought. The frozen containment model:

- The research model receives **no arbitrary tool access**. The pipeline is a fixed stage sequence; there is no mechanism by which retrieved text can select tools, request URLs, or alter control flow.
- Fetched content enters prompts only as untrusted **evidence/data**, inside explicit delimited regions that the system instructions declare to be quotable evidence whose embedded instructions must be ignored.
- System/application instructions never originate from, and are never concatenated with, fetched content. Retrieved text never appears in a system-role message (structurally enforced and tested, extending the Phase 2 T-7 invariant).
- Fetched text never becomes executable instructions or tool commands.
- Model outputs are validated **structurally as data** (typed schemas for plans, claims, citations) before any downstream use; URL-shaped strings in model output are text, never fetched.
- Source identity is pipeline-assigned and held in a registry independent of page content, so a page cannot impersonate or renumber sources.
- A page containing instruction-like text is the expected adversarial case (mandatory tests T-18, T-19).

### 15.7 Citation integrity contract (AD-033)

Citation integrity is a programmatic trust chain, not model self-report. The system must not rely on the model saying "this claim came from source X."

The frozen chain:

```text
source (persisted) → source_chunk/passage (persisted) → claim (persisted) → citation (persisted)
```

Contract requirements:

- Every citation references a known, persisted source/evidence object from **this** run; a citation to content never retrieved is constructively impossible (foreign-key registry).
- Every cited passage must exist as a persisted chunk; claims are traceable to evidence.
- Claim-to-passage support is verified **programmatically** (the quoted passage must verify against stored chunk text) before a citation is accepted — not taken from the model's word.
- Unsupported claims are detectable (explicit claim status) and are omitted or explicitly marked (FR-39).
- Model-supplied citation markers in the final answer are resolved against the registry at assembly; unresolvable markers are stripped and the affected claim marked — never silently re-pointed.
- Citation validation happens programmatically at build time and is covered by mandatory test T-15.

### 15.8 Data model contract (AD-034)

The Phase 3 persistence model is frozen at exactly five tables:

- **`research_runs`** — one research execution: question, plan, status, provider, token/cost totals, failure summary, timestamps. Owned by exactly one user; linked to a conversation where applicable.
- **`sources`** — normalized sources discovered/fetched for a run: URL, domain, title, snippet, status, failure reason, content hash, rank, retrieval timestamps, duplicate-of relationship.
- **`source_chunks`** — persisted evidence passages from a source: ordinal, text, position metadata.
- **`claims`** — extracted/synthesized claims for a run, with support status.
- **`citations`** — the relationship between claims and the specific source/chunk evidence supporting them.

The following are **not Phase 3 domain tables**: `ResearchQuery`, `SearchResult`, `FetchResult`, `ResearchReport`. They may exist as transient Python/domain concepts (a plan is JSONB on the run; a search result is a source at `pending` status; a fetch outcome is source state; the report is the run's `final_answer` plus the delivered assistant message), but they do not become persistence tables.

Additionally:

- Phase 3 **must** extend the existing `/delete_account` cascade so account deletion removes a user's entire research subtree (runs → sources/chunks/claims/citations). This is a hard requirement: without it, user isolation (SR-10) and deletion completeness (Phase 2 SR-9) break silently.
- All research data is user-owned and isolated; every read path is ownership-scoped.
- Phase 5 workspace persistence (projects, sessions, tags, bookmarks, source occurrences) is **out of scope**. The schema only carries the Phase 5 affordances that cost nothing now (`content_hash` for cross-run dedup; normalized URLs), per the Phase 5 prerequisite notes.

### 15.9 Failure model (frozen categories)

The failure taxonomy is frozen; per-stage mapping to the pipeline is an implementation detail. Classification legend: **R** = retriable (bounded), **N** = non-retriable, **P** = convertible to a partial research result, **F** = fatal to the entire run.

| Category | Class | Notes |
|---|---|---|
| Invalid research request | N, F | Rejected at intake with usage guidance; no run created |
| Search provider failure | R (bounded), P if partial query set succeeded, F if total | Degradation surfaced honestly (FR-8) |
| Search provider timeout | R (bounded), P, F if total | As above |
| No results | N, F | Clear, non-misleading failure (FR-41) |
| Fetch timeout | N, P | Recorded per source; run continues (FR-19) |
| Blocked/unsafe URL (SSRF) | **N (never retried)**, P | Recorded per source; no internal detail to the user (FR-42) |
| DNS resolution failure | N, P | Recorded per source |
| Redirect rejection | N, P | Recorded per source |
| Unsupported content type | N, P | Recorded per source |
| Response too large | N, P | Recorded per source |
| Extraction failure / malformed-unusable source | N, P | Source marked unusable (FR-23) |
| LLM failure (planning/summarization/synthesis) | R (bounded, existing taxonomy + fallback chain), F if exhausted | Partial artifacts kept; run status reflects stage |
| Token/budget exhaustion | N, F-with-partial-state | Run terminates with what it has and says so (FR-44) |
| Citation validation failure | N; affected claims omitted/marked | Answer never delivered with unverified citations (SR-9) |
| Persistence failure | N (within a run), F | Distinct user message; no fabricated completion |
| Delivery failure | R (existing `DeliveryError` path) | Answer persisted; redelivery re-sends stored answer |
| Cancellation/deadline | N, F-with-partial-state | Explicit deadline model; see § 15.11 |

A run with zero usable sources always fails clearly and never invents an answer (FR-41, SR-12). A partial run states how many sources failed and why, without internal URLs or stack detail (FR-42). Provider, fetch, and extraction failures produce distinguishable outcomes (FR-43).

### 15.10 Testing requirements — additions to § 8

- **Persian-language fixtures are mandatory** (frozen at P3.0): Persian research questions; Persian web content; mixed Persian/English pages; Unicode punctuation; RTL text; citations around Persian passages; extraction and normalization of Persian text. The product's primary language is Persian, so English-only fixtures would certify the wrong system. Fixtures are created with the stages that need them (P3.4 onward), not at P3.0.
- The mandatory security tests of § 8 are unchanged and remain the phase's gates: T-4…T-7 (SSRF), T-15 (citation integrity), T-18, T-19 (prompt injection).
- Fetcher tests must drive the client through a mocked transport or a loopback-only server: the suite's autouse socket guard fails any non-loopback connection, and that guard must stay green.
- New structural tests expected from the frozen contract: provider swap requires no orchestration change; source content never reaches a system-role message; account deletion removes research artifacts; citations resolve only within their run.

### 15.11 Celery execution constraint (AD-035 — recorded risk, not a config change)

The existing worker limits (120 s hard / 100 s soft, from Phase 1) are sized for a single chat LLM call, not a multi-stage research run with several provider, fetch, and synthesis calls. This is a **known Phase 3 implementation risk**:

- It must be resolved before production-grade research execution.
- The resolution belongs to the implementation stage that owns run execution (P3.9), which must determine the correct timeout/deadline model (per-task limits for the research task, stage checkpointing, or task chaining) without regressing chat's limits.
- Research cancellation/deadline behavior must be explicit: a run exceeding its time or cost budget terminates with partial state and says so (FR-44).
- P3.0 deliberately changes no Celery configuration.

### 15.12 Phase boundary (restated for the contract)

Not Phase 3: PDF/DOCX/file ingestion (Phase 4); workspaces, saved research, bookmarks, tags, sessions, cross-run source identity (Phase 5); multi-agent orchestration or any tool-use agent loop (Phase 6); Deep Research mode, exports, citation styles (Phase 7); API surface, dashboard (Phase 8); billing (Phase 9); embeddings/vector store (spec § 4); user-supplied URL fetching (SR-2); renaming `process_research` (AD-027); any change to Phase 2 chat behavior.

### 15.13 Next implementation stage

**P3.1 — Domain types / ports + migration 0002 + research deletion cascade extension.** Scope: `app/domain/research/` (pure types and ports per § 15.3/§ 15.7); the five-table migration per § 15.8 with constraints and indexes; repository ports; the `/delete_account` cascade extension with its real-PostgreSQL deletion test. No search provider, no fetcher, no extractor, no LLM calls in the research path. Evidence expectations: `alembic check` clean, migration round-trip, cross-user isolation and cascade tests, suite green.

---

## 16. P3.5 status — Deduplication + Ranking (2026-10-08)

**Implemented.** FR-24 … FR-30. Nothing else.

| Requirement | Status | Evidence |
|---|---|---|
| FR-24 Near-duplicate sources identified, incl. syndicated copies | **Partial** | Exact normalized-content fingerprints (SHA-256 over NFC + casefold + whitespace collapse) collapse identical text at different URLs. **A truncated or re-worded syndicated copy is not detected** — see § 16.4 |
| FR-25 Dedup does not discard distinct sources sharing boilerplate | ✅ | Fingerprints are taken over P3.4's *extracted main content*, never the raw page, so a shared nav menu or cookie banner cannot cause a merge. Test-covered |
| FR-26 Representative recorded, relationship retained | ✅ | `DuplicateGroup` keeps every member plus the chosen representative; `DedupRankingResult.candidates` keeps every input. One row per distinct source with `duplicate_of_id` is a persistence concern for the orchestration stage |
| FR-27 Documented, configurable ranking strategy | ✅ | Weights are named constants with stated rationale and sum to 1.0; `RANKING_MIN_QUALITY` is configurable. Deliberately **not** configurable as weights — see § 16.3 |
| FR-28 Relevance, quality, freshness, depth | ✅ | Five signals: `provider`, `title_match`, `content_match`, `freshness`, `quality` (depth + metadata). No signal is fabricated where the upstream contract supplies none |
| FR-29 Ranking replaceable | ✅ | `RankingPolicy` and `SourceDeduplication` are `runtime_checkable` `Protocol`s in `ports.py`; verified by test with a custom structural implementation |
| FR-30 Minimum quality threshold; exclusion recorded | ✅ | Below threshold ⇒ the entry stays in the output with an `excluded_reason` naming the measured quality and the threshold. Never silently dropped (FR-42) |
| T-12 Near-duplicates collapsed, distinct sources not | ✅ | Covered in both directions |
| T-13 Ranking deterministic and configurable; threshold excludes weak sources | ✅ | Covered, including idempotence, input-order independence, and repeated-run identity |

**Files:** `app/domain/research/dedup.py` (the policy), `app/domain/research/ports.py`
(two ports), `app/infrastructure/ranking/deterministic.py` (one stateless adapter +
factory), `app/domain/errors.py` (`DeduplicationError` family, all non-retryable),
`app/core/config.py` + `.env.example` (three settings),
`tests/test_research_dedup_ranking.py` (109) and
`tests/test_research_dedup_ranking_policy.py` (70).

**No migration. No model change. No new dependency. No schema expansion** — P3.1's
`sources` columns (`content_hash`, `duplicate_of_id`, `rank`, `quality_score`)
already carry what the output needs, so the frozen five-table contract was not
extended.

### 16.1 Canonicalization and the SSRF boundary

`canonical_url` **compares** two values. It does not validate against a scheme
allowlist, resolve a hostname, open a socket, or approve anything for retrieval.
The P3.3 boundary is untouched and was not duplicated, weakened, or bypassed. A
canonical URL is a comparison key, never a permission — stated explicitly because
this is the natural place for a second, weaker SSRF boundary to appear.

Tracking parameters are removed **only** when the name is in the explicit
`TRACKING_PARAMETERS` set. Anything outside it is preserved.

### 16.2 Determinism

No LLM, no randomness, no clock, no network, no database insertion order, no
set/dict iteration order. `reference_time` is an **input** to freshness, not
`datetime.now()`, so a stored candidate set reproduces its original order. Shuffling
the input produces identical scores and ordering; provider position survives as a
field. Both structural claims (no I/O imports, no clock call) are asserted by
parsing the module's AST.

### 16.3 Deliberate decisions worth recording

- **Weights are constants; only the threshold is configurable.** A configurable
  weight invites a value no benchmark justified, producing an ordering that is
  unexplainable while looking deliberate. The threshold is an operational judgement
  about acceptable evidence, which is a different kind of decision.
- **Representative selection is a lexicographic priority chain, not a weighted
  score**, so a strong third criterion cannot outvote "has usable content" — which
  is a precondition, not a preference.
- **Excluded and kept sources share one ordering.** Sorting excluded sources to the
  bottom would make the kept positions depend on which weak sources were in the batch.
- **The path is lowercased but the query string is not.** A judgement, documented in
  the audit rather than buried.

### 16.4 Limitation carried forward

Content identity is **exact only**. FR-24's near-duplicate detection covers
identical extracted text, not an edited copy. A shingle or MinHash rule would
extend it deterministically and stdlib-only, but merging two genuinely different
articles is a worse failure than missing a duplicate, and the threshold cannot be
set responsibly without a measurement. Not implemented; recorded in audit § N.13.

### 16.5 Not done at P3.5

Planning (P3.6), summarization and claim extraction (P3.7), synthesis and citations
(P3.8), the orchestrator, Celery task, `/research` handler, and any research LLM
call. No embeddings, no vector store, no crawling, no additional provider, no
persistence of ranking artifacts.

### 16.6 Next implementation stage

**P3.6 — Planning stage** (FR-1 … FR-4): `ResearchPlan` domain type, a validated
plan produced through the `LLMProvider` port, plan persistence on
`research_runs.plan`, and the open decision on whether `Supervisor.analyze_query`
is wired in or superseded (AD-026). Depends on P3.1, which is complete.

---

## 17. P3.6 status — Research Planning (2026-10-08)

**Implemented.** FR-1 … FR-4. Nothing else.

| Requirement | Status | Evidence |
|---|---|---|
| FR-1 Decompose a question into sub-questions, search strategies, and expected source types | ✅ | `ResearchPlan` carries `objective` + `steps`; each `ResearchStep` carries `question` (the sub-question), `query` (the search strategy), and `expected_source_types` |
| FR-2 Plan derived from the question, not hardcoded | ✅ | Two questions produce two different provider requests (asserted); the question planned is the **persisted** one from the run row |
| FR-3 Plan validated before execution; a degenerate plan fails clearly | ✅ | Empty objective, no steps, no search step, over-ceiling steps, duplicate ids, unknown/self dependency, and any cycle all rejected with a typed error at construction |
| FR-4 Plan persisted so a run can be inspected and reproduced | ✅ | `research_runs.plan` (JSONB, reused from P3.1); lossless round-trip asserted field by field against real PostgreSQL; reads are re-validated, not trusted |
| T-2 Planning produces a validated, non-degenerate plan | ✅ | `tests/test_research_planning*.py` — 204 tests |

**Files:** `app/domain/research/planning.py` (contract + validation),
`app/domain/research/ports.py` (`PlanningProvider`), `app/infrastructure/planning/`
(`LLMPlanner`, `create_planner`, versioned prompt), `app/domain/errors.py`
(`PlanningError` family), `app/services/research/planning.py` (`PlanningService`,
`load_plan`), `app/repositories/research.py` (`set_plan`), `app/core/config.py` +
`.env.example` (`PLAN_MAX_STEPS`, `PLAN_MAX_QUESTION_CHARS`, `PLAN_MAX_TOKENS`).

**No migration.** The `plan` JSONB column already existed from P3.1 and was reused.
No new table, no new column, no new dependency.

**Phase 2's `LLMProvider` was reused unchanged** — no second provider abstraction,
no new client, no SDK import in the planner. Its existing bounded retry and model
fallback chain (AD-024) are reused, so the planner adds **no retry of its own**.

### 17.1 AD-026 resolved — Option B

`Supervisor.analyze_query` is **superseded for planning** by the dedicated Phase 3
planner, and `Supervisor` is **retained unchanged**. The reason is shape, not safety:
`QueryAnalysis` holds a summary and sub-questions, with no objective, no steps, no
search strategy, no expected source type, and no dependency — so widening it would
mean changing a Phase 2 schema the chat path also depends on, to serve a Phase 3
requirement. `analyze_query` executes nothing, so reusing it would not have broken a
capability boundary; it simply would not have satisfied FR-1. Full evidence in audit
§ O.10.

### 17.2 Prompt-injection boundary

The question is untrusted input and is treated as data: `build_system_prompt` takes
only a step count, so no code path can place a question in the instructions; the
question is the user-role message inside explicit delimiters; and the planner has **no
tools at all**, which closes the "injected content steers a follow-up request" loop by
construction (AD-032). A plan has no URL or source field, so an injected link has
nowhere to land. This does **not** claim full injection immunity — containment for
retrieved web content belongs to P3.8 (§ C.2).

### 17.3 Not done at P3.6

No search, fetch, extraction, dedup, ranking, claims, synthesis, citations,
orchestration, Celery task, `/research` command, or Telegram behaviour. The planner
describes research; it performs none of it.

### 17.4 Next implementation stage

**P3.7 — Per-source summarization + claim extraction** (FR-31, FR-32): each selected
source summarized through the same `LLMProvider`, and claims extracted with a required
passage quote and chunk reference, so a claim is born with its evidence (AD-033).
Depends on P3.5, which is complete.

---

## 18. P3.7 status — Per-source summarization + claim extraction (2026-10-08)

**Implemented.** FR-31, FR-32. Nothing else.

| Requirement | Status | Evidence |
|---|---|---|
| FR-31 Each retained source is summarized independently | ✅ | `SourceSummarizer` takes **one** `SourceContent`, so a second source is not in reach; `SourceSummary` records the `source_id` the application supplied. No cross-source synthesis exists in the module |
| FR-32 Claims carry an explicit link to the supporting passage | ✅ | Every accepted claim is a `VerifiedClaim` holding an `EvidenceReference` (`source_id`, `chunk_id`, `ordinal`, `quote`); the persisted citation carries the same quote. A candidate cannot become a claim without one |
| No evidence → no accepted claim (AD-033) | ✅ | `ClaimExtractor` returns `ClaimCandidate` values; only `verify_claims()` can produce a `VerifiedClaim`. Enforced in the domain and covered by 322 tests |
| The quote is verified programmatically | ✅ | `verify_claims` compares the quote against the stored passage text under `normalize_evidence_text` and nothing else. No similarity score, no second model, no LLM adjudication |
| The model cannot self-certify | ✅ | The response schemas are closed; `verified`, `confidence`, `citation`, `source_id` and every other unknown key are refused with a typed error. `claims.confidence` is left `NULL` because nothing at P3.7 measures it |
| Prompt-injection containment (SR-3, SR-4) | ✅ | Retrieved content and source metadata are untrusted data in the user role inside delimiters; both system-prompt builders take **numbers only**; the adapter has no tool surface (asserted from its import list and constructor signature) |
| Summary is not evidence | ✅ | `sources.summary` is a text column with no relation to `citations`, so it cannot be cited; asserted |
| Token budgeting (§ 16) | ✅ | `plan_chunk_windows` packs whole passages into bounded windows using Phase 2's `CharRatioTokenEstimator`; nothing that did not fit is dropped silently — the shortfall is counted and reported |
| Determinism | ✅ | Canonical `(ordinal, chunk_id)` ordering, no clock, no randomness, no set iteration. **Not claimed:** the model's own output |

**Files:** `app/domain/research/evidence.py` (contract + verification + window
planner), `app/domain/research/ports.py` (`SourceSummarizer`, `ClaimExtractor`),
`app/infrastructure/evidence/` (`LLMSourceSummarizer`, `LLMClaimExtractor`,
versioned prompts, two factories), `app/services/research/evidence.py`
(`SourceEvidenceService`, `load_source_evidence`), `app/domain/errors.py` (the
`EvidenceError` family), `app/core/config.py` + `.env.example` (six settings), and
migration `0003_research_evidence`.

**Migration 0003 — the smallest change the frozen contract requires.** `sources.summary`
(FR-31 has to persist somewhere, and the summary is a property of the source);
`citations.quote` (`NOT NULL` — the frozen schema had nowhere to keep the passage, and
`citations.location` is a 255-character *locator*, a different thing); and a composite
foreign key `fk_citations_chunk_id_source_id_source_chunks` on `(chunk_id, source_id)`
with the unique constraint it requires. **No table was added**: the five-table model of
AD-034 and § 15.8 is unchanged. `alembic check` reports no drift and the migration
round-trips.

### 18.1 Where the claim→passage link actually lives

The frozen schema defines the relationship through `citations`, not through `claims`:
`claims` carries `(run_id, text, status, confidence)` and `citations` carries
`(claim_id, source_id, chunk_id)`. That design is **preserved**, and the invariants map
onto it as:

| Invariant (§ 14) | Where it is enforced |
|---|---|
| a claim has a source and a passage | `citations.source_id` / `citations.chunk_id`, both `NOT NULL` foreign keys |
| the passage belongs to that source | `fk_citations_chunk_id_source_id_source_chunks` — the database refuses a citation whose passage belongs to another source. Before this revision the two foreign keys were independent and that insert succeeded |
| a citation carries the passage quote | `citations.quote NOT NULL`, required by `CitationRepository.add`, refused before the row exists when empty |
| the quote is inside the passage text | **not expressible in SQL** (it compares one row's text against another's). Enforced by `verify_claims` before persistence and re-checked against the loaded passage by `SourceEvidenceService._assert_evidence_is_real`; asserted by test against the stored rows |
| a claim exists without no citation | **not expressible as a constraint.** The service writes the claim row and its citation row in the same transaction, so no path creates one without the other; asserted by test (`SELECT … LEFT JOIN citations`) |

### 18.2 Decisions worth recording

- **Two ports, two calls.** Summarization and claim extraction are separate
  capabilities with separate failure modes, and the frozen failure model wants a
  summarization failure to be distinguishable from a claim failure. The cost is one
  extra provider call per source; the benefit is that a source whose claims cannot be
  extracted still keeps its summary.
- **Verification lives in the service, not behind the port.** `ClaimExtractor` returns
  candidates; the *application layer* calls `verify_claims`. Putting verification inside
  an adapter would put the ability to accept a claim in the component closest to
  untrusted text.
- **A reference is never resolved against the database.** The passage set supplied for
  the source is the only authority. A chunk id belonging to another source is rejected
  exactly as an id that does not exist is — resolving it "because it exists" is the
  behaviour that lets a claim cite evidence from outside its own source.
- **Quotes are normalized explicitly and minimally** (NFC + whitespace collapse):
  nothing is lower-cased, transliterated, or stripped of punctuation or digits.
  `MIN_QUOTE_CHARS = 16` exists because a substring test alone is satisfied by `"the"` —
  text that is present and supports nothing.
- **`claims.confidence` stays `NULL`.** No stage measures confidence here, and a stored
  model-supplied number would be the self-certification this stage forbids.

### 18.3 Verification (2026-10-08, actual output)

```text
P3.7 targeted tests:   322 passed
  tests/test_research_evidence_domain.py          180
  tests/test_research_evidence_summarization.py    44
  tests/test_research_evidence_claims.py           48
  tests/test_research_evidence_security.py         32
  tests/test_research_evidence_persistence.py      18
Full repository tests: 1492 passed, 21 failed
ruff check (P3.7 files):  clean
mypy (P3.7 files):       clean (7 sources)
alembic check:            No new upgrade operations detected
```

**The 21 failures are pre-existing and are not attributed to P3.7:**

| Failure | Count | Cause |
|---|---|---|
| `_embedded_ipv6` undefined in `app/infrastructure/fetch/ssrf.py` | 17 + 1 | P3.3 defect, reported and unchanged (also 2 `ruff` and 3 `mypy` errors) |
| `FetchHTTPStatusError.__init__() missing keyword-only 'status_code'` in `test_telegram_chat_flow` | 1 | P3.3 defect, unchanged |
| `idna`/`trafilatura` imported but not declared in `requirements.txt` | 1 | P3.4 debt, unchanged — **no dependency was installed or added at P3.7** |
| `test_the_system_resolver_raises_a_typed_error_for_an_unknown_name` | 1 | Pre-existing P3.3 test defect: it performs a **real DNS lookup**, and this machine's resolver answers `nonexistent.invalid` with `198.18.0.26` (a benchmarking address the P3.3 policy correctly blocks). Environment-dependent, not code-dependent |

`make verify` therefore fails at `lint` and `typecheck` on the two P3.3 files and at
`test` on the above. **The repository is not green and is not claimed to be.** P3.7
introduced no new failure: the failure set is identical to the pre-P3.7 baseline except
for the DNS test, which depends on the machine's resolver.

Three existing test files were updated because the contract changed, not to make a suite
green: `test_research_domain` (a citation now requires its quote),
`test_research_persistence` and `test_data_deletion` (citation writes supply a quote),
`test_migrations_and_isolation` (the migration head is now `0003_research_evidence`).

### 18.4 Known limitations

1. **No live-provider verification.** Summarization and claim extraction were tested
   against a fake provider only. Whether the configured model emits conforming JSON for
   these two prompts is **NOT VERIFIED** — the same measurement gap AD-036 records for
   search quality and O.17 records for planning.
2. **Containment is architectural, not immunity.** A hostile page can still influence
   the *content* of a summary or a claim. What cannot happen is the content reaching the
   instructions, or model output causing an action, or a claim being accepted without a
   passage that verifies. A page that emits the closing delimiter itself can also make
   the prompt read differently; it still cannot cite evidence that does not exist.
3. **Coverage is bounded and reported, not eliminated.** A source larger than the
   configured budgets is only partly summarized and only partly examined for claims.
   `summary_truncated` / `claim_truncated` say so; a caller that ignores them will
   present partial coverage as complete.
4. **Chunking of extracted text is still not built.** P3.7 consumes persisted
   `source_chunks` rows; no stage produces them from P3.4's `ExtractedContent` yet, so
   the end-to-end path from a fetched page to a claim is **not exercised** and is not
   claimed.
5. **`_rejection_reason` is ordered, and the order is the contract.** A candidate with
   three problems is rejected for the first one. Fine for reporting; a caller that wants
   every problem would need a different model.

### 18.5 Not done at P3.7

No synthesis, no cross-source comparison or contradiction detection (FR-33, FR-34),
no citation construction or answer assembly (FR-36 … FR-40), no orchestration, no Celery
task, no `/research` command, no Telegram behaviour, no workspace, no embeddings or
vector search, no agent loop, no second LLM client, and no change to the P3.3 SSRF
boundary or the P3.5 dedup/ranking policy.

### 18.6 Next implementation stage

**P3.9 — Orchestration + `/research` trigger.** Join the completed stages
(P3.3 fetch → P3.4 extract → **the still-missing chunk producer** → P3.5 dedup and
rank → P3.7 evidence → P3.8 synthesis) into one ordered run, implement the
`/research <question>` contract frozen in § 15.1 (AD-029), resolve the Celery deadline
model (AD-035), and decide the `completed` vs `partial` reporting that P3.8
deliberately left open. Depends on P3.8, which is complete — **except** for the chunk
producer, which no stage has built and which P3.9 must supply or the path from a
fetched page to an answer stays unexercised.

---

## 19. P3.8 status — Synthesis + Citations (2026-10-08)

**Implemented.** FR-33 … FR-39. Nothing else.

| Requirement | Status | Evidence |
|---|---|---|
| FR-33 Cross-source comparison identifies agreement, contradiction, and silence | ✅ | One `SynthesisProvider` call over the run's whole bounded evidence set — P3.7's extractor was deliberately single-source so it *could not* compare two; agreement is several citations on one section, silence is a source that contributes no citation, and both are assertable from `answer.cited_source_ids` |
| FR-34 Contradictions are reported rather than silently reconciled | ✅ | A reported conflict keeps **both** sides with **all** their citations, in the answer and in the persisted payload; `claims.status='contradicted'` is deliberately **unused**, because marking a side reconciles the conflict silently. Asserted: both statuses remain `supported` and both citation rows survive |
| FR-35 The answer is assembled from the claim set, not from raw pages | ✅ | The synthesis input carries verified claim + verified quote + source metadata. Passage text is never rendered: the quote is a literal substring of it and is what a citation names |
| FR-36 Every factual claim carries at least one citation | ✅ | Deterministic policy at **section** granularity: every section must name at least one reference that resolves. `VerifiedSection` cannot be constructed without one. **No semantic truth detection is attempted** — see § 19.3 |
| FR-37 Each citation resolves to a specific source and location | ✅ | `VerifiedCitation` carries the persisted `source_id`, `chunk_id`, `ordinal`, and `location`; every field read from rows, never from the model |
| FR-38 Every citation is verifiable — it points to content actually retrieved in this run | ✅ | The registry is scoped by `claims.run_id`, so a foreign citation is **absent** rather than filtered; § 8's Rules 1–8 each have a test class in `tests/test_research_synthesis_citations.py` |
| FR-39 An unsupported claim is omitted or explicitly marked | **Partial** | An unsupported *citation* is refused. FR-39's other half — marking a section the evidence does not support — requires deciding that a section's prose follows from its passage, which no contract here can do. Recorded in audit § Q.13 item 2 |
| FR-40 Citation rendering respects Telegram length limits | **Not Implemented** | Rendering is presentation: `SynthesizedAnswer.plain_text` carries **no citation markers**, and Markdown/Telegram formatting and length bounds belong to the delivery stage (§ 3, P3.9) |
| T-14 Every claim in the final answer has at least one citation | ✅ | `TestRule8Completeness` |
| T-15 Every citation resolves to content actually retrieved in that run | ✅ | `TestRule1CitationMustExist`, `TestRule2CitationMustBelongToThisRun`, `TestRule3EvidenceChain`, `TestRule5`, `TestRule6`, `TestRule7` |
| T-17 Contradictory sources are reported, not silently reconciled | ✅ | `TestConflictPreservation`, `TestContradictionHandling` |
| T-18 / T-19 Prompt injection cannot alter behaviour; retrieved content never enters the system instruction | ✅ | `TestHostileContentIsData` (EN/FA/mixed × title, domain, passage), `TestPipelineAssignedIdentity`, `TestHostileTextCannotAlterValidation` |

**Files:** `app/domain/research/synthesis.py` (contract, registry, context planner,
verification), `app/domain/research/ports.py` (`SynthesisProvider`,
`list_citable_for_run`, `set_synthesis`), `app/infrastructure/synthesis/`
(`LLMSynthesizer`, `create_synthesizer`, versioned prompt), `app/services/research/synthesis.py`
(`SynthesisService`, `SynthesisOutcome`, `load_synthesis`), `app/domain/errors.py` (the
`SynthesisError`, `CitationIntegrityError`, and `ContradictionError` families plus
`CitationFailureKind`/`CitationFailure`), `app/core/config.py` + `.env.example` (four
`SYNTHESIS_*` settings), and migration `0004_research_synthesis`.

**Migration 0004 — one nullable JSONB column.** `research_runs.synthesis`. **No table
was added**: AD-034's five-table model and § 15.8 are unchanged. `alembic check` reports
no drift and the migration round-trips.

### 19.1 The trust chain, and the one thing the model may do

```text
verified claims + citations  (persisted)
    → CitationRegistry        the only authority for what a citation means
    → SynthesisProposal       sections + citation_ids (integers) + reported conflicts
    → verify_synthesis()      the decision
    → SynthesizedAnswer       constructible only by the verifier
    → one transaction         synthesis + final_answer + status='completed'
```

The proposal schema is closed and carries **identifiers only**. There is no `url`,
`quote`, `source`, `source_id`, `confidence`, or `verified` field at either level, and
unknown keys are refused rather than ignored — the structural reason a model cannot
invent a source or self-certify a citation. `SynthesizedAnswer` and the three trusted
value types inherit a guard that refuses any construction not carrying a module-private
token, for `Model(...)` and `model_validate(...)` alike; `from_payload` re-verifies a
stored answer against the registry, so a hand-edited row cannot be read back as
trustworthy. Full detail and the five defect-injection results are in audit § Q.

### 19.2 Fail-closed is a deliberate narrowing of § 15.7

§ 15.7 says an unresolvable marker is "stripped and the affected claim marked". P3.8
refuses the **whole answer** instead (AD-041). A fabricated citation is an attack
signal, not a formatting defect, and delivering the remainder of a proposal that tried
to deceive is the failure SR-9 names. Every refusal is still reported — the typed error
carries the full ordered list of findings so counts by category are available (FR-42) —
but nothing is delivered and the run is not marked successful. P3.9 may adopt a
partial-delivery policy; that is a delivery decision this stage does not own.

### 19.3 Citation completeness, stated exactly

One deterministic rule, and one thing that is explicitly **not** claimed:

> Every section must name at least one citation reference that resolves to persisted
> evidence of this run.

Duplicate references inside a section collapse on first occurrence and are counted
rather than refused — repeating a real citation is harmless, and failing closed on it
would let a model veto its own answer. **No semantic truth detection is attempted or
claimed**: the contract can establish that a citation exists, belongs to this run, was
shown to the model, and carries a verified passage — it cannot establish that a
section's sentences follow from that passage.

### 19.4 Verification (2026-10-08, actual output)

```text
P3.8 targeted tests:   340 passed
  tests/test_research_synthesis_domain.py          143
  tests/test_research_synthesis_provider.py         53
  tests/test_research_synthesis_citations.py        38
  tests/test_research_synthesis_security.py         50
  tests/test_research_synthesis_persistence.py     32
  tests/test_research_synthesis_cross_source.py    24
Full repository tests: 1833 passed, 20 failed  ← figure at P3.8; see § 21.8 for the current baseline
ruff check (P3.8 files):  clean
mypy (P3.8 files):       clean
alembic check:            No new upgrade operations detected
```

**The 20 failures are pre-existing and are not attributed to P3.8.** The pre-P3.8
baseline was `1492 passed, 21 failed`; the 20 failures are the same set, and the
twenty-first (an environment-dependent P3.3 DNS test) depends on the machine's resolver
rather than on the code — it failed before P3.8 and passed after. Full breakdown in
audit § Q.14. `make verify` therefore still fails at `lint` and `typecheck` on the two
P3.3 files and at `test` on the above. **The repository is not green and is not
claimed to be.** P3.8 introduced no new failure.

### 19.5 Known limitations

1. **No live-provider verification.** Tested against a fake provider only; whether the
   configured model emits conforming JSON for this third prompt is **NOT VERIFIED** —
   the same measurement gap AD-036, O.17, and P.13 record.
2. **The answer's prose is model text and may be wrong.** The contract bounds what a
   citation may mean; it does not check that the prose follows from the passage.
3. **Coverage is bounded, not complete.** `evidence_offered` / `evidence_available` /
   `evidence_truncated` exist so a caller cannot present a truncated synthesis as
   exhaustive; a caller that ignores them still can.
4. **`claims.status='contradicted'` remains unused**, deliberately — see the table
   above. Recorded so it is not read as an oversight.
5. **The page-to-answer path is still not exercised end to end.** P3.7 consumes
   persisted `source_chunks` and P3.8 consumes persisted citations, and **no stage yet
   produces `source_chunks` from P3.4's `ExtractedContent`** — unchanged from P3.7.
6. **`research_runs.synthesis` grows without bound** within a run. One row per run,
   deleted with the user; no column-size limit is enforced. Phase 5's cross-run history
   is where this would start to matter.

### 19.6 Not done at P3.8

No orchestration, no Celery task, no `/research` command, no Telegram behaviour, no
citation rendering or citation styles (FR-40; Phase 7 owns styles), no search execution,
no fetching, no chunking, no scheduling, no deep-research iteration, no agent loop, no
embeddings or vector store, no workspace tables, no second LLM client, and no change to
the P3.3 SSRF boundary, the P3.5 dedup/ranking policy, or P3.7's evidence contract.

### 19.7 Next implementation stage

**P3.9 — Orchestration + `/research` trigger.** Join the completed stages into one
ordered run, implement the `/research <question>` contract frozen in § 15.1 (AD-029),
resolve the Celery deadline model (AD-035), and decide the `completed` vs `partial`
reporting P3.8 left open. **Prerequisite gap:** the chunk producer that writes
`source_chunks` from extracted content does not exist in any stage, so without it the
path from a fetched page to a cited answer stays unexercised. Depends on P3.8, which is
complete.

---

## 20. P3.9 read-only audit + contract freeze (2026-10-08)

**Nothing implemented.** P3.9 was a read-only stage. No orchestration, no
`/research`, no chunk producer, no migration, no Celery configuration change, no
dependency change. Production code, tests, and schema are untouched; the failed
baseline is unchanged and is **not** a P3.9 regression.

**Outcome:** `READY WITH PREREQUISITES`. Full evidence, the contract matrix, the
failure matrix, the frozen `/research` contract, and the implementation contract
are in the canonical Phase 3 audit § R (appended, append-only):

* **`/research` contract (§ R.4)** — frozen on input shape, empty/whitespace/Unicode
  behaviour, and the asynchronous `accepted → queued → running → terminal` lifecycle.
  Two values were left explicitly **UNRESOLVED** rather than invented at P3.9: the
  inbound question length cap (`PLAN_MAX_QUESTION_CHARS=2000` vs
  `CHAT_MAX_MESSAGE_CHARS=4000`) and the stricter research rate allowance § 15.1
  permits. **Both are now decided — see § 21 (P3.9-C): AD-046 and AD-047.**
* **Run lifecycle (§ R.5)** — the five statuses and the DB CHECK already support
  `partial`, but **no component writes `running` or `partial`**, and
  `ResearchRunRepositoryImpl.set_status` is the only research write that is **not
  ownership-scoped**. Cancellation has no representation at all.
* **SourceChunk gap (§ R.6)** — resolved as **absent**. `uow.source_chunks.add` has
  zero callers in `app/` *and* `tests/`; nothing chunks `ExtractedContent`.
  `app/services/formatting.py` is a Telegram message splitter and
  `plan_chunk_windows` is a prompt-window planner — neither is a producer.
* **Celery / AD-035 (§ R.11)** — the shape is decidable (one orchestration task,
  all stages inside it, on the worker's long-lived loop; chained stage tasks are
  not available because `task_ignore_result=True` and nothing reads results). The
  **deadline value is not**: 100 s soft / 120 s hard is inherited globally, and
  `task_acks_late=True` + `task_reject_on_worker_lost=True` means a hard kill
  **redelivers and re-executes the run from the start**. **Both were resolved at
  P3.9-C — the mechanism is AD-045 (a cooperative in-coroutine deadline, because the
  signal-based soft limit does not unwind the unit of work) and the numeric value
  remains blocked pending a measured run; see § 21.**
* **P3.5 gap found during the audit (§ R.9)** — `SourceRepository` has **no writer**
  for `rank`, `quality_score`, `content_hash`, or `duplicate_of_id`, so ranking
  cannot be persisted. This also silently de-ranks the synthesis context, which
  orders and budgets on `sources.rank`.
* **Mandatory prerequisites (§ R.18)** — B1 chunk producer, B2 ranking writers,
  B3 research composition root (`build_container` wires only `ChatService`), B4
  `running`/`partial` writers + `set_status` ownership, B5 AD-035 deadline
  decision, B6 question length cap, B7 idempotency key. **B1–B4 were discharged at
  P3.9-A/P3.9-B; B5 and B6 were decided at P3.9-C (§ 21); B7 remains open.**
* **Account deletion (§ R.16)** — **complete and FROZEN**, not a prerequisite.
* **Baseline verification (§ R.21)** — `pytest` 1833 passed / 20 failed (the exact
  pre-P3.8 set), `ruff` 2 errors and `mypy` 4 errors, all in the two P3.3 files.
  `alembic check` fails only because the *developer* database sits at
  `0001_phase2`; against the database the suite migrates to head it reports
  **no drift**. Nothing was fixed (§ 30 forbids it).

**History preserved.** § 15's frozen contract and § 16–§ 19's stage sections are
unchanged. This section records what P3.9 found and froze; it does not restate
what P3.1–P3.8 built.

---

## 21. P3.9-C — B5 / B6 operational decisions (2026-10-09)

**Nothing implemented.** A decision-and-documentation stage: no orchestrator, no
`/research` route, no rate limiter, no timeout handling, no Celery configuration
change, no schema change, no test change. Full evidence, the option comparison,
and the measurement gaps are in the canonical Phase 3 audit **§ S** (appended,
append-only).

**Outcome:** **B6 resolved. B5 resolved as a policy, BLOCKED as a number.**

### 21.1 Decision summary

| ID | Decision | Status |
|---|---|---|
| **AD-045** | A research run's deadline is enforced by a **cooperative, in-coroutine deadline** in the orchestrator, checked at stage boundaries and before each provider call. Celery's soft limit is a **backstop, not the mechanism**, and the research task must catch `SoftTimeLimitExceeded` **without** letting it reach the retry path. Celery configuration is unchanged. | **Frozen** |
| **AD-046** | `/research` intake is bounded by the existing **`PLAN_MAX_QUESTION_CHARS`** (default **2000**). No new setting is introduced. | **Frozen** |
| **AD-047** | Research gets a **dedicated rate-limit allowance** — separate window, daily allowance, and in-flight budget — keyed by the same Telegram user id, enforced at intake, **separate from chat's**. Numeric values **BLOCKED**. | **Policy frozen; values blocked** |

### 21.2 B5 — the deadline policy (AD-045)

This resolves AD-035's *deadline model*. AD-043 already resolved its *shape* (one
Celery task per run, all stages inside it).

**Why not a measured hard deadline.** No measurement exists and none can be made
in this environment: no run has executed, and `SEARCH_API_KEY` is absent from
`.env` so `search_enabled` is `False`. A number chosen now would be invented,
which `DEVELOPMENT_RULES.md` § 6 forbids.

**Why not chained stage tasks.** Unavailable: `task_ignore_result=True` with
nothing reading results, no routing design, no per-stage state column. Already
rejected as AD-043.

**Why the deadline must be cooperative — the finding that decides this.** Celery
raises the soft limit through a POSIX signal handled in the worker *process*
(`billiard/pool.py:227-228`). Because this repository's task body is synchronous
and blocks in `loop.run_until_complete()` (`asyncio_runtime.py:70-82`), the
exception is raised **in the awaiting frame, not inside the coroutine**. Verified
by execution against the repository's own async bridge: on a soft timeout the
in-flight `SqlAlchemyUnitOfWork.__aexit__` (`unit_of_work.py:54-72`) **never
runs**, so no commit, no rollback, and no session close occur, and the coroutine is
left `PENDING`. The control case — `asyncio.timeout`, the mechanism the fetch
stage already uses — unwinds correctly.

An in-coroutine deadline is therefore the only mechanism that can *persist
partial results before the hard limit*. It is also what the existing transaction
discipline already supports: every research service closes its read transaction
before any provider call and commits its writes separately
(`planning.py:79-80`, `chunking.py:194-211`, `evidence.py:257-269`,
`synthesis.py:260-264`), so a deadline checked *between* stages lands on a
committed boundary. This is exactly what chat cannot do — its turn is one
transaction.

**Redelivery.** `task_acks_late=True` + `task_reject_on_worker_lost=True` means a
hard-killed run is **re-delivered and re-executed from the start**, and a hard
kill produces no partial status and no terminal message because the process dies
mid-transaction. Consequences: the cooperative deadline is the *only* source of a
user-visible outcome, and **B7 (idempotency) is a prerequisite of B5, not a
parallel concern**.

**Lifecycle fit.** `partial` is reachable only from `running` (verified by
executing `ResearchRunLifecycle.validate`: `queued→running` ACCEPT,
`running→partial` ACCEPT, `completed→partial` REFUSED, `partial→completed`
REFUSED, `queued→completed` REFUSED). No schema change and no new status is
required; the orchestrator writes `running` before the first stage.

### 21.3 B5 — what is BLOCKED, and what is missing

The **numeric deadline value is BLOCKED**. Nothing was invented in its place.
Missing evidence, all of which requires executing a research run:

1. Wall-clock duration of each stage in the deployment environment. No such
   measurement exists; the audit's § K.3 latency samples are unauthenticated
   handshake timings and explicitly bound connectivity only.
2. A chosen bound on **candidates fetched per run**. `RESEARCH_MAX_SOURCES` does
   not exist, so per-run cost is `SEARCH_MAX_RESULTS × plan search steps` and any
   deadline would be arbitrary. Adding that setting is P3.9-D work.

   > **Superseded (2026-10-10, P3.9-D):** `RESEARCH_MAX_SOURCES` exists (default
   > 5, ceiling 20) and the orchestrator enforces it over fetched *and* selected
   > candidates. Items 1 and 3 still hold, so the numeric deadline stays blocked.
3. A credentialed end-to-end run (`SEARCH_API_KEY` absent here).

### 21.4 B6 — question length (AD-046), FROZEN

**`PLAN_MAX_QUESTION_CHARS` (default 2000) governs `/research` intake.** The two
candidates were both verified in code — `CHAT_MAX_MESSAGE_CHARS = 4000`
(`config.py:87`, chat turns only, enforced at `chat.py:140-145`) and
`PLAN_MAX_QUESTION_CHARS = 2000` (`config.py:241`, enforced at
`planner.py:112-119`).

Why 2000, on evidence:

- **aiogram imposes no cap.** Its `Command` filter splits with
  `text.split(maxsplit=1)` and passes the whole tail as `args` (verified: a
  5000-character argument arrives intact). The framework neither supports nor
  opposes either value.
- **Telegram's 4096 governs outgoing delivery only** (`formatting.py:16`,
  `delivery.py:42-43`); nothing in the repository rejects an inbound message by
  length.
- **The planner already refuses above 2000, before any provider call.** Accepting
  4000 at intake would create a run row, a queue message, and a worker execution
  before rejecting an input that could have been refused with no state change —
  the opposite of § 15.9 ("Invalid research request → rejected at intake,
  **no run created**").
- **Token budgeting does not discriminate between the two**, and this is stated
  rather than dressed up: measured with the repository's own estimator, a
  2000-character question makes the planning prompt ≈1029 estimated tokens and a
  4000-character one ≈1529. Both are small; `PLAN_MAX_TOKENS = 1500` is an
  **output** bound, not an input bound. The decision rests on intake consistency,
  not on a budget claim.
- **No third number.** Introducing a new research-specific limit would be an
  arbitrary value with no evidence behind it. Reusing the existing setting means
  one number governs the question end to end.

**Known limitation, recorded not fixed:** `PlanningInputError` has no entry in
`failures.MESSAGES`, so `user_message_for` maps it to `GENERIC`. An intake
rejection should say the question is too long. Fixing that means editing
`app/services/failures.py` — P3.9-D.

> **Superseded (2026-10-10, P3.9-D):** the mapping exists, and the handler
> additionally rejects an empty question with usage text before any state is
> created. See § 23.

### 21.5 B6 — rate limiting (AD-047), policy FROZEN / values BLOCKED

Research gets a **dedicated allowance**, enforced at intake, keyed by the same
Telegram user id, **separate from chat's**:

| Aspect | Decision |
|---|---|
| Identity | Telegram user id (per-user, not per-chat) — unchanged from chat; matches § 15.1's ownership rule and SR-10 |
| Separate from chat? | **Yes**, a separate window, daily allowance, and in-flight budget. Chat's controls are unchanged. § 15.1 permits a stricter research allowance because a run is far more expensive; FR-9/SR-5 require per-run cost limits |
| Requests per interval | **BLOCKED** — see below |
| Failed / rejected / cancelled | **Counted at intake**, exactly as chat does, and not refunded on later failure (`rate_limit.py:83-85`) |
| Concurrency | A **separate in-flight budget** for research: a run occupies a worker for minutes, chat's for one LLM call, and `--concurrency=1` + `worker_prefetch_multiplier=1` on one shared queue means a long run blocks all chat traffic |
| Message when exceeded | Reuse the existing convention — `RATE_LIMITED_TEXT` with `retry_after_seconds`, `GLOBAL_CAPACITY_TEXT` for capacity. No new copy |
| Backend unavailable | **Not applicable** — there is no backend. State is in-process, so the real property is fail-open on restart, recorded as a known limitation |

**Why the numbers are BLOCKED:** no cost per research run is modelled anywhere;
`research_runs.prompt_tokens`/`completion_tokens` exist and `set_status` accepts
them, but **no component writes them**; no run has been measured; the per-run
provider call count is bounded only by configuration nobody has fixed in
production; and **no repository document states a spend ceiling per user per day**
— that is a product/operational decision, not a technical one. P3.0's § E.5
deliberately deferred it and no value was recorded.

### 21.6 A defect found while establishing this evidence

**The global in-flight counter never returns to zero in the API process.**
`check()` runs in the API process (`handlers.py:226`) and increments `_in_flight`
on the container built at `main.py:71`; `release()` runs in the **Celery worker
process** (`research_task.py:111`) against *its own* container
(`container.py:211-214`). `docker-compose.yml` runs `api` and `worker` as
separate services, so these are two different `RateLimiter` instances. Verified by
execution: after 8 lifetime messages the API process refuses every user
permanently with `global_capacity`.

This is **pre-existing Phase 2 behaviour, not a Phase 3 regression**, and the
existing tests cannot detect it because they drive the handler and the task in a
single process. **Reported, not fixed** — correcting it is outside this stage's
scope. Any research concurrency control inherits the defect until it is fixed, so
it is recorded as prerequisite **B6-P4**.

### 21.7 Prerequisite state after P3.9-C

| # | Prerequisite | State |
|---|---|---|
| B1 / B2 | Chunk production / ranking persistence | **Resolved** (P3.9-A); not yet wired into a pipeline |
| B3 / B4 | Composition root / run-state writers | **Partial** (P3.9-B); `partial` still written by nothing |
| **B5** | Deadline decision | **Policy RESOLVED (AD-045); value BLOCKED** |
| **B6** | Question length + research rate allowance | **RESOLVED (AD-046, AD-047); rate numbers BLOCKED** |
| B7 | Run idempotency key | Open — now a **prerequisite of B5** |

New prerequisites recorded at P3.9-C: B5-P1 (B7 idempotency), B5-P2 (bound on
candidates fetched per run), B5-P3 (a `partial` writer), B5-P4 (distinct terminal
messages for no-evidence / pipeline-failed / deadline), B6-P1 (dedicated research
limiter), B6-P2 (`user_message_for` entries for the research failure family),
**B6-P4 (fix the in-flight cross-process leak)**.

### 21.8 Verification and preserved baseline

```text
pytest -q             1959 passed, 22 failed  (1981 collected)
ruff check app tests  4 errors
mypy app              12 errors in 4 files
```

The `1833 / 20` figures in § 20 were true at P3.9; the current numbers differ
because **P3.9-A and P3.9-B added test modules and code** — P3.9-C changed no
test and no source. Of the 22: 18 are P3.3 `_embedded_ipv6` debt, 1 is P3.3
`FetchHTTPStatusError`, 1 is P3.4 undeclared dependencies, and 2 are P3.9-B
(`test_concurrent_transition_one_wins`; and the P3.8-era
`test_the_service_cannot_write_a_status_without_verifying`, whose expectation the
P3.9-A fixture change made obsolete). The `lifecycle.py` `describe` redefinition
behind one `ruff` and eight `mypy` errors is likewise P3.9-B, reported and not
fixed. **The repository is not green and is not claimed to be.**

**Superseded (2026-10-09):** the figures above were already stale against the working
tree. The measured pre-fix baseline was **4 failed, 1977 passed, 1 error**; all four
failures and all `ruff`/`mypy` errors were fixed in the P3.9 stabilization pass, and the
suite is now green. See § 22.

---

## 22. P3.9 stabilization — defect fixes and green baseline (2026-10-09)

A bug-fix/stabilization pass over the P3.9-A/P3.9-B working tree. No P3.9-D (or later)
work was started. The goal was to establish a trustworthy measured baseline and fix the
confirmed, in-scope defects it exposed.

### 22.1 Measured baseline before the pass

The figures recorded at § 21.8 (`1959 passed / 22 failed`) were stale with respect to the
working tree. The measured pre-fix baseline was:

```text
pytest -q             4 failed, 1977 passed, 1 error  (1981 collected)
ruff check app tests  2 errors   (F811 in lifecycle.py:265; RUF059 in a P3.9-A test)
mypy app              9 errors in 3 files
                      (7 in lifecycle.py; 1 fetch/client.py:511; 1 bot/container.py:67)
```

The 18 `_embedded_ipv6` failures § 21.8 attributed to P3.3 do not reproduce: the name no
longer exists anywhere in `app/` or `tests/`, so the earlier figure described a tree in
which the P3.3 fix had not yet landed. The actual failures were four, all reproduced.

### 22.2 Defects fixed

| # | Defect | Fix | File |
|---|--------|-----|------|
| 1 | `describe` defined twice on `ResearchRunLifecycle` (instance method at :260, `@staticmethod` at :265), shadowing the first; `TransitionErrorKind(str)` class attributes were plain `str`, so mypy saw `str` where `TransitionErrorKind` was declared | Renamed the transition-description method to `describe_transition` (kept `@staticmethod`); converted `TransitionErrorKind` to `enum.StrEnum`, matching `CitationFailureKind` and the other closed sets | `app/domain/research/lifecycle.py` |
| 2 | `BoundAsyncStream(response.stream, ...)` — mypy could not narrow `SyncByteStream \| AsyncByteStream` from the transport | `assert isinstance(response.stream, httpx.AsyncByteStream)` before the wrap (the transport is async-only), no unchecked cast | `app/infrastructure/fetch/client.py` |
| 3 | `research_uow_factory: callable \| None` — `builtins.callable` is not a type | `Callable[[], Any] \| None`, matching the service constructors' annotation | `app/bot/container.py` |
| 4 | `FetchHTTPStatusError.__init__` required `status_code`, so the structural test that constructs every `ResearchError` subclass from one positional message (`test_every_domain_failure_has_its_own_message`) raised instead | `status_code: int \| None = None`; `retryable` is `False` when no status is known. `for_status` remains the typed factory that supplies it | `app/domain/errors.py` |
| 5 | `idna` and `trafilatura` imported by `app/` but undeclared (T-13) | Declared `idna==3.18` and `trafilatura==2.3.1` with justifications; both were already installed, no package was added or upgraded | `requirements.txt` |
| 6 | `RUF059` — unused unpacked `source` in a P3.9-A ranking test | Renamed to `_source` | `tests/test_research_ranking_persistence.py` |
| 7 | Stale synthesis security test: asserted a refused synthesis leaves the run `queued`, but the P3.9-A `seed_verified_run` fixture sets the run to `running`, which is the state synthesis transitions from | Assertion updated to `running`; the intent (a refused synthesis must not look successful) is unchanged | `tests/test_research_synthesis_security.py` |
| 8 | Concurrency lifecycle test drove `run_on_process_loop` from inside a `pytest-asyncio` async test (`RuntimeError: Cannot run the event loop while another loop is running`), and it could not have passed anyway: it passed a Telegram id where an internal user id is required | Rewritten as a **synchronous** test that drives its whole scenario on the process's long-lived loop; the two callers contend deterministically (first holds its uncommitted transaction). `set_status` now reads the run under `SELECT ... FOR UPDATE`, which is what makes "exactly one wins" true | `tests/test_research_lifecycle.py`, `app/repositories/research.py` |

### 22.3 The `set_status` row lock (B4 contract, now enforced)

`ResearchRunRepositoryImpl.set_status` documents an atomic, ownership-scoped
read-then-write. Before this pass the read used `session.get` with no lock, so under READ
COMMITTED two concurrent transitions both read the old status and both wrote — the
concurrency test failed at `assert accepted is False` when the lock was removed, and
passes with it (verified both ways). The read is now `select(...).with_for_update()`.
Ownership and the lifecycle policy are unchanged; this makes the write actually serialise,
as the method already claimed. No schema change (`alembic check`: no new upgrade
operations) and no new migration. The `partial` writer remains absent (P3.9-B omission,
unchanged).

### 22.4 Verification (actual output, 2026-10-09)

```text
pytest -q                    1981 passed  (1981 collected)
ruff check app tests         All checks passed
mypy app                     Success: no issues found in 85 source files
alembic check                No new upgrade operations detected
make verify                  passes (lint + typecheck + test)
```

**The repository is green.** This supersedes the "not green" statements above; the
earlier figures remain true for their dates. The full record is the Phase 3 audit § T.

### 22.5 Scope

No `/research` handler, orchestrator, Celery research task, B7 idempotency, deadline or
partial-writer or terminal-message flow, dedicated research rate limiter, or B6-P4 fix
was added. No frozen P3.9-C decision was changed. No dependency was added or upgraded
(the two declared packages were already installed). No historical migration was edited
and no migration was created. Security invariants (SSRF validation/pinning/redirect
revalidation/downgrade rejection, response-size/decompression/deadline limits, no
credential forwarding, typed errors) and the lifecycle contract are preserved.

---

## 23. P3.9-D status — research orchestration (2026-10-10)

**Complete.** `/research <question>` now runs end to end: the handler accepts and
acknowledges once, one Celery task per run executes the whole pipeline on the
worker's long-lived loop, and the user receives exactly one terminal message. The
full record — decisions, prerequisite closure, and verification output — is the
Phase 3 audit **§ U**; this section states the shape and what a reader of the
phase document needs.

### 23.1 The path a question takes

1. **`/research <question>`** (`app/bot/handlers.py`): parsed, rejected with usage
   text when empty, rejected above `PLAN_MAX_QUESTION_CHARS` (AD-046, § 21.4) with
   **no run created**, throttled through the dedicated research allowance
   (`RateLimiter.check_share`, AD-047), then accepted by
   `ResearchIntakeService.accept`.
2. **Intake** (`app/services/research/intake.py`): derives the request key
   (sha256 of the Telegram user id and message id), creates the run with
   `ON CONFLICT (user_id, request_key) DO NOTHING`, and reports whether this
   delivery created it (B7). A redelivered command converges on the existing run —
   no second run, no second acknowledgement, no second enqueue.
3. **One acknowledgement**, then `run_research.delay(run_id, user_id, chat_id)`
   (`app/tasks/research_pipeline_task.py`). If the enqueue itself fails, the run
   is marked `failed` ("not started: queuing failed") so nothing stays apparently
   active.
4. **The task** re-executes nothing by itself: it resolves the container, calls
   `ResearchOrchestrator.execute`, and delivers **one** terminal message — the
   verified answer, an explicit no-evidence notice, a stopped-early notice on a
   cooperative deadline or soft limit, or the mapped failure text. Retryable
   provider failures are retried on the Celery allowance without messaging the
   user; `SoftTimeLimitExceeded` and `ResearchDeadlineError` are not retried
   (AD-045, B5-C1).
5. **The orchestrator** (`app/services/research/orchestrator.py`) owns sequence
   only: claim the run to `running` (ownership-scoped), plan, search, fetch /
   extract / chunk at most `RESEARCH_MAX_SOURCES` candidates, deduplicate and
   rank (persisting P3.5's metadata), collect evidence per selected source
   (refusing re-analysis, B7), then synthesize. Every stage commits in its own
   short unit of work; providers are called outside transactions. Degradation is
   recorded in tallies and becomes `partial` vs `completed` (AD-044), with a
   counts-only `failure_summary` (`summarize_run`).

### 23.2 Decisions worth knowing (audit § U.2 has the full list)

- **The deadline's numeric value stays BLOCKED** (§ 21.3): the mechanism exists
  and `RESEARCH_DEADLINE_SECONDS` defaults to `None` = unbounded, deliberately
  not an invented number. Latching: once expired, always expired.
- **`partial` and `running` are now written** — B4 is closed; `partial` is
  written by synthesis via `terminal_status`, and the terminal summary via the
  status-free `set_summary_note` (the lifecycle's same-state refusal protects
  terminal runs and was not relaxed).
- **B7's guard is durable**: the unique key at intake, plus a per-source
  `SELECT … FOR UPDATE` claim at evidence (`summary IS NULL`), so a hard-kill
  redelivery cannot double-spend a source or double-notify a user.
- **B6-P4 (the global in-flight counter's cross-process leak) is reported, not
  fixed**; research does not take the global slot and so does not inherit it.
- **Two Phase 2 tests changed their unknown-command example** from `/research`
  to `/doesnotexist`; the asserted contract is unchanged.

### 23.3 Verification (2026-10-10)

```text
pytest -q                    2063 passed, 1 failed (2064 collected)
                             — the failure is the SSRF resolver test and is
                             environmental (this machine's DNS wildcard-resolves
                             every name); not a P3.9-D regression, audit § U.5
ruff check app tests         All checks passed
mypy app                     Success: no issues found in 90 source files
alembic check                No new upgrade operations detected
```

### 23.4 Not done at P3.9-D

- the deadline and rate-limit **numbers** (still BLOCKED, § U.4);
- token-ledger population (`prompt_tokens`/`completion_tokens` remain unwritten) —
  **closed at P3.10, § 24.2**;
- B6-P4's durable in-flight store; a `cancelled` status; a separate research
  worker queue or concurrency budget; any change to the frozen P3.9-C decisions.

---

## 24. P3.10 — Phase 3 closure (2026-10-13)

Three blockers closed, the documentation reconciled, and the baseline re-measured.
This section states what changed and what a reader of this document needs; the full
record is the canonical audit **§ V**.

### 24.1 Citation rendering (FR-40, FR-37)

P3.8 deliberately stored a *structured* answer and refused to render markers into it,
deferring presentation to "a later stage". That stage did not exist, and the
consequence was that the product's defining promise — an answer with traceable
citations — was not delivered. `app/services/research/rendering.py` is it.

**Marker format.** A section renders as its heading, its prose, and then its markers
on the final line:

```text
Growth in the sampled districts
The 2024 survey recorded a rise across the sampled districts. [1] [2]

Sources:
[1] Field survey 2024 — example.com (chunk 3)
https://example.com/survey
[2] Statistical annex — stats.example.org (Introduction)
https://stats.example.org/table
```

**Four rules, each structural rather than conventional:**

1. **Numbering is per source, not per citation.** Two citations from different passages
   of one document are one reference. A list printing the same URL twice would read as
   two independent sources, which is the thing citation integrity is supposed to
   prevent.
2. **Markers are section-level**, because that is the granularity at which support was
   verified. `VerifiedSection` binds citations to a section and FR-36 is enforced there;
   placing a marker mid-paragraph would assert a sentence-level correspondence that no
   check established (§ 19.3).
3. **The renderer verifies nothing and invents nothing.** It renders only what
   `verify_synthesis` already accepted, and every field it prints — URL, domain, title,
   locator — was copied from persisted rows. A citation that failed to resolve never
   became part of an answer, so there is nothing here to strip.
4. **A run with no verified citation renders no markers and no list.** The renderer
   does not imply a source it cannot name.

**Telegram length and chunking (FR-40).** The answer and its reference list are packed
as **one document** to `formatting.telegram_chunk_limit(settings.TELEGRAM_MAX_MESSAGE_LENGTH)`
— 4080 on a 4096-capable deployment — so a short answer and its sources arrive in one
message and a long one simply continues. Packing is **line-boundary based**, which is
the property that matters: a marker, a source label, and its URL are never torn apart
by the split, no entry is duplicated by it, and nothing is silently truncated. The
limit lives in one function used by both the renderer and `DeliveryService`, so the two
cannot drift and re-chunk a message whose markers were deliberately placed.

**Escaping.** Every dynamic value is HTML-escaped. The bot's default parse mode is HTML,
and a retrieved page title is untrusted content (SR-3) — a title containing `<` must not
become markup, and a URL containing `&` must not break message parsing.

**Degradation.** If rendering raises, the orchestrator logs a warning and delivers the
verified answer as plain prose. That asymmetry is deliberate: a refusal from synthesis
means *the citations did not resolve* and withholding the answer is correct, whereas a
rendering failure means *the reference list could not be formatted*, and discarding a
verified result over a presentation defect would trade a correct answer for none.

### 24.2 Per-run token accounting (FR-9, SR-5, T-25)

`app/services/research/usage.py` adds a `RunTokenLedger` and `UsageRecordingLLM`, a
**pass-through** that records only the usage the provider reported. The composition root
hands the four research adapters the shared provider behind it, which is why no adapter
changed; `ChatService` keeps the raw client and its own per-turn accounting, so the two
ledgers cannot be mixed.

The orchestrator opens one ledger scope per run. Totals reach the run through the
lifecycle's **existing** `set_status` parameters — in the same transaction as the answer
on success, and on the terminal failure write when there is no answer (a failed run's
cost is exactly what an operator most needs to see).

**Guarantees:**

- **Only reported usage is recorded.** A value that is absent or not a non-negative
  integer contributes zero and increments `calls_without_usage`. Nothing is estimated.
- **No double counting.** `set_status` *sets* the ledger columns rather than
  incrementing them, and the lifecycle separately refuses a terminal run being rewritten,
  so a Celery retry cannot re-report what a prior attempt already wrote.
- **A failed call records nothing.** No usage was reported for it. Retries *inside* the
  provider client are opaque to this layer and are counted once per successful call —
  stated rather than papered over.

**Known limitations, recorded rather than worked around:**

- The Phase 2 client normalises an absent provider `usage` block to `0` at parse time,
  so "the provider reported nothing" and "the call used no tokens" are indistinguishable
  at this layer. Counting upstream absence would mean changing the Phase 2 client's
  contract, which is outside this change.
- A run interrupted by a **soft limit** logs its totals but does not persist them: the
  terminal row belongs to the task layer there, and competing for that row's lifecycle
  lock from the exception path would be worse than a missing number on that one path.

**Still deferred, deliberately:** cost-budget enforcement, per-user spend ceilings, and
rate-limit numbers. No pricing is modelled and no limit is invented (AD-047).

### 24.3 What one research run costs *by construction*

Provider calls per run, from the orchestrator's own structure — a count, not a
measurement:

| Stage | Calls | Bound by |
|---|---|---|
| Planning | 1 | one per run, always |
| Per-source summarization + claim extraction | 2 × selected sources | `RESEARCH_MAX_SOURCES` (default 5, ceiling 20) |
| Synthesis | 1 | one per run, only when citable evidence exists |

So a default run is **at most 12 provider calls**, and fewer whenever the bound is not
reached, a source fails before evidence, or the deadline stops the run. The *tokens*
behind that count were previously unrecorded and now persist on the run — but **no
figure is asserted here**, because no run has been executed against live providers. A
measured figure is an operational input the project does not have yet.

### 24.4 Search provider (AD-050)

`SEARCH_PROVIDER` defaults to **`tavily`**; the Brave adapter stays registered. The
rationale is **budget**: a credential now exists, which discharged audit § K.10's
outstanding item, and on that re-evaluation Tavily met the required search functionality
within this project's free-tier allowance where Brave was not a viable free option.
No comparative quality or latency benchmark was run, so those axes stay **NOT VERIFIED**,
and no claim is made that Tavily is better on any axis or free indefinitely. What is
unchanged is the decision itself — search behind `SearchProvider`, selected by
configuration, so re-selecting is a settings value.

### 24.5 Verification (2026-10-13, measured, not copied)

```text
pytest -q                     2221 passed in 17.85s   (2221 collected)
ruff check app tests          All checks passed!
mypy app                      Success: no issues found in 96 source files
alembic check                 No new upgrade operations detected
alembic current               0005_research_request_key (head)
make verify                   lint + typecheck + test, all green
tests/test_research_fetch_ssrf.py    128 passed   (T-4…T-7 and the extended cases)
```

**This supersedes the P3.9-D figures and corrects a stale claim.** § 23.3 recorded
`2063 passed / 1 failed`, attributing the failure to this machine's DNS wildcard-resolving
every name into `198.18.0.0/15`. **That no longer happens**: a direct probe shows
`nonexistent.invalid` now fails resolution with `gaierror`, so
`test_the_system_resolver_raises_a_typed_error_for_an_unknown_name` passes for the
correct reason. No SSRF control, no policy entry, and no test was changed to achieve
this — the fetch boundary was not touched.

### 24.6 Definition of Done — status

| § 13 item | Status |
|---|---|
| 1. Every § 10 criterion checked and evidenced | **Partial.** 30 of 31 ticked, each with its evidence. The one that is not is FR-39's semantic half, and it **stays unticked deliberately** rather than being ticked approximately. Three further boxes are ticked only in their evidenced half and say so in place: the time-or-cost-budget criterion, the rate-and-cost-limits criterion, and documentation. |
| 2. A user question produces a **cited**, multi-source answer | **Met** (§ 24.1). Was the blocking gap. |
| 3. Every claim carries a citation resolving to this run's content | **Met** (P3.7 + P3.8; T-15). |
| 4. SSRF protections pass all mandatory tests | **Met** (P3.3; T-4…T-7 green). |
| 5. Prompt-injection tests prove behaviour cannot be altered | **Met** (P3.8; T-18/T-19). |
| 6. Every stage testable without network | **Met** — socket guard green. |
| 7. Degraded, partial, zero-source outcomes honest and tested | **Met.** |
| 8. Provider selected on documented criteria, replaceable by configuration | **Met** (AD-036 → **AD-050**). |
| 9. Documentation updated, incl. SSRF and prompt-safety | **Substantially met.** 9 of 11 § 9 items complete; the two open ones are named in § 9 with the reason each remains open. |
| 10. No Phase 4–9 functionality | **Met.** |
| 11. Status updated here and in `PROJECT_PLAN.md` | **Met.** |

**Deferred, and recorded rather than closed:** the deadline value, the rate-limit
numbers, cost-budget enforcement, the consolidated unprocessable-page-type list, and
B6-P4 (a pre-existing Phase 2 defect that § 4 forbids absorbing). Each is named where
it is claimed, and none is marked complete.
