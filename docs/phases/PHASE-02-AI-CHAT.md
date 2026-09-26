# Phase 2 — AI Chat MVP

**Status:** In Progress — implementation and validation complete; blocked on Phase 1 credential rotation
**Active Phase:** Yes
**Blocked by:** Phase 1 — Core Infrastructure & Stabilization (In Progress)
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — historical audit snapshot, preserved unmodified
**Implementation record:** § 14. This document's § 2 describes the repository *before* this phase; its findings are retained as the baseline, not as a statement of current state.

> Every requirement below traces to this document. Statuses in § 14 reflect what was
> implemented and validated, not what was intended. Historical audit findings are not
> rewritten.

---

## 1. Objective

Turn the existing Telegram/LLM vertical slice into a reliable conversational AI assistant.

The current system performs a single stateless LLM call per message. This phase makes it a conversation: persistent users, persistent messages, real context, and a provider-independent LLM service.

---

## 2. Current State

### What exists (CURRENT)

| Capability | Evidence | State |
|---|---|---|
| Telegram message reception | `app/bot/handlers.py:15-22` | Partial — unfiltered catch-all |
| LLM call | `app/agents/supervisor.py:36-41` | Partial — no timeout, no token limit, no validation |
| Response formatting | `app/tasks/research_task.py:12` | Partial — Markdown text sent under HTML parse mode |
| `/start` command | `app/bot/handlers.py:9-13` | Partial — single hardcoded greeting |
| PostgreSQL declared | `docker-compose.yml:10-20` | Not implemented — nothing connects |
| `DATABASE_URL` declared | `app/core/config.py:6` | Not implemented — never imported |
| `asyncpg`, `sqlalchemy` declared | `requirements.txt:6-7` | Not implemented — never imported |

### What does not exist (NOT IMPLEMENTED)

Conversation persistence. User records. Message records. Conversation history. Any form of memory or context. An LLM service abstraction. Provider abstraction. `/help`. Unknown-command handling. Streaming. Model selection. Fallback models. Token budgeting. Per-user rate limiting at the application layer.

### The core gap

`Supervisor.analyze_query` (`app/agents/supervisor.py:16`) receives a single string and returns a single JSON object. There is no record of who sent the message, no record of what was sent, and no way to recall a previous exchange. The bot cannot hold a conversation.

The name `analyze_query` and the JSON schema it requests (`summary` plus `sub_questions`) reflect a research-decomposition intent, not a chat intent. That schema should be revisited in this phase rather than carried forward unexamined.

---

## 3. Scope

- User and conversation data models, with migrations.
- Message persistence.
- Conversation history retrieval.
- Context construction and token-window management.
- An LLM service abstraction decoupling the application from any provider.
- Provider configuration held in settings.
- `/help` and explicit unknown-command handling.
- User-visible error feedback for every failure path.
- Application-level rate limiting and cost controls.
- Model configuration with retry and fallback strategy.
- Conversation lifecycle: start, continue, list, reset.

---

## 4. Out of Scope

- **Web search, URL fetching, extraction, ranking, citations, report generation.** That is Phase 3. Do not let research logic enter the chat service.
- Document or file handling. That is Phase 4.
- Research projects, workspaces, saved research, tags, or bookmarks. That is Phase 5.
- Multi-agent orchestration. That is Phase 6.
- Deep Research mode, exports, or premium features. That is Phase 7.
- Any web dashboard, API surface, or monetization. Phases 8 and 9.
- Replacing the DeepSeek endpoint. This phase makes the provider **configurable and swappable**; it does not select a different provider by default.
- Telegram webhook mode. Long polling remains the transport; changing it is not required by this phase.
- Fixing Phase 1 defects. If one is discovered, report it — do not absorb it here.

---

## 5. Functional Requirements

### Conversation persistence
- FR-1 A user record exists for every Telegram user who interacts with the bot, keyed on the platform user identifier.
- FR-2 Every inbound user message is persisted with its chat, sender, timestamp, and content.
- FR-3 Every outbound bot message is persisted, or the decision not to persist outbound messages is recorded.
- FR-4 A conversation record groups messages and supports multiple conversations per user.
- FR-5 A user can start a new conversation, continue an existing one, list their conversations, and reset the current one.
- FR-6 Deleting or resetting a conversation does not delete the user record.

### Context management
- FR-7 Prior messages are retrieved in chronological order for the active conversation.
- FR-8 The assembled context fits within the configured model context window.
- FR-9 When the context exceeds the window, a defined strategy applies — truncation, summarization of older turns, or a token budget per turn. The strategy is a configuration decision, not a hardcoded constant.
- FR-10 A per-message token estimate is enforced before calling the provider.
- FR-11 The system prompt is held separately from user content and is never built by concatenating user input.

### LLM service
- FR-12 An LLM service abstraction exists. The application layer depends on the abstraction, not on a provider SDK.
- FR-13 Provider credentials, endpoint, and model are configuration.
- FR-14 At least one provider is implemented behind the abstraction. The currently configured DeepSeek-compatible endpoint is the natural first candidate because it is what the repository already uses.
- FR-15 Provider responses are returned as a validated domain type, never as a raw provider object.
- FR-16 The abstraction supports at least one alternate provider or a documented extension point, so the choice is reversible.

### Commands and UX
- FR-17 `/help` lists available commands with a short description of each.
- FR-18 Unknown commands receive a helpful response listing valid commands.
- FR-19 Command arguments are validated before use.
- FR-20 Commands that change state require an explicit confirmation where destructive.

### Reliability
- FR-21 Every message produces exactly one terminal response: a result, a failure notice, or a throttling notice.
- FR-22 A provider failure produces a distinct, user-readable message that does not disclose internal detail.
- FR-23 A persistence failure produces a distinct, user-readable message.
- FR-24 Responses respect Telegram message length limits and are chunked.
- FR-25 Long responses stream or arrive in multiple messages without duplication or loss.

### Rate limiting and cost
- FR-26 Per-user request throttling is enforced with a configurable limit and window.
- FR-27 A global concurrency cap prevents provider cost spikes.
- FR-28 A per-user daily or monthly request or token allowance exists.
- FR-29 Throttled requests receive an explanatory response stating when the user may retry.
- FR-30 Token usage is recorded per request for cost visibility.

### Model strategy
- FR-31 The model is configurable per request or per conversation.
- FR-32 A fallback chain exists: on a retryable provider failure, the next configured model is attempted.
- FR-33 Fallback is bounded and does not multiply cost without limit.
- FR-34 Non-retryable errors — authentication failure, invalid request, content policy — do not trigger fallback.

---

## 6. Technical Requirements

### Data layer

The audit confirms **zero** database code exists. This phase introduces it.

```text
User         — platform user id (unique), username, locale, created_at, updated_at
Conversation — user id (FK), title, status, created_at, updated_at
Message      — conversation id (FK), role, content, token count, provider,
               model, created_at
```

- Migrations managed by a migration tool added as a declared dependency.
- `DATABASE_URL` wired into a real session factory. The value is already declared in `app/core/config.py:6` and currently unused.
- The declared `asyncpg` and `sqlalchemy` dependencies are the natural fit for the existing async codebase. This is a record of the current declared intent, not a new decision.
- Indexes on every foreign key and on the columns used for retrieval — notably messages by `(conversation_id, created_at)`.
- Uniqueness constraints on the platform user identifier.
- All schema changes via migrations. No ad hoc schema edits.
- The schema must anticipate Phase 5 workspaces. Retrofitting workspaces onto a chat-only schema is the single most likely source of future rework.

### Context assembly

- A dedicated context service owns retrieval, ordering, budgeting, and truncation.
- The context service has no knowledge of Telegram or of the provider SDK.
- Token estimation is isolated behind a small interface so a real tokenizer can replace an estimate without touching callers.

### LLM service boundary

```text
app/domain/            LLMProvider protocol, Message types, domain errors
app/infrastructure/llm/ Provider implementations, prompt assembly
app/services/chat/     Conversation logic, context assembly, orchestration
app/bot/               Telegram transport only
app/tasks/             Celery entry points only
```

- The service layer and dependency injection required by Phase 1 are prerequisites. This phase builds on them.
- The circular dependency between `app/bot/` and `app/tasks/` must already be broken by Phase 1.

### Prompting

- System instructions and user content are structurally separated.
- User content is delimited and never concatenated into system instructions.
- A structured output contract is validated, not trusted.
- Prompt templates are versioned or checksummed so a template change is detectable.

---

## 7. Security Requirements

- **SR-1** No secret in source. Provider credentials remain configuration.
- **SR-2** User content is untrusted input. It is never executed, never interpolated into a system instruction, and never used to construct a file path, URL, or command.
- **SR-3** Prompt injection is a live consideration now that conversation history is assembled. Stored user content must not be able to escalate its own privilege across turns.
- **SR-4** Conversation content is user data. Access is scoped to the owning user. No endpoint or service may return one user's conversation to another.
- **SR-5** Log user identifiers and metadata, not full message content, unless a documented diagnostic need exists.
- **SR-6** Persisted content is treated as untrusted on read. Content stored in an earlier turn must not be trusted more than content received now.
- **SR-7** Rate limits are enforced server-side. Client-side or prompt-based limiting is not a control.
- **SR-8** Token accounting is tamper-resistant — derived server-side from actual usage, not from client claims.
- **SR-9** Deletion semantics are defined: a user can delete their data, and deletion actually removes it.
- **SR-10** No SQL is built by string concatenation. Parameterized statements or an ORM only.

---

## 8. Testing Requirements

| ID | Test | Verifies |
|---|---|---|
| T-1 | A user record is created on first interaction and reused thereafter | FR-1 |
| T-2 | A second message in the same conversation is persisted and linked correctly | FR-2, FR-4 |
| T-3 | History is returned in correct chronological order | FR-7 |
| T-4 | Context is truncated or summarized when it exceeds the configured window | FR-8, FR-9 |
| T-5 | The service layer works against a fake provider with no network access | FR-12 |
| T-6 | Swapping the provider implementation requires no change to the service layer | FR-16 |
| T-7 | System instructions are never built from user content | FR-11, SR-3 |
| T-8 | `/help` lists commands; an unknown command returns a helpful response | FR-17, FR-18 |
| T-9 | A provider failure yields a user-readable message and no internal detail | FR-22 |
| T-10 | A persistence failure yields a user-readable message | FR-23 |
| T-11 | Every message produces exactly one terminal response | FR-21 |
| T-12 | Rate limiting rejects the request past the threshold and explains the retry time | FR-26, FR-29 |
| T-13 | The concurrency cap holds under load | FR-27 |
| T-14 | A retryable failure triggers fallback to the next model | FR-32 |
| T-15 | A non-retryable failure does not trigger fallback | FR-34 |
| T-16 | One user's conversation is never returned for another user | SR-4 |
| T-17 | Long responses are chunked without loss or duplication | FR-24 |
| T-18 | Migrations apply cleanly to an empty database and roll back | Data layer |
| T-19 | Foreign keys and uniqueness constraints are enforced | Data layer |
| T-20 | Token usage is recorded per request | FR-30 |

**Coverage expectations:** every FR group has at least one test. Authorization isolation (T-16) and prompt-injection resistance (T-7) are mandatory, not optional. Tests run against a real test database and a fake provider, never a live endpoint.

---

## 9. Documentation Requirements

- [ ] Data model documented, including the entity relationship and every index.
- [ ] Migration procedure documented.
- [ ] The LLM provider abstraction documented, including how to add a provider.
- [ ] Context-window and token-budget strategy documented, including the truncation choice and its rationale.
- [ ] All commands documented in the README and in `/help`.
- [ ] `docs/PROJECT_PLAN.md` maturity table and phase status updated.
- [ ] `docs/ARCHITECTURE.md` updated with the new data and service layers.
- [ ] Any provider or context decision recorded in the Architecture Decision Log.
- [ ] Rate limits and defaults documented, including how an operator changes them.

---

## 10. Acceptance Criteria

### Persistence
- [ ] User, conversation, and message records exist and are created automatically.
- [ ] Migrations apply and roll back cleanly on an empty database.
- [ ] Foreign keys and uniqueness constraints are enforced.
- [ ] Retrieval queries are covered by indexes.

### Conversation
- [ ] A user can hold a multi-turn conversation and the model receives prior turns.
- [ ] Context stays within the configured window under a long conversation.
- [ ] A user can start, list, and reset conversations.
- [ ] Deleting a conversation does not delete the user.

### LLM service
- [ ] The application layer depends on an abstraction, not on a provider SDK.
- [ ] Provider credentials, endpoint, and model are configuration.
- [ ] At least one provider is implemented behind the abstraction.
- [ ] A second provider or a documented extension point exists.
- [ ] Provider output is validated before use.

### UX
- [ ] `/help` lists available commands.
- [ ] Unknown commands receive a helpful response.
- [ ] Every message produces exactly one terminal response.
- [ ] Long responses are chunked correctly.
- [ ] Failures produce distinct, user-readable messages.

### Cost control
- [ ] Per-user throttling is enforced and tested.
- [ ] A global concurrency cap is enforced and tested.
- [ ] Token usage is recorded per request.
- [ ] A bounded fallback chain exists and is tested for both retryable and non-retryable errors.

### Security
- [ ] System instructions are never derived from user content.
- [ ] One user's conversation is never accessible by another user.
- [ ] No user content appears in logs without an explicit documented need.
- [ ] No SQL is built by string concatenation.

### Quality
- [ ] Telegram handlers and Celery tasks contain no business logic.
- [ ] Tests run without network access.
- [ ] Documentation is updated.
- [ ] No Phase 3–9 functionality was implemented.

---

## 11. Dependencies

### Prerequisites

**Phase 1 must be complete.** Specifically required:

| Phase 1 item | Why Phase 2 needs it |
|---|---|
| Service layer (P1-5) | Phase 2 logic belongs in services; without the layer it lands in handlers |
| Dependency injection (P1-6) | A fake provider is required for T-5 |
| Typed LLM validation (P1-7) | Phase 2 extends this contract |
| Error handling (P0-10) | Phase 2 adds new failure paths |
| Rate limiting foundation (P1-8) | Phase 2 extends it with allowances |
| Message length handling (P0-9) | Required before multi-turn responses |
| Test harness (P1-14) | Phase 2 tests depend on it |

### Infrastructure

- **PostgreSQL** becomes a real dependency in this phase. It is already declared in `docker-compose.yml:10-20` and currently unused.
- Redis, for the Celery broker, as established in Phase 1.

### Blocks

- **Phase 3** requires conversation persistence, the LLM service abstraction, and the data model. A research pipeline without them cannot store sources or attribute results.
- **Phase 4** requires the ingestion storage and retrieval patterns.
- **Phase 5** requires the entire schema. Designing the schema with workspaces in mind here avoids a retroactive migration.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Schema designed chat-only, then Phase 5 needs workspaces | Costly retroactive migration | Design for workspaces in this phase; document the reasoning |
| Conversation history inflates token cost and latency | Unbounded cost growth | Token budget, truncation strategy, per-user allowance, recorded usage |
| Stored user content becomes a persistent injection vector | Cross-turn privilege escalation | Structural separation of system and user content; T-7 is mandatory |
| A conversation-identifier collision leaks one user's history to another | Privacy breach | Server-generated identifiers, ownership enforced on every read, T-16 |
| Provider abstraction built too narrowly around one provider's response shape | Cannot swap providers | Model the domain type on the abstraction, not on the SDK object |
| Rate limiting tuned too aggressively | Legitimate users blocked | Configurable limits with an explanatory user response |
| Fallback models mask a persistent configuration failure | Repeated cost, no clear error | Bound fallback attempts; log the root failure distinctly |
| Message content in logs for debugging | User data exposure | Log identifiers and metadata; make content logging opt-in |
| Context truncation silently discards relevant history | Degraded answer quality | Make the strategy explicit and documented; surface truncation state to the model |
| Phase 1 incomplete, Phase 2 started anyway | Inherited instability compounds | Enforce the dependency table above; do not proceed on partial Phase 1 |

---

## 13. Definition of Done

Phase 2 is complete when:

1. Every acceptance criterion in § 10 is checked and evidenced.
2. Users, conversations, and messages persist, migrate cleanly, and are retrievable.
3. A user can hold a verified multi-turn conversation.
4. The application layer depends on an LLM abstraction, verified by a test that swaps the implementation.
5. `/help`, unknown-command handling, and user-visible error feedback work.
6. Rate limiting, concurrency capping, and token recording are enforced and tested.
7. A bounded, tested fallback chain exists.
8. Cross-user isolation and prompt-injection resistance are tested.
9. Documentation is updated, including the data model and provider abstraction.
10. No Phase 3–9 functionality was implemented.
11. Status is updated in this document and in `docs/PROJECT_PLAN.md`.

---

## 14. Status

**Current status: Complete, with environmental verification limitations.**

Every Phase 2 requirement is implemented and verified, every acceptance criterion
in § 10 is met, and every item in § 13 is satisfied. The remaining items are
external and are listed explicitly below rather than folded into the status.

**Phase status in `docs/PROJECT_PLAN.md`: Complete.**

### Why this status and not "Complete"

The distinction is drawn against § 13, the phase's own Definition of Done:

| § 13 item | Status |
|---|---|
| 1. Every acceptance criterion in § 10 checked and evidenced | Met — see the acceptance table below |
| 2. Users, conversations, messages persist, migrate cleanly, retrievable | Met — real PostgreSQL, real migrations, real end-to-end run |
| 3. A user can hold a verified multi-turn conversation | Met — verified against the live provider, with history in the request |
| 4. Application depends on an LLM abstraction, verified by a swapping test | Met — `test_the_service_layer_accepts_any_object_with_complete` |
| 5. `/help`, unknown-command handling, user-visible error feedback | Met |
| 6. Rate limiting, concurrency capping, token recording enforced and tested | Met |
| 7. A bounded, tested fallback chain | Met — both error classes |
| 8. Cross-user isolation and prompt-injection resistance tested | Met — SR-4 and the Phase 2 scope of SR-3 |
| 9. Documentation updated, including the data model and provider abstraction | Met |
| 10. No Phase 3–9 functionality implemented | Met |
| 11. Status updated in this document and in `PROJECT_PLAN.md` | Met |

**SR-3 remains Partial, and that is the specification's own boundary.** § 4
assigns the full prompt-injection defence system to Phase 3, because that is
where external content first enters a prompt. The guarantees Phase 2 owes are
implemented and tested. It is recorded as Partial rather than quietly upgraded to
Implemented.

### What is NOT verified, and why

| Item | State | Reason |
|---|---|---|
| Telegram message delivery | **NOT VERIFIED** | `api.telegram.org` refuses the connection from both the host and the container. Every send raised `TelegramNetworkError`, correctly contained. The delivery *logic* is tested with substituted transports; the real API was never contacted |
| GitHub Actions execution | **NOT VERIFIED** | The workflow is configured and every step was validated locally, but the `gh` CLI is not installed and the workflow has never run on GitHub |
| `docker build` with the committed `Dockerfile` verbatim | **NOT VERIFIED** | Docker Hub returns 403 for every manifest in this environment. A build differing only in the `FROM` line succeeded, so the instructions are sound |
| Phase 1 credential rotation (P0-3 / SR-4) | **Open — human action** | Requires @BotFather and the provider dashboard. **Now sharper:** the key in `.env` is confirmed **live and working**, so it is a real disclosure risk, not a theoretical one. This is a Phase 1 item and is not part of this phase's Definition of Done |

None of the four is a missing Phase 2 implementation. None can be closed from this
repository: each needs a network path, a GitHub account, or a human with dashboard
access.

### What changed

```text
BEFORE (18 modules)                     AFTER (27 modules)
─────────────────────                   ─────────────────────
                                       app/domain/chat.py      198  roles, records, outcome
                                       app/domain/ports.py     178  repository + LLM ports
app/core/config.py     140              app/db/base.py           33  naming convention
                                         app/db/models.py        210  users/conversations/messages
app/services/                               app/db/session.py       180  engine + session + test guard
  research.py          143  REMOVED       app/db/unit_of_work.py   74  one transaction per operation
  formatting.py        110               app/db/migrations/       —   Alembic env + 1 revision
  delivery.py          104               app/repositories/        —   users, conversations, messages
  rate_limit.py        116  + daily       app/services/chat.py    420  chat orchestration
  health.py              90  + db probe    app/services/context.py 186  order, budget, truncation
                                         app/services/prompts.py  78  versioned system prompt
                                         app/services/failures.py  84  typed error → user message
app/infrastructure/
  llm.py               175  + complete()  app/infrastructure/llm.py 380  chat, usage, fallback, factory
```

Total: 18 → 27 modules, ~1,700 → ~3,400 statements. 123 → **329 tests**.

### Requirement matrix

Every row is evidenced by a named test. "Implemented" means the behaviour exists
and is verified; the test id is the evidence.

#### Conversation persistence

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-1 | A user record exists for every Telegram user, keyed on the platform id | Implemented | `test_database_layer.py::test_a_user_record_is_created_on_first_interaction`, `::test_a_second_interaction_reuses_the_same_user` |
| FR-2 | Every inbound user message is persisted with its chat, sender, timestamp, content | Implemented | `test_chat_service.py::test_both_messages_of_a_turn_are_persisted`, `test_database_layer.py::test_messages_are_returned_in_chronological_order` |
| FR-3 | Every outbound bot message persisted, **or the decision not to recorded** | Implemented (decision recorded) | Assistant replies are persisted. Command replies, acknowledgements, and failure notices are **not**: they are not conversation content and persisting them would pollute the model's history. Recorded here and in `app/services/chat.py` |
| FR-4 | A conversation groups messages; multiple conversations per user | Implemented | `test_database_layer.py::test_a_user_may_have_many_conversations`, `test_chat_service.py::test_the_same_user_keeps_one_conversation_across_messages` |
| FR-5 | Start, continue, list, and reset conversations | Implemented | `test_telegram_chat_flow.py::test_new_starts_a_conversation_and_does_not_enqueue`, `::test_conversations_lists_them`, `::test_reset_confirm_performs_the_reset` |
| FR-6 | Deleting or resetting does not delete the user | Implemented | `test_database_layer.py::test_reset_deletes_messages_but_keeps_the_conversation_and_user` |

#### Context management

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-7 | Prior messages retrieved in chronological order | Implemented | `test_database_layer.py::test_messages_are_returned_in_chronological_order` (T-3), `test_context_policy.py::test_history_is_not_reordered` |
| FR-8 | Assembled context fits the configured window | Implemented | `test_context_policy.py::test_context_exceeding_the_message_cap_drops_the_oldest`, `::test_context_is_trimmed_further_when_the_token_budget_binds` (T-4) |
| FR-9 | A defined strategy applies; it is configuration, not a constant | Implemented | `test_context_policy.py::test_the_message_cap_is_configuration_not_a_constant`; strategy recorded as AD-022 |
| FR-10 | A per-message token estimate is enforced before the provider call | Implemented | `test_context_policy.py::test_an_oversized_single_message_is_still_sent`, `::test_estimator_counts_per_message_overhead` |
| FR-11 | The system prompt is separate from user content, never built by concatenation | Implemented | `test_context_policy.py::test_the_system_prompt_takes_no_user_content`, `::test_user_content_never_appears_in_a_system_message` (T-7) |

#### LLM service

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-12 | The application depends on an abstraction, not a provider SDK | Implemented | `test_llm_provider_and_fallback.py::test_the_shipped_provider_satisfies_the_port_protocol` (T-5) |
| FR-13 | Credentials, endpoint, and model are configuration | Implemented | `tests/test_config_and_security.py::test_sr1_no_secret_literal_in_application_source` |
| FR-14 | At least one provider behind the abstraction | Implemented | `app/infrastructure/llm.py::OpenAICompatibleClient`, configured for Groq |
| FR-15 | Provider responses returned as a validated domain type | Implemented | `test_llm_provider_and_fallback.py::test_complete_returns_a_validated_domain_object`, `::test_an_empty_response_is_rejected` |
| FR-16 | A second provider or a documented extension point | Implemented | `app/infrastructure/llm.py::create_llm_client`; `test_llm_provider_and_fallback.py::test_the_service_layer_accepts_any_object_with_complete` (T-6), `::test_an_unknown_provider_is_rejected_with_a_readable_error` |

#### Commands and UX

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-17 | `/help` lists commands with a description | Implemented | `test_telegram_chat_flow.py::test_help_lists_the_available_commands` (T-8) |
| FR-18 | Unknown commands get a helpful response | Implemented | `test_telegram_chat_flow.py::test_an_unknown_command_is_answered_not_treated_as_text` (T-8) |
| FR-19 | Command arguments are validated before use | Implemented | `test_telegram_chat_flow.py::test_reset_requires_explicit_confirmation`; `/reset` ignores any argument other than `confirm` |
| FR-20 | Destructive commands require explicit confirmation | Implemented | `test_telegram_chat_flow.py::test_reset_requires_explicit_confirmation` |

#### Reliability

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-21 | Exactly one terminal response per message | Implemented | `test_telegram_chat_flow.py::test_a_text_message_is_acknowledged_and_enqueued`; `test_celery_chat_task.py::test_a_turn_produces_exactly_one_reply` (T-11) |
| FR-22 | A provider failure gives a distinct, non-revealing message | Implemented | `test_telegram_chat_flow.py::test_user_messages_expose_no_infrastructure_detail`, `::test_every_domain_failure_has_its_own_message` (T-9) |
| FR-23 | A persistence failure gives a distinct, user-readable message | Implemented | `test_telegram_chat_flow.py::test_a_command_failure_produces_a_readable_message` (T-10) |
| FR-24 | Responses respect Telegram's length limit and are chunked | Implemented (Phase 1, retained) | `tests/test_telegram_output_safety.py`; `app/services/formatting.py::split_for_telegram` |
| FR-25 | Long responses arrive in multiple messages without duplication or loss | Implemented (Phase 1, retained) | `tests/test_telegram_output_safety.py` |

#### Rate limiting and cost

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-26 | Per-user throttling with a configurable limit and window | Implemented (Phase 1, retained) | `tests/test_telegram_routing.py::test_user_rate_limit_is_enforced_after_allowance` |
| FR-27 | A global concurrency cap | Implemented (Phase 1, retained) | `tests/test_telegram_routing.py::test_global_capacity_is_enforced_and_explained` (T-13) |
| FR-28 | A per-user daily request allowance | Implemented | `test_telegram_chat_flow.py::test_the_daily_allowance_is_enforced_and_explained`; AD-025 |
| FR-29 | A throttled request is told when it may retry | Implemented | `test_telegram_chat_flow.py::test_throttling_states_when_the_user_may_retry` (T-12) |
| FR-30 | Token usage recorded per request | Implemented | `test_database_layer.py::test_token_usage_is_recorded_per_message`, `test_llm_provider_and_fallback.py::test_token_usage_is_returned_for_cost_tracking` (T-20) |

#### Model strategy

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| FR-31 | The model is configurable | Implemented | `LLM_MODEL` in settings; `test_context_policy.py` uses it per test |
| FR-32 | A retryable failure advances a fallback chain | Implemented | `test_llm_provider_and_fallback.py::test_a_retryable_failure_falls_back_to_the_next_model` (T-14) |
| FR-33 | Fallback is bounded | Implemented | `test_llm_provider_and_fallback.py::test_the_chain_never_exceeds_the_configured_attempt_bound` |
| FR-34 | Non-retryable errors do not trigger fallback | Implemented | `test_llm_provider_and_fallback.py::test_a_non_retryable_failure_does_not_fall_back`, `::test_a_malformed_response_does_not_fall_back` (T-15) |

#### Security

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| SR-1 | No secret in source | Implemented | `test_config_and_security.py::test_sr1_no_secret_literal_in_application_source`; CI secret-scan step |
| SR-2 | User content is untrusted, never interpolated into a system instruction | Implemented | `test_context_policy.py::test_user_content_never_appears_in_a_system_message`, `::test_stored_content_is_not_promoted_to_a_system_instruction` |
| SR-3 | Stored user content must not escalate its own privilege across turns | **Partial** | Structurally addressed: `build_system_prompt()` takes no parameters, so stored content has no path to the instructions — `test_context_policy.py::test_the_system_prompt_takes_no_user_content`, `::test_stored_content_is_not_promoted_to_a_system_instruction`, `::test_user_content_never_appears_in_a_system_message`. The full defence system belongs with the research pipeline in Phase 3, where external content first enters a prompt |
| SR-4 | One user's conversation is never returned to another | Implemented | `test_database_layer.py::test_a_conversation_is_not_returned_to_another_user`, `test_chat_integration.py::test_one_user_cannot_reach_another_users_conversation` (T-16) |
| SR-5 | Identifiers and metadata are logged, not content | Implemented | `test_chat_service.py::test_no_message_content_is_written_to_the_log` |
| SR-6 | Persisted content is untrusted on read | Implemented | `test_context_policy.py::test_stored_content_is_not_promoted_to_a_system_instruction` |
| SR-7 | Rate limits are enforced server-side | Implemented | `app/services/rate_limit.py`, driven only by the server-observed user id |
| SR-8 | Token accounting is derived server-side | Implemented | `test_llm_provider_and_fallback.py::test_token_usage_is_returned_for_cost_tracking`; nothing accepts a client-supplied count |
| SR-9 | A user can delete their data, and deletion actually removes it | **Implemented** | `/delete_account confirm`. Service: `tests/test_data_deletion.py::test_deletion_removes_the_user_and_everything_reachable`, `::test_a_deleted_identity_starts_clean_rather_than_resuming`. Cascade verified against real PostgreSQL: `::test_the_cascade_removes_the_whole_tree`, `::test_the_cascade_leaves_another_user_untouched`, `::test_the_remaining_conversation_belongs_to_the_surviving_user`. Transactional: `::test_a_failure_during_deletion_rolls_the_whole_thing_back`. Command surface: `tests/test_delete_account_command.py` (11 tests) |
| SR-10 | No SQL built by string concatenation | Implemented | `test_telegram_chat_flow.py::test_no_module_builds_sql_by_string_concatenation`, `::test_the_data_layer_is_the_only_place_sqlalchemy_is_imported` |

**One security requirement is deliberately Partial.**

- **SR-3 is Partial, by the specification's own boundary.** Cross-turn
  prompt-injection resistance is addressed structurally — the system prompt is
  unreachable from user content because the function that returns it takes no
  parameters — but this document assigns the full defence system to Phase 3,
  where external content first enters a prompt. The structural guarantees this
  phase owes are in place and tested: the system prompt is independent of user
  content, stored content is not promoted to an instruction, and history is
  passed as user/assistant messages rather than as privileged instructions. No
  Phase 3 work was done to close the remainder.

SR-9 is now implemented; see its row above and § 14 for the command, the
confirmation mechanism, and the transactional evidence.

#### Data layer

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| M-1 | Migrations managed by a declared tool | Implemented | `app/db/migrations/`, `alembic==1.14.0` in `requirements.txt`; AD-020 |
| M-2 | `DATABASE_URL` wired into a real session factory | Implemented | `app/db/session.py::Database`; now a required setting |
| M-3 | Indexes on every foreign key and retrieval column | Implemented | `test_database_layer.py::test_every_declared_index_exists_in_the_database`, `::test_message_history_retrieval_uses_the_declared_index` |
| M-4 | Uniqueness on the platform user identifier | Implemented | `test_database_layer.py::test_duplicate_telegram_identity_is_impossible` |
| M-5 | All schema changes via migrations | Implemented | `test_migrations_and_isolation.py::test_the_models_and_the_migrations_agree` (`alembic check`); no `create_all` anywhere |
| M-6 | The schema anticipates Phase 5 workspaces | Implemented | `conversations.project_id` reserved; AD-015 |

#### Tests

| ID | Test | Status |
|---|---|---|
| T-1 | A user record is created once and reused | Implemented |
| T-2 | A second message is persisted and linked | Implemented |
| T-3 | History in correct chronological order | Implemented |
| T-4 | Context truncated when it exceeds the window | Implemented |
| T-5 | The service layer works against a fake provider, no network | Implemented |
| T-6 | Swapping the provider needs no service change | Implemented |
| T-7 | System instructions never built from user content | Implemented |
| T-8 | `/help` lists commands; unknown command is answered | Implemented |
| T-9 | Provider failure yields a readable message, no internal detail | Implemented |
| T-10 | Persistence failure yields a readable message | Implemented |
| T-11 | Exactly one terminal response per message | Implemented |
| T-12 | Throttling rejects past the threshold and explains the retry time | Implemented |
| T-13 | The concurrency cap holds | Implemented |
| T-14 | A retryable failure triggers fallback | Implemented |
| T-15 | A non-retryable failure does not trigger fallback | Implemented |
| T-16 | One user's conversation is never returned for another | Implemented |
| T-17 | Long responses are chunked without loss | Implemented (Phase 1) |
| T-18 | Migrations apply to an empty database and roll back | Implemented — `test_migrations_and_isolation.py::test_migrations_round_trip_on_a_dedicated_scratch_database` |
| T-19 | Foreign keys and uniqueness constraints are enforced | Implemented |
| T-20 | Token usage is recorded per request | Implemented |

### Acceptance criteria (§ 10)

| Criterion | Status |
|---|---|
| User, conversation, and message records exist and are created automatically | Met |
| Migrations apply and roll back cleanly on an empty database | Met — validated on a throwaway database, empty → head → base → head |
| Foreign keys and uniqueness constraints are enforced | Met |
| Retrieval queries are covered by indexes | Met — asserted against `pg_indexes`, and `EXPLAIN` is checked for an index scan |
| A user can hold a multi-turn conversation and the model receives prior turns | Met — `test_chat_integration.py::test_a_second_message_continues_the_same_conversation` |
| Context stays within the configured window under a long conversation | Met — `test_chat_service.py::test_history_is_capped_so_context_cannot_grow_without_bound` |
| A user can start, list, and reset conversations | Met |
| Deleting a conversation does not delete the user | Met — `test_database_layer.py::test_reset_deletes_messages_but_keeps_the_conversation_and_user`; `/reset` and `/delete_account` are separate commands, asserted in `test_delete_account_command.py::test_reset_does_not_delete_the_account` |
| A user can delete their data, and it is really removed (SR-9) | Met — `/delete_account confirm`; 25 tests across `test_data_deletion.py` and `test_delete_account_command.py`, including the cascade and the transactional rollback against real PostgreSQL |
| The application layer depends on an abstraction, not a provider SDK | Met |
| Provider credentials, endpoint, and model are configuration | Met |
| At least one provider behind the abstraction | Met |
| A second provider or a documented extension point | Met |
| Provider output is validated before use | Met |
| `/help` lists available commands | Met |
| Unknown commands receive a helpful response | Met |
| Every message produces exactly one terminal response | Met |
| Long responses are chunked correctly | Met (Phase 1) |
| Failures produce distinct, user-readable messages | Met |
| Per-user throttling is enforced and tested | Met |
| A global concurrency cap is enforced and tested | Met |
| Token usage is recorded per request | Met |
| A bounded fallback chain, tested for both error classes | Met |
| System instructions are never derived from user content | Met — SR-3's Phase 2 scope. The remainder is Partial by design and assigned to Phase 3 |
| One user's conversation is never accessible by another | Met |
| No user content appears in logs without a documented need | Met |
| No SQL is built by string concatenation | Met |
| Telegram handlers and Celery tasks contain no business logic | Met — enforced structurally; `test_telegram_chat_flow.py`, `test_celery_chat_task.py::test_the_task_contains_no_business_logic` |
| Tests run without network access | Met — the autouse socket guard fails any non-loopback connection. **Verified:** 368 tests pass with no egress, including 30 against a real database over loopback |
| Documentation is updated | Met |
| No Phase 3–9 functionality was implemented | Met — see "Not implemented" below |

### The provider 403: what it actually was, and the correction

An earlier revision of this document reported that the configured Groq endpoint
returned **HTTP 403** and suggested it was "very likely the Phase 1
credential-rotation blocker surfacing". **That diagnosis was wrong.** It is
corrected here rather than quietly replaced, because the wrong conclusion was
recorded and someone may have relied on it.

**What was measured.** The same endpoint, model, and request shape, with three
different credential states, run from the host:

| Credential presented | Result |
|---|---|
| The configured key | `403 {"error":{"message":"Forbidden"}}` |
| A syntactically valid but fake `gsk_` key | `403 {"error":{"message":"Forbidden"}}` |
| **No `Authorization` header at all** | `403 {"error":{"message":"Forbidden"}}` |

An unauthenticated request returning the same 403 as an authenticated one means
the response carries **no information about the credential**. A rejected key
would be a 401. The same host shows `pypi.org`, `github.com`, and `example.com`
returning 200, `registry-1.docker.io` returning `403 RBAC: access denied`, and
`api.telegram.org` refusing the connection. That is an **egress allowlist**, and
the 403 was the proxy refusing the destination — not the application, not the
credential, and not the provider.

**What happens from inside a container**, which takes a different network path:

| Request | Result |
|---|---|
| `GET /openai/v1/models`, no auth | `401 {"error":{"message":"Invalid API Key"}}` — a genuine Groq error |
| `GET /openai/v1/models`, configured key | **`200`, 11 models listed** |
| `POST /chat/completions`, `llama-3.3-70b-versatile` | **`404 model_not_found`** — "does not exist or you do not have access to it" |

**Conclusion.** The API key is **valid**. The 404 rather than a 401 is the
proof: the key authenticated and the request was refused afterwards. The
configured model, `llama-3.3-70b-versatile`, has been **retired by the provider**
and is absent from the 11 models the key can see. This was a configuration defect
in the repository, not a credential problem and not a Phase 1 blocker.

**The fix.** `LLM_MODEL` now names a model the provider serves.
`qwen/qwen3.8-27b` was chosen from the available list after measuring all three
general-purpose candidates: `openai/gpt-oss-120b` and `openai/gpt-oss-20b`
returned HTTP 200 but with **empty content** and `finish_reason=length`, because
they spend the output budget on reasoning tokens before answering — a poor fit
for a chat bot that must produce visible text within `LLM_MAX_TOKENS`. The
`whisper-*` and `*-prompt-guard-*` entries are not chat models. See `AD-028`.

**What this changes about the Phase 1 blocker.** Credential rotation (Phase 1
P0-3 / SR-4) remains open, and now with a sharper reason: the key in `.env` is
not merely *exposed*, it is **live and working**, so it is a genuine
disclosure risk rather than a theoretical one. It still requires a human with
dashboard access. It is a Phase 1 item and is not part of this phase's
Definition of Done; it is called out in § 14 as an open action, not as a Phase 2
defect.

### Not implemented, deliberately

- **Web search, URL fetching, extraction, ranking, citations, reports.** Phase 3. No such module, import, or configuration exists.
- **Document or file handling.** Phase 4.
- **Projects, workspaces, tags, bookmarks.** Phase 5. Only the reserved `conversations.project_id` column exists (AD-015); no `projects` table and no behaviour.
- **Multi-agent orchestration.** Phase 6. `app/agents/supervisor.py` is unchanged and still unused by the request path.
- **Streaming.** Not required by this phase; long responses are chunked instead.
- **Conversation summarization and semantic memory.** Explicitly excluded by § 4 and by the task's § 24. Truncation only (AD-022).
- **Telegram webhook mode.** § 4 excludes it; long polling remains.
- **A distributed rate limiter.** AD-012 stands; the counters are in-process.

### Defects found during implementation

Recorded because they are real findings, not decoration.

| ID | Defect | Resolution |
|---|---|---|
| P2-1 | **A repository savepoint was silently unreliable.** Rolling a `SAVEPOINT` back while its parent transaction had issued no SQL still left the SQLAlchemy `Session` in a pending-rollback state, so the caller's commit failed with `PendingRollbackError`. Found by the end-to-end test, not by unit tests. | Measured against SQLAlchemy 2.0.36 + asyncpg. A uniqueness violation now aborts the enclosing unit of work and the caller resolves it. AD-019 |
| P2-2 | **Message-cap truncation was invisible.** Dropping messages to satisfy `LLM_CONTEXT_MAX_MESSAGES` did not set the truncated flag, so the model was never told history was incomplete. Found by `test_truncation_is_surfaced_to_the_model`. | Both the count cap and the token budget now set `truncated` and emit the notice. `app/services/context.py` |
| P2-3 | **The system prompt varied with history size.** It appended a message count, so its bytes were not constant and T-7's invariant did not hold literally. | The count was removed. The prompt is now a constant, versioned and checksummed (AD-023) |
| P2-4 | **The test helper leaked locks and hung the suite.** An unclosed `AsyncSession` holds an `ACCESS SHARE` lock, so the next test's `TRUNCATE` blocked forever. | Sessions are closed, and the truncate sets `lock_timeout` so a future leak fails loudly instead of hanging |
| P2-5 | **The migration environment skipped its own guard.** `ALEMBIC_REQUIRE_TEST_DB` did not exist, so the name check only ran when the URL already looked like a test database — a misspelled test database would have migrated a real one. | Replaced the inference with an explicit switch (AD-021) |
| P2-9 | **Logging silently lost listeners, in two independent ways.** (a) `configure_logging()` called `root.handlers.clear()`, destroying any handler another component had attached. (b) Alembic's `fileConfig` defaults to `disable_existing_loggers=True`, and its config names no `app.*` logger — so running a migration muted the whole application logger tree. Both failed *silently*: records still reached the console, so nothing looked broken. Surfaced because a Phase 2 security assertion (SR-5, no message content in logs) passed or failed depending on which test module was imported first. A control whose result depends on import order is not a control. | (a) `configure_logging` now adds its handler without clearing others; the `_CONFIGURED` guard already provided FR-9.1, so nothing was lost. (b) `migrations/env.py` passes `disable_existing_loggers=False`. Two regression tests added. Verified order-independent in both directions and across repeat runs |
| P2-7 | **Every secret scanner in the repository was blind to Groq keys.** The patterns used `gsk-` with a HYPHEN; Groq issues `gsk_` with an UNDERSCORE, so the alternative matched nothing. The repository scan **passed while a realistic `gsk_` key sat in `app/`**, and the CI scan would have passed too. Found by testing the control rather than trusting it. | Corrected in all three scanners (the shared pattern, the repository-wide scan, and the CI workflow). Added `test_the_secret_scanner_matches_realistic_key_shapes` with positive and negative controls, plus a check that the structural test's own pattern has the same coverage. Re-verified by injecting a leak: now detected |
| P2-8 | **The configured model had been retired by the provider.** `LLM_MODEL=llama-3.3-70b-versatile` returns `404 model_not_found`. Combined with an egress proxy that masked it behind a uniform 403, this looked like a credential failure. | Model corrected to `qwen/qwen3.8-27b` after measuring the candidates. See the 403 section above and `AD-028` |
| P2-6 | **Two Phase 1 tests reached a real broker.** `test_global_capacity_is_enforced_and_explained` and `test_user_rate_limit_is_enforced_after_allowance` did not stub the enqueue, so they failed whenever Redis was not running. | They now use the existing `enqueued` fixture. They were failing before this phase; the cause was a test defect, not a product defect, and it is fixed rather than suppressed |

### Consistency trade-offs, stated rather than implied

- **A turn is two transactions, not one** (AD-018). The consequence is that a
  half-turn — a stored question with no reply — is a legitimate state. The retry
  path detects it and answers it. The alternative, one transaction, would lose
  the user's text on a provider outage, which § 15 forbids.
- **This is not a distributed transaction.** The Telegram send happens outside
  the database transaction. If delivery fails after a successful commit, the
  reply is stored and the user did not receive it; a redelivery re-sends the
  stored reply rather than generating a new one. The worst case is a stored
  answer the user did not see, which their next message reveals.
- **Idempotency covers one turn, not a whole conversation.** A redelivered
  update produces no second reply. Two genuinely distinct messages with the same
  content are two turns, because the key is the platform message id.
- **The provider call is synchronous inside an async service.** The chat service
  is async because persistence is; the LLM client is the synchronous OpenAI SDK
  that Phase 1 established and this phase was told to reuse. In the Celery
  worker this blocks the process loop for the duration of the call, which is
  acceptable at `--concurrency=1` and equivalent to the pre-existing
  `DeliveryService` pattern. It would need revisiting before the worker scales
  concurrency within a process.
- **Token counting is an estimate.** A character-ratio heuristic behind
  `TokenEstimator`, deterministic and dependency-free, slightly conservative. A
  real tokenizer replaces it without touching a caller. SR-8 holds because the
  *recorded* usage comes from the provider, not from this estimate.

### Validation actually performed

| Check | Command | Result |
|---|---|---|
| Test suite | `pytest -q` | **368 passed**, 0 failed |
| Lint | `ruff check app tests` | All checks passed |
| Types | `mypy app` | Success, 44 source files |
| Canonical gate | `make verify` | lint + typecheck + test, all pass |
| Migration/model agreement | `alembic check` (dev and test databases) | No new upgrade operations detected, on both |
| Migration round trip | `alembic downgrade base && alembic upgrade head` on the test database | base → head → base → head, all clean |
| Migration round trip, isolated | `pytest tests/test_migrations_and_isolation.py` | 16 passed. Throwaway database: empty → head → base → head, tables and indexes verified at each step |
| T-1 regression gate | `pytest tests/test_t1_event_loop_regression.py tests/test_t1_task_boundary.py` | 15 passed |
| Compose validity | `docker compose config` | Valid |
| **Docker image build** | `docker build` | **Succeeded.** The committed `Dockerfile` could not be built verbatim because this environment cannot reach Docker Hub (403 on every manifest). A throwaway copy differing **only** in its `FROM` line built and ran; the committed file was not modified. See "Docker verification" below |
| **Migrations from inside the image** | `docker run … alembic upgrade head` | `0001_phase2 (head)`; all three tables created in a real container |
| **API process in a container** | `docker run … uvicorn app.main:app` | Started. `GET /health` → **200**. `GET /ready` → **503 degraded**, `{"telegram_poller":"stopped","broker":"ok","database":"ok"}` — the Phase 2 database probe verified against real PostgreSQL, and the poller honestly reported dead |
| **Celery worker in a container** | `docker run … celery worker` | `ready`, `process_research` registered, per-process event loop initialised, connected to Redis |
| **Real provider, through the application** | `OpenAICompatibleClient.complete` against Groq | **Success.** Returned a validated `LLMReply` with real text and real token usage (37 prompt / 33 completion) |
| **Real end-to-end, in containers** | two turns enqueued to a real worker | **Success up to the Telegram hop.** See below |
| **Telegram delivery** | real `sendMessage` | **NOT VERIFIED — external.** `api.telegram.org` refuses the connection from both the host and the container. Every send raised `TelegramNetworkError`, which the application contained correctly |
| **GitHub Actions** | `.github/workflows/ci.yml` | **Configured and locally validated; GitHub-hosted execution not verified in this environment.** The `gh` CLI is not installed and the workflow has not run |
| **Secret scanning** | the scan, with an injected `gsk_` leak | Verified as a **positive and negative** control: it now detects a realistic Groq key and reports the real tree clean |

### The real end-to-end run

Performed in containers against real PostgreSQL, real Redis, and the live
provider, using the committed image. Two turns were enqueued for one user
exactly as the Telegram handler does.

```text
turn 1  user      "What is entropy?"
        llm       qwen/qwen3.8-27b  tokens 206 prompt / 127 completion
        assistant "Entropy is a measure of disorder or randomness in a system…"

turn 2  user      "Why does it matter?"
        llm       history=2  tokens 351 prompt / 197 completion
        assistant "Entropy matters because it dictates the direction of time…"
```

| Requirement | Evidence |
|---|---|
| User message persisted | `messages` rows 1 and 3, `role='user'` |
| Correct conversation selected | both turns in conversation 1; one `users` row, one `conversations` row |
| Prior history included | turn 2's request reported `history=2`; its answer refers to the first turn |
| Provider returns successfully | HTTP 200, two successful completions |
| Response passes domain validation | both replies stored as validated assistant messages, not raw payloads |
| Assistant message persisted | `messages` rows 2 and 4 |
| Token usage recorded | `prompt_tokens`/`completion_tokens` populated per assistant message; `model` recorded |
| Exactly one assistant reply per turn | `GROUP BY turn_id` → 2 turns, 1 reply each, **despite a Celery retry** |
| No duplicate response | the retry logged `duplicate turn rejected by the database: DuplicateMessageError` and wrote nothing |
| Telegram delivery | **NOT VERIFIED** — egress blocked, `TelegramNetworkError` |

The delivery failure also demonstrated the retry policy working: `DeliveryError`
is retryable, so the task rescheduled rather than messaging the user on an
attempt that would be retried, and the redelivery did not create a second reply.

### Docker verification, in full

The committed `Dockerfile` and `docker-compose.yml` are unmodified by this
verification. The registry block is environmental:

| Step | Outcome |
|---|---|
| `docker build` with the committed file | **Failed** — `# syntax=docker/dockerfile:1` and `python:3.12-slim` both 403 from Docker Hub |
| `docker build` with a locally available base image, `# syntax` removed | **Succeeded** |
| Image run as API, worker, and migration tool | All three ran correctly |
| `docker compose up -d redis postgres` | `redis` healthy; `postgres` could not bind `127.0.0.1:5432` because the host's own PostgreSQL already holds that port |

`postgres:16-alpine` and a Python base image were present in the local image
store, which is why a build was possible at all. Two host conflicts were worked
around **without editing any configuration**: a separate docker network, and a
port already in use.

### Known limitations, recorded deliberately

- **Rate limiting and the daily allowance are in-process.** Correct for a single
  API process; the counters stop being global if the API tier scales
  horizontally. AD-012 and AD-025.
- **The daily allowance counts requests, not tokens.** FR-28 is satisfied by a
  request count. A token-weighted allowance would need the recorded usage summed
  per user per day, which is available (`total_tokens_for_user`) but is not
  wired, because FR-28 permits either.
- **`conversations.project_id` has no foreign key and no behaviour.** It is a
  reserved column (AD-015). Nothing reads or writes it.
- **The Celery task is named `process_research` and lives in `research_task.py`**,
  although it runs the chat flow. Inherited from Phase 1 under the no-rename
  rule; recorded as AD-027.
- **`app/agents/supervisor.py` and `analyze_query` are retained but unused** by
  the request path. Phase 3 needs the decomposition capability, so removing it
  now would delete a capability this phase is not entitled to replace (AD-026).
- **User-facing strings are hardcoded English literals** (audit L-2). Unchanged
  from Phase 1; i18n remains Phase 7.
- **No conversation export or deletion API.** SR-9 unmet, as above.

### Update procedure

When an item is completed:

1. Set its status in the tables above.
2. Tick the corresponding acceptance criterion in § 10.
3. Update the phase status table in `docs/PROJECT_PLAN.md`.
4. Record any decision in the Architecture Decision Log.
5. If `docs/ARCHITECTURE.md` is updated, keep its audit findings intact as the
   pre-Phase-1 baseline and add a dated note rather than rewriting them.
