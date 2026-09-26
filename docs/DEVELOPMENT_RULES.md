# Development Rules — Telegram AI Research Bot

**Document type:** Mandatory governance for all Agents and developers
**Applies to:** Every change to this repository
**Source of truth for current state:** [`ARCHITECTURE.md`](./ARCHITECTURE.md)

> These rules are not advisory. A change that violates them is incomplete, regardless of whether the code works.

> **Note on the "Current state" blocks below (added 2026-09-26).** They were
> written against the pre-Phase-1 audit and several are now out of date: there *is*
> a service layer, there *is* a database, and the suite is no longer empty. They
> are left in place as the baseline the rules were written for. For current state
> see [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) and
> [`phases/PHASE-02-AI-CHAT.md`](./phases/PHASE-02-AI-CHAT.md) § 14. The rules
> themselves are unchanged.

---

## 1. Phase Discipline

**The active Phase is the Agent's scope boundary.**

1. **Always identify the active Phase before writing code.** The active Phase is stated at the top of [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) and in the "Status" section of the Phase document.
2. **Only implement work belonging to the active Phase.**
3. **Do not silently implement future-phase features.** A useful idea is still out of scope.
4. **If a future-phase dependency is discovered, document it.** Record it in the Phase document under Risks, or open a note. Do not build it.
5. **Do not move to the next Phase until the current Phase's acceptance criteria are satisfied** and the status has been updated in both the Phase document and [`PROJECT_PLAN.md`](./PROJECT_PLAN.md).

### Scope boundary example — while Phase 1 is active

```text
ALLOWED
- reliability
- security
- infrastructure
- configuration
- tests
- architecture foundations
- logging, health checks, error handling
- dependency correctness

NOT ALLOWED
- web search
- PDF analysis
- research workspace
- multi-agent orchestration
- SaaS dashboard
- monetization
- any feature belonging to Phases 2–9
```

The single most common failure mode in this project is **scope leakage**: a Phase 1 infrastructure fix drags in a research capability because the fix seemed to need it. It does not. If a fix appears to require a research capability, that is a design signal to raise, not a reason to expand scope.

---

## 2. Repository Inspection

Before modifying any code:

1. **Read the relevant documentation.** [`PROJECT_PLAN.md`](./PROJECT_PLAN.md), this file, and the active Phase document.
2. **Read [`ARCHITECTURE.md`](./ARCHITECTURE.md)** if the change touches anything the audit already examined. It records verified line numbers and known defects; re-deriving them wastes effort and risks contradicting them.
3. **Read the active Phase file.** Confirm the work item is listed there.
4. **Inspect the existing implementation.** Do not assume. The audit found code whose names misrepresent its behavior — a function named `process_research` that performs no research, and a method named `generate_final_answer` that is never called.
5. **Identify dependencies** of the files you are about to change.
6. **Plan the change.** Know which files change and why, before editing.
7. **Implement** the smallest change that satisfies a stated requirement.
8. **Test** the change, including the failure paths.
9. **Update documentation** in the same change. See § Documentation.

### Documentation you must not skip

```text
docs/PROJECT_PLAN.md         — status tables, phase table, decision log
docs/DEVELOPMENT_RULES.md    — this file
docs/ARCHITECTURE.md         — current architecture & audit
docs/phases/PHASE-0N-*.md    — the active phase
```

---

## 3. Security

Non-negotiable.

- **Never hardcode secrets.** No API keys, tokens, passwords, or connection strings in any source file, including comments, docstrings, and commented-out code.
- **Never print secrets.** Not in logs, not in error messages, not in exception text, not in test fixtures, not in documentation, not in a commit message, not in terminal output shared with the user.
- **Never commit `.env`.** It must be listed in `.gitignore` before the repository is initialized.
- **Never expose API keys in logs.** A key present in a log is a disclosed key.
- **Treat all user-controlled content as untrusted.** Message text, usernames, callback data, and file content are attacker-controlled input.
- **Consider prompt injection whenever external content is introduced.** The moment web pages, documents, or third-party text enter an LLM prompt, assume they may contain instructions aimed at the model. Isolate untrusted content, do not concatenate it into system instructions, and never let retrieved content grant capabilities.
- **Do not weaken the security posture to make a test pass.**
- **Report, do not fix, out-of-scope security problems.** If you find a vulnerability outside the active Phase, document it with file, line, and severity. Do not patch it silently.

### Current state

**SECRET FOUND IN FILE: `app/agents/supervisor.py`** — a plaintext API key literal at line 8, and it is the wrong key format for the endpoint it is sent to.

**SECRET FOUND IN FILE: `.env`** — contains a bot token and a provider API key.

There is **no `.gitignore`**. The directory is not currently a git repository, so nothing is committed today, but a single `git init && git add .` would disclose both credentials and the entire `venv/`.

Both credentials require manual rotation. That is a human action and is listed as a Phase 1 requirement.

---

## 4. Architecture

- **Keep Telegram handlers thin.** A handler should parse the update, delegate, and return. No business logic, no LLM calls, no string formatting of model output, no event-loop management.
- **Keep Celery tasks thin.** A task should orchestrate a service call and report the outcome. No prompt construction, no HTTP client lifecycle, no transport management.
- **Put business logic in services.** A service layer does not currently exist. Creating it is Phase 1 work.
- **Keep external providers behind abstractions.** Any LLM provider, search provider, or storage backend must sit behind an interface so it can be replaced without touching the domain.
- **Avoid unnecessary global state.** Module-level singletons built from settings at import time are the current pattern and are the direct cause of several audit findings. They also make the system untestable.
- **Use dependency injection where practical.** Construct clients in one place and pass them, rather than importing globals at the point of use.
- **Preserve clear boundaries** between bot, application, domain, infrastructure, and integrations.
- **Do not add a layer "just in case."** Every new module must have a caller in the same change.
- **Prefer removing code to adding code.** Dead code such as `Supervisor.generate_final_answer` should be wired in or removed, not left to rot.

### Current architectural facts

- There is **no service layer**. `app/tasks/research_task.py` composes the agent, formats the response, manages an asyncio event loop, and calls Telegram — all in 18 lines.
- The code graph detects exactly two disconnected clusters: the Telegram/FastAPI process and the Celery/agent process. They communicate only through a Redis queue. This is the structural root cause of most audit findings.
- `app/bot/handlers.py` imports from `app/tasks/`, and `app/tasks/research_task.py` imports `bot` from `app/bot/dispatcher.py`. The layering is circular by design accident.

---

## 5. Testing

- **Critical behavior must have regression tests.** A test is required for any change to the request path, the LLM call, message formatting, or the database layer.
- **Test failure paths, not only success paths.** The most damaging defects in this repository are silent failures. A test that only asserts the happy path does not cover them.
- **The audit's required regression test for Phase 1:**

  > Verify that `process_research` can execute successfully more than once within the same worker process.

  This test exists because the current implementation creates and closes a new asyncio event loop per task while reusing a module-level `aiogram.Bot` whose HTTP session is bound to the first loop. The consequence is that the core feature can succeed approximately once per worker process. No test currently detects this.
- **No external calls in unit tests.** LLM and Telegram clients must be substitutable. If a test requires network access, the design is wrong.
- **A phase is not complete without its tests.** See each Phase document § Testing Requirements.
- **Do not add tests to code you have not changed** unless the Phase document requires it. Adding a test suite is Phase 1 work with a defined scope.

### Current state

Zero tests. No test files, no test directory, no test framework in `requirements.txt`, no CI configuration.

---

## 6. Documentation

After implementing anything:

- **Update the active Phase document's Status section** and tick acceptance criteria only when they are actually satisfied.
- **Update [`PROJECT_PLAN.md`](./PROJECT_PLAN.md)** — the phase status table, the maturity table, and the Architecture Decision Log if a decision was made.
- **Update [`ARCHITECTURE.md`](./ARCHITECTURE.md) when the architecture changes.** Note that this document is an audit snapshot. When the code changes, either update it or record explicitly that it is now a point-in-time snapshot and add a new architecture document. Do not silently let it become false.
- **Record important decisions** in the Architecture Decision Log in [`PROJECT_PLAN.md`](./PROJECT_PLAN.md). A decision is recorded when it is made, not when it is implemented. Never delete a log row; supersede it.
- **Document what you did not do.** If a requirement was skipped, say so and why.

### Documentation honesty

Never write any of the following unless repository evidence supports it at the moment of writing:

```text
"implemented"      "production ready"     "working"
"supported"        "completed"            "fully functional"
```

Use instead:

```text
Implemented        Partial                Skeleton
Planned            Not Implemented        Blocked
Needs Verification  In Progress
```

If evidence is insufficient, write exactly:

```text
NOT VERIFIED — insufficient evidence in repository
```

---

## 7. Agent Behavior

The Agent must **not**:

- **Invent requirements.** Work from the Phase document, not from imagination about what the product "should" have.
- **Assume future features are already implemented.** The repository contains code whose names imply capabilities that do not exist. Verify by reading.
- **Rewrite unrelated code.** Scope creep disguised as cleanup is still scope creep.
- **Expand scope without justification.** If expansion is genuinely necessary, stop and document the reason instead of proceeding.
- **Mark a Phase complete without validation.** Acceptance criteria require evidence, not optimism.
- **Modify files outside the active Phase's stated scope**, including configuration, dependencies, and infrastructure.
- **Delete or rename existing application files** without an explicit instruction.
- **Commit changes** unless explicitly asked.
- **Install or upgrade packages** unless explicitly instructed, and never as a side effect of a fix.
- **Run database migrations** against any environment not explicitly designated for that purpose.
- **Fabricate verification.** If a test was not run, do not describe it as passing. Say it was not run.
- **Suppress a failing test to make a suite green.** Fix the cause or report the blocker.

The Agent **must**:

- Report blockers plainly rather than working around them invisibly.
- State clearly when something could not be verified from the repository.
- Prefer the smallest change that fully satisfies a stated requirement.
- Leave the repository in a state where the next Agent can act on accurate documentation.

---

## 8. Provider and Vendor Decisions

- **Do not lock in a vendor without a recorded decision.** If no decision exists in the Architecture Decision Log, the choice is open.
- **External services belong behind abstractions.** Search providers, embedding providers, vector stores, payment processors, and hosting platforms are all replaceable.
- **Do not invent provider selections in documentation.** Write intent, not a chosen vendor, unless the repository already specifies one.

Currently open, deliberately undecided: web search provider (Phase 3), embedding provider and vector store (Phase 4), hosting and cloud provider (Phase 8), payment provider (Phase 9). See Architecture Decision Log entries AD-006 through AD-009.

---

## 9. Status Vocabulary

Use these terms consistently across all documentation.

| Term | Meaning |
|---|---|
| `Implemented` | Exists in code, verified by reading the source, and works as described |
| `Partial` | Exists but is incomplete, unreliable, or missing required behavior |
| `Skeleton` | Structure or naming exists with no working behavior behind it |
| `Planned` | Documented intent, no implementation |
| `Not Implemented` | No code exists |
| `Blocked` | Cannot proceed until a named prerequisite is satisfied |
| `In Progress` | Actively being worked on, not yet meeting acceptance criteria |
| `Needs Verification` | Documentation claims something the evidence does not confirm |
| `NOT VERIFIED` | Insufficient evidence in the repository to make any claim |

---

## Related Documents

| Document | Purpose |
|---|---|
| [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) | Master roadmap, phase status, decision log |
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | Current Architecture & Audit Reference |
| [`../.agents/skills/research-bot/SKILL.md`](../.agents/skills/research-bot/SKILL.md) | Operational skill briefing for coding Agents |
