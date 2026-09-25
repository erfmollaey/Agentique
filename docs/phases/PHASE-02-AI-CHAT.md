# Phase 2 — AI Chat MVP

**Status:** Not Started
**Blocked by:** Phase 1 — Core Infrastructure & Stabilization
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md)

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

**Current status: Not Started**

**Phase status in `docs/PROJECT_PLAN.md`: Not Started.**

**Blocked by:** Phase 1 — Core Infrastructure & Stabilization.

No Phase 2 work has been implemented. The repository has no database code, no conversation model, and no service layer. The only current behavior is a single stateless LLM call per message, as described in § 2.
