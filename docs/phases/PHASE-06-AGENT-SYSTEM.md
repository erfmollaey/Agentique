# Phase 6 — Agent System

**Status:** Not Started (Skeleton Only)
**Blocked by:** Phase 3 — Research Engine, Phase 4 — File Intelligence
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md)

---

## 1. Objective

Replace the single orchestrated research pipeline with a modular system of cooperating agents, each with a defined role, defined tools, and defined boundaries.

The motivation is that different research subtasks — planning, searching, reading, evaluating, verifying, summarizing, reporting — have different inputs, different failure modes, and different quality criteria. A single pipeline forces one prompt to do all of them.

---

## 2. Current State

### Current — stated plainly

```text
A directory named app/agents/ exists.

It contains one file with one class:

    class Supervisor          (app/agents/supervisor.py:12)
        __init__              sets self.model = "llama-3.3-70b-versatile"
        analyze_query         1 LLM call → {summary, sub_questions}   WORKS
        generate_final_answer combines sub-answers → text            DEAD CODE

    Zero callers of generate_final_answer.
    Zero orchestration.
    Zero tools.
    Zero agents.
    Zero state.
```

That is the entire current state. `Supervisor` is a minimal LLM interaction, not an agent system. The name anticipates an architecture that has not been built.

### What does not exist

- No agent base class, interface, or registry.
- No tool abstraction or tool registry.
- No planning, dispatch, or execution loop.
- No inter-agent communication.
- No agent state, memory, or context management.
- No agent-level error handling or retry.
- No observability into what an agent did or why.
- No evaluation of agent output quality.
- No mechanism preventing an agent from exceeding its role.

### The role list in this document is intent, not design

The agent roles named in § 5 are a reasonable decomposition of the Phase 3 pipeline. They are **not** a commitment. The correct decomposition should be derived from where the single-pipeline approach actually fails in practice, measured during Phase 3. If evaluation quality does not improve from splitting a stage, that stage should not be split.

---

## 3. Scope

- An agent interface with a defined contract.
- A tool abstraction and registry that agents invoke through.
- Role-specific agents for the research subtasks that warrant specialization.
- A planning and dispatch layer that assigns subtasks to agents.
- Inter-agent result passing with explicit contracts.
- Agent-level state, context, and budget management.
- Error handling, retry, and fallback at the agent level.
- Observability: what each agent did, what it consumed, what it produced, what it cost.
- An evaluation approach for comparing agent output against the single-pipeline baseline.

---

## 4. Out of Scope

- **New research capability.** Agents orchestrate Phases 3 and 4; they do not add new sources of information.
- **New document capability.** Phase 4.
- **New workspace or organizational capability.** Phase 5.
- **Long-running Deep Research jobs, report export, citation styles, charts, multilingual research, voice.** Phase 7.
- **Web dashboard, workspace UI, API surface, team collaboration, SaaS, billing.** Phases 8 and 9.
- **An agent framework dependency chosen in advance.** See § 6.
- **Unbounded autonomy.** No agent may act outside its defined role or tool set.
- **Fixing Phase 1–5 defects.** Report them; do not absorb them.

**Scope note:** building an elaborate agent framework before measuring whether the single pipeline fails is premature. The evaluation baseline is a prerequisite, not an afterthought.

---

## 5. Functional Requirements

### Agent contract
- FR-1 Every agent implements a common interface with a declared role, an input contract, an output contract, and a declared tool set.
- FR-2 An agent's output is a validated domain type, not free text.
- FR-3 An agent declares which tools it may use. It cannot reach a tool outside that set.
- FR-4 An agent declares its own token and time budget.
- FR-5 An agent's execution is traced: inputs, tool calls, outputs, token usage, and errors.

### Tool abstraction
- FR-6 Tools are exposed through a registry with a declared interface.
- FR-7 A tool invocation receives structured input and returns structured output or a typed error.
- FR-8 Tools are the only way an agent affects the outside world. An agent has no direct network, filesystem, or database access.
- FR-9 Tool calls are logged with arguments and results, subject to secret and content-redaction rules.
- FR-10 Adding a tool does not require modifying agent implementations.

### Planning and dispatch
- FR-11 A planner produces a task breakdown from the user's question.
- FR-12 The plan is validated before execution.
- FR-13 The dispatcher assigns subtasks to the appropriate agent.
- FR-14 Subtasks may execute concurrently where independent, subject to the concurrency cap.
- FR-15 The plan and its execution are persisted, so a run is inspectable after the fact.

### Agent roles

Candidate roles, to be confirmed against Phase 3 evidence:

| Role | Responsibility | Tools |
|---|---|---|
| Planner | Decompose the question into a research plan | LLM |
| Search | Execute search queries from the plan | search provider |
| Reader / Extraction | Fetch and extract a source's content | restricted fetch client, extractor |
| Source Evaluation | Judge relevance, quality, and credibility | LLM |
| Fact Checking | Test claims against source passages | LLM, source store |
| Summarization | Produce per-source and cross-source summaries | LLM |
| Report Generator | Assemble the final structured answer | LLM, citation store |

- FR-16 Each role's input and output contracts are defined and validated.
- FR-17 An agent cannot exceed its declared tool set.
- FR-18 An agent cannot exceed its declared budget.

### Inter-agent communication
- FR-19 Results are passed between agents as typed structures, not as concatenated prose.
- FR-20 Each agent receives only the context it needs, not the full accumulated transcript.
- FR-21 Provenance is preserved: a downstream agent can identify which upstream agent and source produced each input.

### Failure handling
- FR-22 An agent failure is contained. One agent failing does not abort the run unless the run cannot proceed without it.
- FR-23 Failures are retried according to a bounded policy, consistent with the Phase 1 Celery configuration.
- FR-24 A run that cannot complete returns partial results with an honest description of what is missing.
- FR-25 An agent that produces invalid output is retried with feedback, then failed — never silently accepted.
- FR-26 A retry loop is bounded. An agent cannot loop indefinitely.

### Evaluation
- FR-27 The multi-agent result is compared against the Phase 3 single-pipeline baseline on a representative question set.
- FR-28 Evaluation measures answer groundedness, citation correctness, coverage, latency, and cost.
- FR-29 If specialization does not measurably improve quality, the affected role is not split.

---

## 6. Technical Requirements

### Framework choice — undecided

**This document does not select an agent framework.** The choice should be made during implementation based on: the shape of the Phase 3 pipeline, whether orchestration is sequential or graph-shaped, observability requirements, the team's operational familiarity, and dependency weight.

If a framework is adopted, the agent interface in FR-1 must be expressible without it, so the framework is an implementation detail rather than an architectural commitment. This is the same reversibility principle applied to providers in Phases 3 and 4.

### Orchestration model

- Orchestration is explicit and inspectable. A run is a sequence or graph of declared steps, not an emergent loop.
- Each step has a defined input, output, budget, and failure policy.
- The run is resumable from a persisted plan.

### Context management

- Each agent receives a purpose-built context: the subtask, the relevant retrieved content, and the constraints — not the full conversation transcript.
- Context is assembled by a dedicated component, not by concatenating strings at the call site.
- Token budgets are per-agent and enforced before the call.
- The Phase 3 untrusted-content isolation applies to every agent that receives retrieved content.

### State and observability

- Agent runs, tool calls, and decisions are persisted, enabling post-hoc explanation of a result.
- Tracing exists per agent: what it was asked, what it did, what it produced, what it cost.
- The Phase 5 provenance model is extended to record which agent produced which claim.

### Cost

- Multi-agent execution multiplies LLM calls. Budget enforcement is not optional: per-run, per-agent, and per-user caps.
- Cost is attributed per agent so the value of specialization is measurable, not assumed.

---

## 7. Security Requirements

An agent system converts a bounded pipeline into a system that makes decisions. That expands the security surface.

- **SR-1 Agents have no ambient authority.** Every capability an agent has is a tool explicitly granted to it. An agent has no direct network, filesystem, database, or shell access.
- **SR-2 Tool access is allowlisted per agent.** The fetch tool remains behind the Phase 3 SSRF controls for every agent that uses it. Agent autonomy must not become a way to bypass the fetch restrictions.
- **SR-3 Retrieved and persisted content remains untrusted data.** An agent reading a web page is reading attacker-influenced content. Phase 3's isolation rules apply to every agent, without exception.
- **SR-4 No instruction escalation between agents.** Content produced by one agent is data to the next. A Fact Checking agent's output must not be able to instruct the Report Generator to omit a citation.
- **SR-5 Injection from tool output.** Tool results are untrusted input. A tool returning web content, a document, or another agent's output enters prompts as delimited data, never as instruction.
- **SR-6 Bounded autonomy.** Agents cannot loop indefinitely, exceed budgets, expand their own tool set, or modify the plan in ways that grant new capability.
- **SR-7 User authority is never transferable.** An agent acts only within the requesting user's authority. No agent may access another user's data regardless of what the plan says.
- **SR-8 Tool arguments are validated.** A model-generated argument is untrusted input. A URL tool argument passes through the SSRF-restricted client; a file argument is checked against ownership.
- **SR-9 Secrets are never exposed to agents.** Provider credentials are not available to agent prompts or tool results.
- **SR-10 Audit trail.** Every tool call and agent decision is attributable. An unexplained change in behavior must be traceable to a specific agent decision and its inputs.
- **SR-11 No agent may weaken a security control.** An agent cannot disable the fetch restrictions, the rate limits, or the budget caps.

---

## 8. Testing Requirements

| ID | Test | Verifies |
|---|---|---|
| T-1 | Every agent implements the common contract and validates its output | FR-1, FR-2 |
| T-2 | An agent cannot invoke a tool outside its declared set | FR-3, SR-1 — mandatory |
| T-3 | An agent cannot exceed its declared token or time budget | FR-4, FR-18, SR-6 — mandatory |
| T-4 | The planner produces a validated plan from a real question | FR-11, FR-12 |
| T-5 | The dispatcher assigns subtasks to the correct agents | FR-13 |
| T-6 | Independent subtasks execute concurrently within the cap | FR-14 |
| T-7 | The plan and its execution are persisted and inspectable afterwards | FR-15 |
| T-8 | Each agent receives purpose-built context, not the full transcript | FR-20 |
| T-9 | Provenance is preserved from source through agent to claim | FR-21 |
| T-10 | One agent failing does not abort the run | FR-22 |
| T-11 | A run that cannot complete returns partial results described honestly | FR-24 |
| T-12 | An agent producing invalid output is retried then failed, never silently accepted | FR-25 |
| T-13 | A retry loop is bounded | FR-26, SR-6 — mandatory |
| T-14 | **A tool returning injection content does not alter downstream agent behavior** | SR-5 — mandatory |
| T-15 | **An agent cannot bypass the SSRF controls through the fetch tool** | SR-2 — mandatory |
| T-16 | **An agent cannot access another user's data regardless of plan content** | SR-7 — mandatory |
| T-17 | Tool arguments are validated, including model-generated URLs and file references | SR-8 |
| T-18 | Provider credentials are not present in any agent context | SR-9 |
| T-19 | Every tool call and agent decision is attributable in the audit trail | SR-10 |
| T-20 | An agent cannot disable rate limits, fetch restrictions, or budgets | SR-11 |
| T-21 | Multi-agent output is compared against the single-pipeline baseline | FR-27, FR-28 |
| T-22 | Adding a tool does not require modifying agent implementations | FR-10 |

**Mandatory tests:** T-2, T-3, T-13, T-14, T-15, T-16. Authority boundaries, budget enforcement, loop bounding, injection resistance, and user isolation are the defining risks of this phase.

---

## 9. Documentation Requirements

- [ ] The agent contract documented, including input, output, tool, and budget declarations.
- [ ] The tool interface and registry documented, including how to add a tool.
- [ ] The orchestration model documented — sequential or graph — with a diagram.
- [ ] Every agent's role, contract, and tool set documented.
- [ ] The evaluation methodology and results documented, including the comparison against the Phase 3 baseline.
- [ ] **The decision on whether each role measurably improved quality documented, including roles that were rejected.**
- [ ] The context-management strategy documented.
- [ ] Agent observability and the audit trail documented.
- [ ] Any framework selection recorded in the Architecture Decision Log with its justification.
- [ ] `docs/PROJECT_PLAN.md` maturity table, phase status, and decision log updated.
- [ ] `docs/ARCHITECTURE.md` updated.

---

## 10. Acceptance Criteria

### Agent system
- [ ] Every agent implements a common contract with a validated output type.
- [ ] Every agent declares its tool set and its budget.
- [ ] Tools are invoked only through a registry, and adding a tool requires no agent changes.
- [ ] Agents have no ambient network, filesystem, or database authority.

### Orchestration
- [ ] A planner produces a validated plan and a dispatcher assigns subtasks.
- [ ] Independent subtasks execute concurrently within the cap.
- [ ] The plan and its execution are persisted and inspectable.
- [ ] Provenance is preserved from source through agent to final claim.

### Reliability
- [ ] One agent failing does not abort the run.
- [ ] An incomplete run returns partial results described honestly.
- [ ] Invalid agent output is retried then failed, never silently accepted.
- [ ] Retry loops and budgets are bounded and enforced.

### Security
- [ ] An agent cannot invoke a tool outside its declared set.
- [ ] An agent cannot exceed its budget.
- [ ] An agent cannot bypass the SSRF controls through any tool.
- [ ] Injection content in tool results does not alter downstream behavior.
- [ ] An agent cannot access another user's data.
- [ ] An agent cannot disable rate limits, fetch restrictions, or budgets.
- [ ] Every tool call and agent decision is attributable.

### Evaluation
- [ ] Multi-agent output is compared against the Phase 3 single-pipeline baseline.
- [ ] Quality, latency, and cost are measured.
- [ ] Roles that did not measurably improve quality were not split.
- [ ] The evaluation result is documented, including negative findings.

### Quality
- [ ] Each agent receives purpose-built context, not the full transcript.
- [ ] Documentation is updated.
- [ ] No Phase 7–9 functionality was implemented.

---

## 11. Dependencies

### Prerequisites

| Phase | Item | Why this phase needs it |
|---|---|---|
| Phase 3 | Working research pipeline | Agents orchestrate it; it is the baseline and the capability being decomposed |
| Phase 3 | Tool-shaped components — restricted fetch client, search abstraction, extractor | Agents invoke these as tools; they must be independent of the pipeline's control flow |
| Phase 3 | Prompt-safety isolation | Reused per agent, without relaxation |
| Phase 3 | Citation and provenance model | Extended to record agent attribution |
| Phase 4 | Document extraction as a tool | A document reader agent needs it |
| Phase 5 | Provenance and project association | Findings written back into projects |
| Phase 1 | Celery reliability and time limits | Multi-agent runs are longer and more expensive |
| Phase 2 | Token budgeting and cost accounting | Agent cost must be attributed |

### Infrastructure

- PostgreSQL, extended with agent run, tool call, and decision records.
- Redis and the Celery worker, with the Phase 1 reliability settings.
- Same LLM and search providers as Phase 3, unchanged.

### Blocks

- **Phase 7** Deep Research mode and long-running jobs depend on an orchestration layer capable of sustained, bounded work.
- **Phase 8** may expose agent run traces in the web UI.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Specialization does not improve quality | Added complexity, cost, and failure surface for no benefit | FR-29 makes the evaluation a gate; roles that do not help are not split |
| Unbounded agent loops | Cost explosion, worker starvation | Bounded retries and budgets, enforced in code; mandatory test T-13 |
| Agents bypassing the SSRF controls through a tool | Reopens the highest-risk Phase 3 vulnerability | Tools are the only capability path and remain wrapped; mandatory test T-15 |
| Injection propagating between agents | One poisoned source corrupts the entire run | Agent outputs are data, not instructions; mandatory test T-14 |
| Framework lock-in | Architectural commitment to a dependency chosen prematurely | Agent interface expressible without the framework; record the choice when made |
| Cost multiplication | Per-run cost far exceeding the single pipeline | Per-run, per-agent, and per-user budgets; cost attributed per agent |
| Debugging a multi-agent failure | High effort to determine what went wrong | Persisted plans, tool-call traces, and per-agent provenance are prerequisites, not enhancements |
| Non-determinism making evaluation unreliable | Inability to tell improvement from noise | Fixed evaluation question set, repeated runs, recorded results |
| An agent exceeding its role | Unpredictable behavior, hard-to-reason-about failures | Allowlisted tools, declared budgets, audited decisions |
| An agent accessing another user's data | Privacy breach via a convoluted path | User authority enforced independently of plan content; mandatory test T-16 |
| Building the framework before measuring | Premature abstraction | Evaluation baseline is an explicit prerequisite |

---

## 13. Definition of Done

Phase 6 is complete when:

1. Every acceptance criterion in § 10 is checked and evidenced.
2. The six mandatory security tests pass.
3. The multi-agent system is compared against the Phase 3 baseline, and the comparison is documented **including roles that were rejected for lack of measured benefit**.
4. Every agent has a documented contract, tool set, and budget.
5. Every tool call and agent decision is attributable in the audit trail.
6. Documentation is updated, including the framework decision if one was made.
7. No Phase 7–9 functionality was implemented.
8. Status is updated in this document and in `docs/PROJECT_PLAN.md`.

---

## 14. Status

**Current status: Not Started (Skeleton Only)**

**Phase status in `docs/PROJECT_PLAN.md`: Not Started / Skeleton only.**

**Blocked by:** Phase 3 — Research Engine, Phase 4 — File Intelligence.

No agent system has been implemented. The repository contains `app/agents/supervisor.py` with one class, one working method that makes a single LLM call, and one dead method with zero callers. There is no agent interface, no tool registry, no orchestration, and no agent-level state.

The `Supervisor` name and the `app/agents/` directory anticipate the architecture described in this document. They do not constitute it.

**Framework selection: Open.**
