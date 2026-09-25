# Phase 1 — Core Infrastructure & Stabilization

**Status:** In Progress — implementation and validation complete; blocked on manual credential rotation and two environment gates
**Active Phase:** Yes
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — historical audit snapshot, preserved unmodified
**Implementation record:** § 14. `docs/ARCHITECTURE.md` describes the repository **as it was before this phase**; its findings are retained as the defect baseline, not as a statement of current state. The current state is recorded in § 14 and in [`../PROJECT_PLAN.md`](../PROJECT_PLAN.md).

> Every requirement below traces to a verified audit finding. Statuses in § 14 reflect
> what was implemented and validated, not what was intended.

---

## 1. Objective

Make the existing application reliable, secure, testable, and runnable.

The repository is a working skeleton with four defects that prevent it from completing a single request reliably. This phase does not add product capability. It converts an undeliverable prototype into a system that can be built on top of.

**Definition of success for this phase:** a user can send a message, receive a reply, and receive a *failure notice* if something breaks — repeatedly, from a clean environment, in a container, with tests proving it.

---

## 2. Current State

### What exists

| Component | File | State |
|---|---|---|
| ASGI application | `app/main.py` | 24 lines. `GET /health` returns a static literal. Polling started via a fire-and-forget task. |
| Bot and dispatcher | `app/bot/dispatcher.py` | 10 lines. Module-level `Bot` with `parse_mode=HTML`. Module-level `Dispatcher`. |
| Telegram handlers | `app/bot/handlers.py` | 22 lines. `/start` and one unfiltered catch-all. |
| Celery app | `app/core/celery_app.py` | 17 lines. Redis broker and backend. One task included. |
| Settings | `app/core/config.py` | 12 lines. Four required variables, no defaults, no validators. |
| Celery task | `app/tasks/research_task.py` | 18 lines. Per-task event loop, unguarded LLM call, unescaped send. |
| LLM interaction | `app/agents/supervisor.py` | 65 lines. Module-level client with a hardcoded key. One working method, one dead method. |
| Infrastructure | `docker-compose.yml` | Redis and PostgreSQL only. No application or worker service. |

### Critical known defects entering this phase

Audit IDs are from [`../ARCHITECTURE.md`](../ARCHITECTURE.md) § 6.

| ID | Defect | Effect |
|---|---|---|
| C-1 | Hardcoded API key literal at `app/agents/supervisor.py:8`, wrong key format for the configured endpoint | Every LLM call fails authentication; credential is disclosed in source |
| C-2 | `openai` imported at `app/agents/supervisor.py:3` but absent from `requirements.txt` | Clean environment cannot start the application |
| C-3 | `app/tasks/research_task.py:15-18` creates and closes a new event loop per task while reusing a module-level `Bot` whose HTTP session is bound to the first loop | The core feature can succeed roughly once per worker process; sockets leak |
| C-4 | `parse_mode=HTML` set globally at `app/bot/dispatcher.py:8`, but output is `**markdown**` and is never escaped or length-checked | Telegram rejects or mis-renders most replies; replies are silently lost |
| H-1 | `message.from_user.id` used as `chat_id` at `app/bot/handlers.py:20` | Bot is broken in every group chat |
| H-2 | Exactly one `try`/`except` in the whole codebase | Every failure is silent; users wait forever |
| H-3 | No LLM timeout, no `max_tokens`, no retry, no Celery task time limits | A hung call occupies a worker indefinitely; cost is unbounded |
| H-4 | `json.loads` and dict access unguarded at `app/agents/supervisor.py:43` and `app/tasks/research_task.py:12` | Malformed model output crashes the task |
| H-5 | `/health` returns a static literal at `app/main.py:11-13` | A dead bot reports healthy |
| H-6 | No graceful shutdown; deprecated `@app.on_event` at `app/main.py:15` | In-flight updates lost on restart |
| H-7 | `@dp.message()` at `app/bot/handlers.py:15` has no content filter | Non-text messages enqueue `query=None` |
| H-8 | `docker-compose.yml` has no application or worker service; no `Dockerfile` | The application cannot be deployed as written |
| M-8 | No rate limiting, no concurrency cap | Unbounded billable work from any user |
| M-9 | Result backend equals broker; `task_ignore_result` unset | Redis keys accumulate without bound |
| M-7 | `logging.basicConfig` called twice; full user text logged; task failures unlogged | No observability where failures occur |
| M-5 | `httpx`, `redis`, `asyncpg`, `sqlalchemy`, `python-dotenv` unused; `os` and `settings` imported and unused | Dependency list misrepresents the system |

### Structural gaps entering this phase

- No service layer. `app/tasks/research_task.py` performs composition, formatting, event-loop management, and transport in 18 lines.
- Module-level singletons (`bot`, `client`) make the system untestable.
- No tests, no test framework, no CI, no `pyproject.toml`, no linter configuration.
- No `.gitignore`, no `.env.example`, no `Dockerfile`, no `README`.
- `DATABASE_URL` is declared and unused. `asyncpg` and `sqlalchemy` are declared and unused. The PostgreSQL container runs and nothing connects to it.

---

## 3. Scope

### P0 — Critical: security and non-functional

| ID | Requirement | Audit ref |
|---|---|---|
| P0-1 | Remove the hardcoded LLM API key from `app/agents/supervisor.py` and read the key from application settings | C-1 |
| P0-2 | Use application settings and environment configuration for all credentials and endpoints; no literals in source | C-1 |
| P0-3 | Rotate the exposed credentials manually. **Human action — the exposed LLM key and the `.env` bot token must both be rotated** | C-1, S-2 |
| P0-4 | Add a `.gitignore` covering `.env`, `venv/`, `__pycache__/`, and local artifacts, **before** the repository is initialized | S-2 |
| P0-5 | Add `.env.example` documenting every required variable with no real values | S-2 |
| P0-6 | Add the missing `openai` dependency to `requirements.txt` with a pinned version | C-2 |
| P0-7 | Fix the aiogram/asyncio event-loop lifecycle so a module-level `Bot` is not reused across closed event loops | C-3 |
| P0-8 | Resolve the Telegram parse-mode mismatch so bot output and configured parse mode agree | C-4 |
| P0-9 | Handle Telegram message length limits by chunking responses that exceed the platform maximum | C-4 |
| P0-10 | Add task-level and LLM error handling, with a terminal user-facing failure message on every path | H-2 |
| P0-11 | Add explicit timeout configuration to every LLM call | H-3 |
| P0-12 | Add bounded retry with backoff for retryable LLM failures | H-3 |
| P0-13 | Add Celery task time limits so a hung call cannot occupy a worker indefinitely | H-3 |

### P1 — Architecture and correctness

| ID | Requirement | Audit ref |
|---|---|---|
| P1-1 | Correct `chat_id` handling so replies target the originating chat, not the sender's user ID | H-1 |
| P1-2 | Restrict the research handler to text messages; handle other content types explicitly | H-7 |
| P1-3 | Add real health and readiness checks covering process liveness, the polling task, and Redis | H-5 |
| P1-4 | Improve application lifecycle and shutdown: use `lifespan`, await startup, stop polling cleanly on SIGTERM | H-6 |
| P1-5 | Introduce a service layer so Telegram handlers and Celery tasks contain no business logic | M-4, audit § 7 |
| P1-6 | Introduce dependency injection for the Telegram bot and the LLM client instead of module-level construction | M-4 |
| P1-7 | Add typed validation around LLM responses with a schema model and safe defaults | H-4, M-10 |
| P1-8 | Address rate limiting and cost control: per-user throttle, concurrency cap, queue-depth guard | M-8 |
| P1-9 | Improve Celery configuration: `acks_late`, worker-loss rejection, broker retry on startup, prefetch, time limits, result handling | M-9, H-3 |
| P1-10 | Add Docker application and worker services plus a `Dockerfile`; add Compose healthchecks | H-8 |
| P1-11 | Remove unused dependencies and unused imports | M-5 |
| P1-12 | Consolidate logging: single initialization, lazy interpolation, stop logging full user text, log task failures, add correlation identifiers | M-7 |
| P1-13 | Make configuration robust: absolute `env_file` resolution, defaults where safe, validators, fail-fast with a readable message | M-6 |
| P1-14 | Add a test harness and foundational tests, including the required regression test below | Audit § 8 |

### Required regression test (from the audit)

> **Verify that `process_research` can execute successfully more than once within the same worker process.**

This test exists specifically to catch audit C-3. No test currently detects that defect. It must be written so that running the task twice in one process would fail against the current implementation.

### Additional foundational tests

- LLM response parsing: malformed JSON, missing keys, null content, empty content.
- Message formatting: length boundary, parse-mode safety, escaping of user- and model-derived text.
- Handler behavior: non-text messages, unknown commands, missing `from_user`, group `chat_id` resolution.
- Settings loading: all required variables resolve; missing-variable failure is readable.
- Health check: returns non-200 when a dependency is unavailable.

### Supporting work permitted in this phase

- `pyproject.toml` with formatter, linter, and type-checker configuration.
- CI configuration running the test suite and static checks.
- `README` documenting how to run the two processes.
- `Makefile` or equivalent run commands.
- `.dockerignore`.
- Reconciliation of inconsistent product naming (`Research Bot Core` / `DeepVoyager` / `research_bot`).
- Removal of the obsolete `version` key from `docker-compose.yml`.

### Explicitly permitted judgment calls

The following are not specified by the repository or the audit and may be chosen during implementation, provided the choice is recorded in the Architecture Decision Log:

- Structured logging format.
- Test framework configuration details beyond the requirement that tests run and are meaningful.
- Container base image and process supervision approach.

---

## 4. Out of Scope

**MUST NOT be implemented in this phase.**

- Web search of any kind.
- URL fetching, HTML extraction, or content cleaning.
- Source ranking, deduplication, summarization, or citation generation.
- Report generation.
- Any document or file handling — no PDF, DOCX, TXT, or Markdown ingestion.
- Any database model, table, migration, or session management. **The data layer is Phase 2.** The unused `DATABASE_URL`, `asyncpg`, and `sqlalchemy` declarations stay as they are during this phase unless removed as unused dependencies.
- Conversation persistence or memory.
- Any user account, authentication, or multi-tenancy.
- Any model selection UI, provider abstraction beyond what P0-1/P0-2 require, or fallback chain.
- Streaming responses.
- Any multi-agent orchestration. `app/agents/supervisor.py` is not to be expanded into an agent system in this phase.
- Any Telegram command beyond fixing and documenting the existing ones. `/help` is Phase 2.
- Any web dashboard, API surface beyond health checks, or monetization.
- Any change to the DeepSeek endpoint or model choice. P0-1 and P0-2 make the existing configuration *correct and configurable*, not different.

**Scope leakage rule:** if a Phase 1 fix appears to require a research capability, stop and document the design question. Do not build the capability.

---

## 5. Functional Requirements

### FR-1 — Configuration and secrets
- FR-1.1 No application source file contains a hardcoded secret.
- FR-1.2 The LLM API key is read from settings, never from a literal.
- FR-1.3 The LLM base URL and model name are read from settings with current values as defaults.
- FR-1.4 `.env.example` lists every required variable with placeholder values only.
- FR-1.5 `.gitignore` excludes `.env`, `venv/`, `__pycache__/`, and test/build artifacts.
- FR-1.6 A missing required variable produces a readable configuration error, not an import-time traceback.
- FR-1.7 Settings resolve identically regardless of the process working directory.

### FR-2 — Dependency correctness
- FR-2.1 Every module imported by `app/` has a corresponding pinned entry in `requirements.txt`.
- FR-2.2 The application starts from a clean environment built only from declared dependencies.
- FR-2.3 Dependencies with no importing module are removed or explicitly justified.

### FR-3 — Telegram lifecycle
- FR-3.1 Replies are sent to the originating chat, verified in both private and group chats.
- FR-3.2 Non-text messages receive an explicit, correct response and never enqueue a research task.
- FR-3.3 Unknown commands receive a helpful response rather than being processed as free text.
- FR-3.4 Every received message produces exactly one terminal response: a result or a failure notice.
- FR-3.5 Responses respect Telegram's maximum message length and are chunked when necessary.
- FR-3.6 Response formatting is consistent with the configured parse mode, and model- or user-derived text cannot break message parsing.
- FR-3.7 The application stops polling cleanly on shutdown.

### FR-4 — Celery reliability
- FR-4.1 A single worker process can execute multiple research tasks successfully in sequence.
- FR-4.2 The Telegram HTTP session is created once per worker process and closed on worker shutdown.
- FR-4.3 Every task has a hard time limit and a soft time limit.
- FR-4.4 Retryable failures are retried a bounded number of times with backoff; non-retryable failures are not retried.
- FR-4.5 Task results do not accumulate indefinitely in the result backend.
- FR-4.6 A task failure is logged with enough context to diagnose it.

### FR-5 — LLM client
- FR-5.1 Every LLM call specifies an explicit timeout.
- FR-5.2 Every LLM call specifies an explicit token limit.
- FR-5.3 Model output is parsed into a validated structure with safe defaults; malformed output does not crash the task.
- FR-5.4 The LLM client is injectable so it can be replaced in tests.

### FR-6 — Error handling
- FR-6.1 The Telegram dispatcher has a registered error handler.
- FR-6.2 The research task body is fully wrapped; no exception escapes without a user-facing notice.
- FR-6.3 Broker unavailability is handled without an unhandled exception in the handler.
- FR-6.4 LLM failure, malformed LLM output, and Telegram send failure each produce a distinct, logged outcome.

### FR-7 — Health and readiness
- FR-7.1 Health reports the real state of the process, not a static literal.
- FR-7.2 Readiness verifies broker connectivity.
- FR-7.3 A failed dependency yields a non-200 response.
- FR-7.4 The poller's liveness is observable; a stopped poller is detectable.

### FR-8 — Cost control
- FR-8.1 Per-user request throttling exists.
- FR-8.2 A global concurrency or queue-depth cap exists.
- FR-8.3 Throttled requests receive an explanatory response.
- FR-8.4 Telegram flood-control responses are handled rather than treated as generic failures.

### FR-9 — Logging
- FR-9.1 Logging is initialized exactly once.
- FR-9.2 Log records use lazy interpolation.
- FR-9.3 Full user message text is not written to logs.
- FR-9.4 Task and request correlation identifiers are present on relevant records.
- FR-9.5 No secret value can appear in log output.

### FR-10 — Runtime and deployment
- FR-10.1 A `Dockerfile` builds a runnable application image.
- FR-10.2 Compose defines an application service and a worker service.
- FR-10.3 Compose defines healthchecks and dependency conditions.
- FR-10.4 The stack starts from a clean checkout using documented commands.
- FR-10.5 Infrastructure credentials are not published with default values.

---

## 6. Technical Requirements

### TR-1 — Event-loop ownership (addresses C-3)

The current implementation creates a new event loop per task and closes it, while the module-level `Bot` caches an aiohttp `ClientSession` bound to the first loop. The corrected design must establish **one** long-lived event loop per worker process and never close it per task, with the Telegram session closed on worker shutdown.

A suitable mechanism is a Celery worker lifecycle signal that initializes the loop once per process. The specific mechanism is an implementation choice; the ownership model is not.

### TR-2 — Layering

```text
app/bot/          Telegram transport. Parse update, delegate, respond. No business logic.
app/services/     Application logic. Orchestrates domain + infrastructure. NEW in this phase.
app/domain/       Pure logic and contracts. NEW in this phase, kept minimal.
app/infrastructure/ External clients: LLM, Telegram, broker, future persistence. NEW.
app/tasks/        Celery entry points. Call a service, report the outcome. No logic.
```

`app/bot/handlers.py` currently imports from `app/tasks/`, and `app/tasks/research_task.py` imports `bot` from `app/bot/dispatcher.py`. That circular dependency must be broken by this phase.

### TR-3 — Dependency injection

The `Bot` and the LLM client must be constructed in one place and passed to consumers. Consumers must not import module-level singletons. This is the prerequisite for the required regression test.

### TR-4 — Configuration

`Settings` should use the current pydantic-settings idioms for the pinned version, resolve `env_file` independently of the working directory, and separate required values from ones with safe defaults. LLM endpoint, model, and timeouts are configuration, not code.

### TR-5 — Message formatting

Formatting must be a single, testable, pure function: input is model output, output is one or more safely formatted Telegram messages. It owns escaping and length chunking. It must be independently testable without network access.

### TR-6 — Health checks

`/health` reflects process liveness. `/ready` verifies broker connectivity. Both are dependency-injected so they can be tested.

### TR-7 — Containerization

Two long-running services sharing one image: the API/poller and the Celery worker. Compose healthchecks and dependency conditions must be present. Published database and broker ports must not use default credentials.

### TR-8 — Test infrastructure

A test runner added to declared dependencies. Tests must not require network access. Celery must be invocable in eager or direct mode for tests so tasks can run in-process — which is what makes the required regression test possible.

---

## 7. Security Requirements

- **SR-1** No secret literal in any file under `app/`.
- **SR-2** `.env` is excluded from version control before the repository is initialized.
- **SR-3** `.env.example` contains no real values.
- **SR-4** The exposed LLM key and the `.env` bot token are rotated. **This is a manual human action and cannot be automated.** It is tracked here so it is not lost.
- **SR-5** Secrets are never written to logs, including on exception paths.
- **SR-6** User-controlled text is never passed to the LLM in a way that grants it capability. The current single user-role prompt remains acceptable in this phase; prompt hardening becomes relevant in Phase 3 when external content is introduced.
- **SR-7** No file path, URL, or command is derived from user input in this phase.
- **SR-8** Infrastructure services do not run with default or published credentials.
- **SR-9** The health endpoint reveals no secret and no internal credential detail.
- **SR-10** A secret-scanning check runs in CI to prevent regression.

### Security baseline note

The audit found no SQL, no `subprocess`/`eval`/`exec`, no URL fetching, no unsafe deserialization, and no file-path handling from user input. Those surfaces do not exist yet, so this phase establishes the baseline rather than remediating a vulnerability. SSRF and prompt injection become live risks in Phase 3 and must be addressed there.

---

## 8. Testing Requirements

Tests are a **completion gate** for this phase, not an optional extra.

| ID | Test | Verifies |
|---|---|---|
| T-1 | **`process_research` executes successfully more than once within the same worker process** | C-3 — the audit's required regression test |
| T-2 | LLM response parsing handles valid JSON, malformed JSON, missing keys, null content, empty content | H-4 |
| T-3 | Message formatting respects the maximum message length and chunks correctly | C-4 |
| T-4 | Message formatting is safe for text containing markup-significant characters | C-4 |
| T-5 | The research handler produces no task for a non-text message | H-7 |
| T-6 | The research handler resolves the correct `chat_id` for private and group chats | H-1 |
| T-7 | An unknown command does not enqueue a research task | H-7 |
| T-8 | LLM failure results in a user-facing failure notice and a logged error, not a silent drop | H-2 |
| T-9 | Broker unavailability in the handler does not raise an unhandled exception | H-2 |
| T-10 | Settings load correctly and a missing variable produces a readable error | M-6 |
| T-11 | Health returns non-200 when a dependency is unavailable | H-5 |
| T-12 | Rate limiting rejects a request past the configured threshold | M-8 |
| T-13 | The application starts from a clean environment built from declared dependencies only | C-2 |

**Coverage expectations:**

- Every P0 requirement has at least one test.
- Failure paths are covered for every P0 item. A test suite that only exercises success paths does not satisfy this phase.
- No test performs a real network call to an LLM or Telegram endpoint.

---

## 9. Documentation Requirements

- [x] `README.md` created: prerequisites, environment setup, how to run the API process, how to run the worker, how to run tests, Phase 1 limitations, and the outstanding rotation action.
- [x] `docs/PROJECT_PLAN.md` updated: maturity table, phase status table, decision log entries (AD-010 … AD-014).
- [x] `docs/ARCHITECTURE.md` **preserved unmodified** and explicitly designated the historical audit snapshot; its header and § 14 now state that it describes the pre-Phase-1 repository, so it is not silently false.
- [x] `.env.example` created and complete.
- [ ] **Secrets-rotation record.** — NOT CREATED: it cannot be truthfully created, because rotation has not been performed. Tracked as an open blocking item instead of being marked complete.
- [x] New modules and architectural boundaries documented (README architecture table, module docstrings carrying audit references).

---

## 10. Acceptance Criteria

### Security and configuration

- [x] No application source file contains a hardcoded secret. (Enforced by `tests/test_config_and_security.py::test_sr1_no_secret_literal_in_application_source`)
- [x] The LLM API key is read from configuration. (`app/infrastructure/llm.py` reads `settings.GROQ_API_KEY`)
- [x] `.gitignore` exists and excludes `.env`, `venv/`, and `__pycache__/`.
- [x] `.env.example` exists and lists every required variable with no real values.
- [ ] **The exposed LLM key and the `.env` bot token have been rotated.** — **NOT DONE: requires human access to @BotFather and the provider dashboard. This is the blocking item.**
- [x] Secrets do not appear in log output. (Verified against a live run that included a provider traceback)

### Dependencies and startup

- [x] Application starts from a clean environment. (Fresh venv from `requirements.txt` only; all 20 modules import)
- [x] Required dependencies are declared. (`openai` and `pydantic` were both missing and were added)
- [x] Dependencies with no importing module are removed or justified. (`httpx`/`python-dotenv` dropped as transitive; `redis`/`asyncpg`/`sqlalchemy`/`uvicorn` justified in-file; enforced by test)

### Reliability

- [x] Telegram messages can be processed repeatedly by the same worker. (T-1, both service and task boundary)
- [x] The regression test for repeated execution within one worker process exists and passes. (`tests/test_t1_event_loop_regression.py`, `tests/test_t1_task_boundary.py`; includes a canary reproducing the original failure)
- [x] Every LLM call has an explicit timeout and token limit.
- [x] Retryable failures are retried a bounded number of times with backoff. Non-retryable failures are **not** retried (FR-4.4), verified live against a provider 402
- [x] Every Celery task has a hard and soft time limit.
- [x] Telegram responses respect platform message limits.
- [x] LLM failures result in controlled error handling.
- [x] Malformed LLM output does not crash the task.

### Correctness

- [x] Replies are delivered to the correct chat in both private and group chats. (`chat_id` and `user_id` are separate task arguments)
- [x] Non-text messages do not enqueue research tasks.
- [x] Every message receives exactly one terminal response.

### Observability

- [x] Health checks report real dependency state and return non-200 on failure. (Verified live: broker stopped → `/ready` 503 degraded; `/health` stayed 200)
- [x] Task failures are logged with diagnostic context.
- [x] Logging is initialized once and full user message text is not logged. (length and a 40-char prefix only)

### Cost control

- [x] Per-user rate limiting is enforced and tested. (in-process; see limitation note)

### Infrastructure

- [ ] **A `Dockerfile` exists and builds a runnable image.** — Dockerfile created and passes `docker build --check`; the **image build is BLOCKED** by PyPI network truncation inside the container (see § 14 blockers)
- [x] Compose defines application and worker services with healthchecks. (`docker compose config` validates; 4 services)
- [ ] **The full stack starts from a clean checkout using documented commands.** — BLOCKED by the same Docker build network failure. The two processes were validated natively instead

### Quality

- [x] Telegram handlers and Celery tasks contain no business logic. (enforced by structural test)
- [x] The circular dependency between `app/bot/` and `app/tasks/` is broken.
- [x] External clients are injectable.
- [x] Every P0 requirement has a test.
- [ ] **CI runs the test suite and static checks.** — `ruff` and `mypy` are configured and passing locally via `make verify`; **no CI provider is configured** (repository is not a git repository)

---

## 11. Dependencies

### Prerequisites

None. This is the entry phase. It depends only on the existing repository.

### Infrastructure required

- Redis, reachable at `REDIS_URL`. Declared in `docker-compose.yml`.
- PostgreSQL is **not** a prerequisite of this phase. It is declared but unused, and no phase-1 work connects to it.

### Blocks

- **Phase 2** cannot begin until T-1, the credential fixes, the error-handling requirements, and the service-layer boundary exist. Phase 2 introduces the data layer and depends on a service layer and an injectable LLM client.
- **Phase 3** inherits the LLM client abstraction, the message-formatting boundary, and the Celery reliability work.
- **Phase 6** long-running jobs, and Phase 7's long research jobs, depend directly on the Celery reliability work in this phase.

### Does not block

Nothing. This phase can proceed independently.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Fixing C-3 requires reworking the worker/async boundary, which touches the only working request path | A regression could leave the bot worse off | T-1 is the gate. It must pass before the phase is considered stable |
| Rotating credentials breaks the running bot until `.env` is updated | Bot offline during rotation | Sequence: update `.env` and source together, then rotate at the provider, then verify |
| Scope leakage into Phase 2 or 3 features | Phase boundary erosion; untestable, unstable code | Enforce § Out of Scope. Document blockers rather than building around them |
| Adding a service layer is a structural change to a small codebase | Larger diff, more review surface | Keep the layer minimal. Every new module must have a caller in the same change |
| Test-first work on code with no seams requires refactoring first | Slower start | Order the work: injection and service boundary first, then the tests that need them |
| Compose changes may disturb a working local setup | Developer friction | Keep service names and volumes stable; document before changing |
| Removing "unused" dependencies may remove something needed at runtime indirectly | Startup failure | Verify with a clean-environment build as part of FR-2.2 |
| Health checks that probe dependencies can make `/health` slow or flaky | Monitoring noise | Set explicit timeouts on probes; keep liveness cheap and separate from readiness |
| Rotating a credential is a human step and can be forgotten | A disclosed key remains valid | Tracked as SR-4 and as an explicit acceptance criterion, not just prose |

---

## 13. Definition of Done

Phase 1 is complete when **all** of the following are true:

1. Every acceptance criterion in § 10 is checked and evidenced.
2. Every P0 requirement in § 3 is implemented and covered by a test.
3. Every P1 requirement in § 3 is implemented or explicitly deferred with a recorded reason.
4. The T-1 regression test exists and passes.
5. The application starts from a clean environment using only declared dependencies.
6. Both credentials have been rotated and no secret appears in any tracked file.
7. The full stack starts from a clean checkout using documented commands.
8. Documentation is updated: README, PROJECT_PLAN status tables, decision log, and ARCHITECTURE.md.
9. No Phase 2–9 functionality was implemented.
10. The Phase status in this document and in `docs/PROJECT_PLAN.md` reads `Complete`, with evidence.

**Partial completion is not completion.** If some items are done and others are not, the status is `In Progress` and the remaining items are listed explicitly.

---

## 14. Status

**Current status: In Progress.**

Implementation and validation are complete. The phase is **not** complete, because
three items cannot be satisfied in this environment:

1. **Credential rotation** — requires human access to @BotFather and the provider
   dashboard. SR-4, P0-3, and the related acceptance criterion remain open.
2. **Docker image build** — the `Dockerfile` is written and passes
   `docker build --check`, but `pip install` inside the container fails on PyPI
   network truncation (`IncompleteRead` / `SSLError` from pypi.org). The same
   `requirements.txt` installs cleanly on the host, so this is an environment
   fault, not a defect in the file. **NOT VERIFIED — the image has never been
   built or run.**
3. **CI configuration** — `ruff`, `mypy`, and `pytest` are wired through
   `make verify` and all pass, but no CI provider is configured because the
   repository is not a git repository.

**Phase status in `docs/PROJECT_PLAN.md`: In Progress.**

### What changed

```text
BEFORE (8 files with code)          AFTER (18 files with code)
─────────────────────────           ────────────────────────────
app/main.py            23 lines     app/main.py                125  lifespan, /health, /ready
app/core/config.py     11 lines     app/core/config.py         140  v2 settings, SecretStr
app/core/celery_app.py 16 lines     app/core/celery_app.py      63  reliability config
                                    app/core/logging_config.py   67  single init, redaction
                                    app/domain/schemas.py        60  QueryAnalysis
                                    app/domain/errors.py         68  typed + retryable
                                    app/infrastructure/
                                      asyncio_runtime.py         85  per-process loop (C-3)
                                      llm.py                    175  timeout/limit/validation
                                      telegram.py                47  factory, not singleton
                                    app/services/
                                      formatting.py            110  escape + chunk
                                      delivery.py              104  long-lived-loop send
                                      research.py              143  orchestration
                                      rate_limit.py            116  throttle + cap
                                      health.py                 90  liveness vs readiness
                                    app/bot/container.py        66  composition root
                                    app/bot/handlers.py        124  thin, F.text, chat_id
                                    app/tasks/lifecycle.py      45  worker signals
                                    app/tasks/research_task.py  72  thin task
                                    app/agents/supervisor.py    34  injected client
```

Total: 8 → 18 modules, ~150 → ~1,700 statements. 0 → 123 tests.

### Defects resolved, with the evidence for each

| Audit ID | Status | Evidence |
|---|---|---|
| C-1 hardcoded key | **Resolved** | `supervisor.py` reads `settings.GROQ_API_KEY`; `SecretStr`; enforced by test; zero secret-shaped literals in `app/` |
| C-2 `openai` undeclared | **Resolved** | Added `openai==2.44.0`; also found and added the missing `pydantic`; clean venv imports all 20 modules |
| C-3 event-loop lifecycle | **Resolved** | `app/infrastructure/asyncio_runtime.py` + `app/tasks/lifecycle.py`; T-1 passes at service **and** task boundary; a canary test reproduces the original failure |
| C-4 parse mode / length | **Resolved** | `formatting.escape` + `split_for_telegram`; HTML-escaping and 4096-chunking tested; markup-significant and 9,000-char replies verified |
| H-1 `chat_id` confusion | **Resolved** | `chat_id` and `user_id` are separate task arguments; group and private routing tested through a real Dispatcher |
| H-2 no error handling | **Resolved** | Task fully wrapped, `@router.errors()` registered, enqueue failure surfaced, user notice on every path |
| H-3 no limits / timeouts | **Resolved** | LLM timeout + `max_tokens`; Celery soft/hard time limits, acks-late, bounded backoff retry |
| H-4 unguarded JSON | **Resolved** | `QueryAnalysis` validates at the boundary; malformed/null/empty/wrong-type all tested |
| H-5 static `/health` | **Resolved** | `/health` liveness + `/ready` probing poller and broker; **verified live**: broker stopped → 503 degraded, restored → 200 |
| H-6 no graceful shutdown | **Resolved** | `lifespan`; `handle_signals=False`; **verified live**: SIGTERM → clean exit in ~1s |
| H-7 unfiltered handler | **Resolved** | `F.text`; non-text rejected explicitly; verified through real filter resolution |
| H-8 no containerisation | **Partially resolved** | Dockerfile + api/worker compose services + healthchecks written and compose-valid; **image build blocked by network** |
| M-5 unused deps/imports | **Resolved** | `httpx`/`python-dotenv` dropped as transitive; rest justified in-file; enforced by test |
| M-6 CWD-dependent config | **Resolved** | Absolute `env_file`; `ConfigurationError` naming the field; tested from a foreign CWD |
| M-7 logging | **Resolved** | Single init, lazy `%s`, no full user text, correlation context, redaction filter |
| M-8 no rate limiting | **Resolved** | Per-user sliding window + global in-flight cap; tested |
| M-9 unbounded results | **Resolved** | `task_ignore_result=True` |
| M-10 untyped contract | **Resolved** | `QueryAnalysis`; mypy clean across 28 files |
| S-2 no `.gitignore` | **Resolved** | `.gitignore` + `.env.example`; enforced by test |

### Defects found during implementation, not in the audit

These were introduced or exposed while implementing, and were fixed in the same
change. Recorded because they are real findings.

| ID | Defect | Resolution |
|---|---|---|
| F-1 | **Handler ordering bug.** The unfiltered catch-all was registered before the text handler, so every text message was swallowed and answered with "text only". Caught only because routing is tested through a real `Dispatcher` rather than by calling callbacks directly. | Catch-all moved last; `test_telegram_routing.py` guards the order |
| F-2 | **Tests passing for the wrong reason.** `research_task` bound `get_container` at module scope, so patching the container module never reached the task. Tests were reaching the real container with real `.env` credentials. | Task now resolves through the module; autouse `forbid_real_container` guard makes any recurrence a hard failure (verified) |
| F-3 | **Graceful shutdown hang.** aiogram's `start_polling` installs its own signal handlers that stop uvicorn's loop; SIGTERM left the process alive indefinitely and the lifespan never completed. Found by live shutdown testing, not by the unit tests. | `handle_signals=False`; verified clean exit in ~1s |
| F-4 | **`run_coroutine` inside a running loop.** `container.close()` called `run_until_complete` from the lifespan, raising `Cannot run the event loop while another loop is running` on every shutdown. | Added `aclose()`; lifespan awaits it |
| F-5 | **Non-retryable failures were retried.** A provider 402 "Insufficient Balance" was retried 3× per task, violating FR-4.4. Found by observing a real end-to-end run. | `retryable` on domain errors; 4xx (except 408/409/429) are terminal. Verified live: `retryable=False will_retry=False` |
| F-6 | **Retry config duplicated.** Decorator said `max_retries=3`, `task_annotations` said 2; the annotation won silently. | Single source of truth in settings |
| F-7 | **Network reached the real Telegram API during tests.** `Message.answer` bypasses `Bot.send_message` and goes through `AiohttpSession.__call__`, so the first stub was ineffective and a test made a live API call. | Stub at the session boundary + autouse socket guard that fails any outbound connection |

### Validation actually performed

| Check | Command | Result |
|---|---|---|
| Test suite | `pytest -q` | **123 passed**, 0 failed |
| Lint | `ruff check app tests` | All checks passed |
| Types | `mypy app` | Success, 28 source files |
| Clean-environment import | fresh venv, `requirements.txt` only, all 20 modules | All import |
| Dockerfile lint | `docker build --check .` | No warnings |
| Compose validity | `docker compose config` | Valid; 4 services |
| Docker image build | `docker build .` | **FAILED** — PyPI `IncompleteRead`/`SSLError` inside the container. Attempted 5 times |
| API startup | `uvicorn app.main:app` | Starts; `/health` 200, `/ready` 200 |
| Readiness failure path | stopped the Redis container | `/ready` **503** `degraded`, broker identified; `/health` stayed 200; recovery → 200 |
| Worker startup | `celery -A app.core.celery_app:celery_app worker` | Ready; task registered; prefetch pool |
| End-to-end | real Telegram message through the real worker | Provider reached; 402 handled; user notified; no retries |
| Graceful shutdown | SIGTERM to both processes | Both exited cleanly, no warnings, no leaked sessions |
| Secret containment | regex scan of live API and worker logs | Clean, including inside a provider traceback |

### Known limitations, recorded deliberately

- **Rate limiting is in-process.** Correct for a single API process; the counters
  stop being global if the API tier is scaled horizontally. A shared store is
  required before that.
- **`DATABASE_URL`, `asyncpg`, `sqlalchemy` are declared and unused.** No
  database code exists; `/ready` does not probe PostgreSQL, because claiming a
  check that does not exist would be false.
- **`Supervisor.generate_final_answer` is still unwired** (audit M-1). Retained
  so a stabilization phase removes no capability; wired in Phase 3.
- **Long polling only.** No webhook, so no webhook secret-token surface.
- **User-facing strings are hardcoded English literals** (audit L-2). They were
  Persian before this change and are now English, but the underlying debt —
  no i18n layer — is unchanged. i18n remains Phase 7 and is not addressed here.

### Work item status — P0

| ID | Requirement | Status |
|---|---|---|
| P0-1 | Remove hardcoded LLM key | Implemented |
| P0-2 | Read credentials from settings | Implemented |
| P0-3 | Rotate exposed credentials | **Not Implemented — manual human action, blocking** |
| P0-4 | Add `.gitignore` | Implemented |
| P0-5 | Add `.env.example` | Implemented |
| P0-6 | Add missing `openai` dependency | Implemented |
| P0-7 | Fix event-loop lifecycle | Implemented |
| P0-8 | Resolve parse-mode mismatch | Implemented |
| P0-9 | Handle message length limits | Implemented |
| P0-10 | Add task and LLM error handling | Implemented |
| P0-11 | Add LLM timeout configuration | Implemented |
| P0-12 | Add bounded retry with backoff | Implemented |
| P0-13 | Add Celery task time limits | Implemented |

### Work item status — P1

| ID | Requirement | Status |
|---|---|---|
| P1-1 | Correct `chat_id` handling | Implemented |
| P1-2 | Restrict handler to text messages | Implemented |
| P1-3 | Real health and readiness checks | Implemented |
| P1-4 | Application lifecycle and shutdown | Implemented |
| P1-5 | Service layer | Implemented |
| P1-6 | Dependency injection | Implemented |
| P1-7 | Typed LLM response validation | Implemented |
| P1-8 | Rate limiting and cost control | Implemented (in-process; see limitations) |
| P1-9 | Celery configuration | Implemented |
| P1-10 | Docker application and worker services | **Partial** — files written and compose-valid; image build blocked by network |
| P1-11 | Remove unused dependencies | Implemented |
| P1-12 | Consolidate logging | Implemented |
| P1-13 | Robust configuration | Implemented |
| P1-14 | Test harness and foundational tests | Implemented |

### Work item status — security requirements

| ID | Requirement | Status |
|---|---|---|
| SR-1 | No secret literal in any file under `app/` | Implemented (test-enforced) |
| SR-2 | `.env` excluded from version control | Implemented |
| SR-3 | `.env.example` contains no real values | Implemented |
| SR-4 | **Credentials rotated** | **Not Implemented — manual human action, blocking** |
| SR-5 | Secrets never written to logs | Implemented; verified against a live run |
| SR-6 | User text never gains capability | Implemented (single user-role prompt retained; no external content in Phase 1) |
| SR-7 | No file path, URL, or command from user input | Implemented (no such surface exists) |
| SR-8 | Infrastructure not on default credentials | Implemented; compose requires `POSTGRES_PASSWORD` and binds ports to loopback |
| SR-9 | Health endpoint reveals no secret | Implemented; asserted by test |
| SR-10 | Secret-scanning check in CI | **Partial** — implemented as a test, not wired to a CI provider (no git repository) |

### Work item status — tests

| ID | Test | Mandatory | Status |
|---|---|---|---|
| T-1 | Repeated execution in one worker process | Yes | **Implemented and passing**, at the service and task boundary, with a canary reproducing the original failure |
| T-2 | LLM response parsing | Yes | Implemented |
| T-3 | Message length and chunking | Yes | Implemented |
| T-4 | Markup-safety of output | Yes | Implemented |
| T-5 | Non-text messages not enqueued | Yes | Implemented |
| T-6 | Correct `chat_id` resolution | Yes | Implemented |
| T-7 | Unknown commands not enqueued | — | Implemented |
| T-8 | LLM failure yields a user notice | Yes | Implemented |
| T-9 | Broker failure not unhandled | — | Implemented |
| T-10 | Settings load; readable error | — | Implemented |
| T-11 | Readiness returns non-200 on failure | — | Implemented; verified live |
| T-12 | Rate limiting | — | Implemented |
| T-13 | Clean-environment startup | Yes | Implemented; verified in a fresh venv |

### Blockers

| Blocker | Blocks | Who resolves it |
|---|---|---|
| **Credential rotation** (P0-3, SR-4) | Phase 1 completion | **Human.** @BotFather `/token` revoke; provider dashboard revoke; the previously hardcoded OpenRouter-format key revoked in the OpenRouter account |
| **Docker image build** (P1-10) | Two acceptance criteria | Environment. Retry `docker build .` on a network that can reach pypi.org |
| **CI provider** (SR-10 tail) | One acceptance criterion | Human. `git init`, then add a CI workflow running `make verify` |

### Update procedure

When an item is completed:

1. Set its status in the tables above.
2. Tick the corresponding acceptance criterion in § 10.
3. Update the phase status table in `docs/PROJECT_PLAN.md`.
4. Record any decision in the Architecture Decision Log.
5. If `docs/ARCHITECTURE.md` is later updated, keep its audit findings intact as
   the pre-Phase-1 baseline and add a dated note rather than rewriting them.
