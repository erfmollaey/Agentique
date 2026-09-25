# Phase 8 — SaaS / Web Platform

**Status:** Not Started
**Blocked by:** Phase 2 (data layer), Phase 5 (workspace model), Phase 7 (reports and exports)
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md)

> **Nothing in this phase is implemented.** The repository has no authentication, no user accounts as a platform concept, no multi-tenancy, no REST API beyond a single static health route, and no frontend.

---

## 1. Objective

Extend the product beyond Telegram into a full web platform: a Next.js dashboard with authentication, a research workspace UI, research history, and an API surface for programmatic access.

Telegram remains the primary conversational interface. The web platform adds depth, organization, and integration — the things a chat interface is structurally poor at.

---

## 2. Current State

### Verified absences

| Capability | Status | Evidence |
|---|---|---|
| Frontend | Not Implemented | No frontend files, no build configuration |
| Authentication | Not Implemented | No auth code, no auth dependency, no session handling |
| User accounts as a platform concept | Not Implemented | No account model; a Telegram user record is not an account |
| Research workspace UI | Not Implemented | No interface of any kind |
| Research history UI | Not Implemented | No interface |
| REST API | Not Implemented | One static `GET /health` route at `app/main.py:11-13` |
| API access / keys | Not Implemented | No key model, no key issuance |
| Usage limits | Partially planned | Per-user rate limiting is Phase 1–2 work; no usage metering for billing |
| Billing integration | Not Implemented | No payment code — that is Phase 9 |
| Team / workspace collaboration | Not Implemented | No tenancy model, no roles, no sharing |
| Discord / Slack integrations | Not Implemented | No integration code |

### What the platform does have

- A FastAPI application, currently 24 lines, serving one static route.
- A Telegram-only identity model: `message.from_user.id` is used directly, with no account, no ownership, and no persistence (audit § 2, § 6 H-1).
- PostgreSQL declared but unused (audit M-2).

### The identity gap

This is the phase's most consequential starting condition. The current system treats a Telegram user ID as a user identifier with no underlying account. There is:

- No account record independent of Telegram.
- No credential, so no way to authenticate a browser session or an API caller.
- No way to link a Telegram identity to an account created on the web, or the reverse.
- No tenancy, so no notion of an organization, a team, or a shared workspace.

Phase 2 introduces user records. This phase must build the **account and identity layer on top of them**, and must resolve the Telegram-to-account linking problem explicitly. It cannot assume Phase 2 anticipated it — that is worth verifying during Phase 2 and recording if it was not.

---

## 3. Scope

- A Next.js dashboard.
- Authentication and session management.
- User accounts independent of Telegram, with Telegram identity linked.
- A research workspace UI over the Phase 5 data model.
- Research history browsing, search, and filtering.
- A proper REST API over research, sources, documents, projects, and reports.
- API key issuance and management for programmatic access.
- Usage metering and limit display.
- Team and organization workspaces with roles.
- Third-party integrations such as Discord and Slack, where appropriate.

---

## 4. Out of Scope

- **New research, document, or agent capability.** Phases 3, 4, and 6.
- **New workspace data model.** Phase 5. This phase builds the interface over it.
- **New premium capability.** Phase 7. This phase surfaces it.
- **Payment processing, subscription enforcement, invoicing, entitlements, pricing.** Phase 9. This phase may display usage and limits; it does not charge.
- **Changing the Telegram bot's core behavior.** The bot and the platform share a domain layer, not logic.
- **A native mobile application.**
- **Fixing Phase 1–7 defects.** Report them; do not absorb them.

---

## 5. Functional Requirements

### Accounts and identity
- FR-1 A user can create an account with an email address and a password or a federated identity.
- FR-2 An account exists independently of any chat platform.
- FR-3 A Telegram identity can be linked to an account, and unlinking is possible.
- FR-4 A user who starts in Telegram and later registers on the web sees their existing research under the same account.
- FR-5 A user who registers on the web can connect Telegram and continue the same conversation history.
- FR-6 Account linking requires proof of ownership of both identities. Linking is never requested by the user alone.
- FR-7 An account can be deleted, along with all associated data.

### Authentication and sessions
- FR-8 Passwords are stored with a modern adaptive hash. Plaintext storage is unacceptable.
- FR-9 Sessions are secure, httpOnly, and support revocation.
- FR-10 Session expiry and refresh follow a defined policy.
- FR-11 Multi-factor authentication is available or formally deferred with a recorded decision.
- FR-12 Authentication failures do not disclose whether an account exists.
- FR-13 Authentication state is enforced server-side on every protected resource. A hidden UI control is not an access control.

### Workspace UI
- FR-14 A user can view, create, rename, archive, and delete projects.
- FR-15 A user can view research runs within a project.
- FR-16 A user can browse full research history with search and filtering.
- FR-17 A user can view sources with their content, metadata, and provenance.
- FR-18 A user can upload and manage documents.
- FR-19 A user can view reports and download exports.
- FR-20 A user can view job status for long-running research.
- FR-21 The UI reflects the same data model as the Telegram interface — no parallel store.

### REST API
- FR-22 A documented REST API exposes research, sources, documents, projects, reports, and usage.
- FR-23 Every endpoint is authenticated and authorized.
- FR-24 Every endpoint is user-scoped. Cross-user access is impossible.
- FR-25 The API is versioned.
- FR-26 The API is documented — schema or equivalent, generated from the implementation so it cannot drift.
- FR-27 Errors follow a consistent, documented format.
- FR-28 Rate limits are applied per credential and surfaced to the caller.

### API keys
- FR-29 A user can create, name, scope, and revoke API keys.
- FR-30 A key's secret is shown exactly once at creation and is never retrievable afterward.
- FR-31 Keys can be scoped to specific capabilities.
- FR-32 Key usage is recorded and visible to the user.
- FR-33 A leaked key can be revoked without affecting the account.

### Usage and limits
- FR-34 Usage is metered per account — requests, tokens, documents, storage, long jobs.
- FR-35 A user can view their own usage and limits.
- FR-36 Limits are enforced on both the Telegram and web interfaces, consistently.
- FR-37 A user approaching a limit is informed before it is reached, where feasible.

### Teams
- FR-38 An organization can be created with an owner.
- FR-39 Members can be invited, and invitations are accepted explicitly.
- FR-40 Roles exist with defined permissions — at minimum owner, member, and a read-only role.
- FR-41 A user can belong to multiple organizations.
- FR-42 Shared research is visible to authorized members and invisible to everyone else.
- FR-43 A user can leave an organization.
- FR-44 Removing a member revokes their access immediately.
- FR-45 Every action within an organization is attributable to a member.

### Integrations
- FR-46 The bot can be delivered to Discord, Slack, or other platforms where appropriate.
- FR-47 Each integration maps platform identities to accounts using the same linking model as Telegram.
- FR-48 Each integration enforces the same authorization, limits, and isolation.
- FR-49 An integration cannot bypass access control by using a different code path.
- FR-50 Integrations are optional and additive. The Telegram path is never weakened to serve them.

---

## 6. Technical Requirements

### Frontend

**Next.js is the stated product intent.** This document does not select the hosting platform, deployment topology, or cloud provider — see § 12 and AD-008.

- The frontend is a separate deployable unit from the Python backend.
- It consumes the REST API only. It does not reach the database or the Celery broker directly.
- Server-side rendering where useful; authenticated data is fetched server-side or via authenticated client calls, never embedded in a public page.

### Backend structure

The Phase 1 service layer becomes the shared domain layer for both the bot and the API:

```text
app/domain/          Pure logic and contracts — shared by all interfaces
app/services/        Application services — shared
app/infrastructure/  Clients and adapters — LLM, search, fetch, storage
app/api/             REST routes, schemas, auth middleware
app/bot/             Telegram transport
app/tasks/           Celery entry points
```

**Both the Telegram handler and the REST endpoint call the same service.** A feature implemented once is exposed through both interfaces. Duplicated logic between the bot and the API is a defect, not a convenience.

### Identity and accounts

The Phase 2 user record is extended with an account layer:

```text
Account         — email, password hash, status, timestamps
Identity        — account id, provider (telegram, discord, slack, email),
                  external subject, linked_at
                UNIQUE (provider, external_subject)
Organization    — name, owner account id
Membership      — organization id, account id, role
```

Identity linking is the security-critical operation. The model makes it explicit: an external identity is a row linking an account to a provider subject, unique per provider subject, so one Telegram identity can never be linked to two accounts.

### Authentication

- Password hashing with a modern adaptive algorithm.
- Sessions stored server-side or as signed, httpOnly cookies, with revocation.
- CSRF protection on state-changing requests.
- Authentication middleware applied to the API router, not per-handler, so a new endpoint cannot be added unprotected.
- A session or token is never logged.

### Authorization

- Authorization is enforced in the service layer, not only in route handlers, so every interface gets the same rules.
- Resource access is ownership-checked on every read and write.
- Organization access is membership-checked, with the role determining permitted actions.
- **Default deny.** An endpoint with no explicit authorization is not reachable.

### API design

- Versioned from the outset.
- Request and response schemas generated from the implementation so documentation cannot drift.
- Consistent error format with a machine-readable code.
- Pagination on every list endpoint.
- Idempotency on operations that create billable work.

### Rate limiting and quotas

- Per-credential limits on the API.
- Per-account quotas shared across the Telegram and web interfaces, so a user cannot exceed a limit by switching channels.

### Infrastructure

- **Hosting, cloud provider, and deployment topology are undecided** and are selected during this phase. The repository specifies nothing, so nothing is assumed.
- The deployment must support at least: the API, the worker pool, PostgreSQL, Redis, and the frontend.

---

## 7. Security Requirements

This phase makes the product reachable from the open internet. These requirements are the phase's core.

- **SR-1 No unauthenticated access to any user data.** Every protected resource requires a valid, unexpired, unrevoked credential.
- **SR-2 Server-side authorization on every request.** Client-side checks are a usability affordance, never a control.
- **SR-3 Default deny.** An endpoint without explicit authorization is unreachable.
- **SR-4 Ownership enforced on every read and write.** Identifiers are never sufficient authorization.
- **SR-5 Cross-user access is impossible** across every endpoint, including search, export, and background jobs.
- **SR-6 Account linking requires proof of both identities.** A user cannot claim a Telegram or Discord identity they do not control, because claiming someone else's would expose their research.
- **SR-7 Passwords use a modern adaptive hash.** Plaintext or fast hashes are unacceptable. Reset flows use single-use, expiring tokens.
- **SR-8 Sessions are secure.** httpOnly, secure, SameSite cookies; revocation; rotation on privilege change.
- **SR-9 CSRF protection** on all state-changing requests.
- **SR-10 Rate limiting per credential and per account**, applied to both interfaces.
- **SR-11 API key secrets are shown once and are stored hashed.** A key is never retrievable after creation, and never logged.
- **SR-12 Key scope is enforced on every request**, not only at issuance.
- **SR-13 No secret ever appears in a response, a log, or an error message.** Including configuration errors surfaced to clients.
- **SR-14 Injection defenses for the new surfaces.** SQL injection: parameterized statements or ORM only. XSS: framework escaping, no raw HTML rendering of user or model content. Command injection: no shell invocation of user input anywhere.
- **SR-15 File upload through the web is treated exactly as Telegram upload** — same type validation by content, same size and resource bounds, same path-traversal prevention, same prompt-injection isolation. The web path must not be weaker than the bot path.
- **SR-16 Organization membership is verified on every access.** Role changes and removals take effect immediately.
- **SR-17 Audit trail for organizational actions** — membership changes, role changes, shared-resource access, and deletions are attributable to a member.
- **SR-18 Deleted accounts are fully deleted**, including research, sources, documents, exports, and stored files.
- **SR-19 Integrations share the authorization path.** A platform-specific handler must not be able to bypass the service layer's checks.
- **SR-20 CORS is configured explicitly** rather than left permissive.
- **SR-21 Security headers are set** on every web response, including content-type sniffing protection and framing control.
- **SR-22 Dependency vulnerabilities are checked in CI** once a web dependency tree exists.

---

## 8. Testing Requirements

| ID | Test | Verifies |
|---|---|---|
| T-1 | An unauthenticated request to any protected endpoint is rejected | SR-1 — mandatory |
| T-2 | A revoked or expired session is rejected | SR-8 — mandatory |
| T-3 | **Linking an identity requires proof of both identities** | SR-6 — mandatory |
| T-4 | **One external identity cannot be linked to two accounts** | SR-6 — mandatory |
| T-5 | A user starting in Telegram sees their research after registering on the web | FR-4 |
| T-6 | Authentication failures do not disclose whether an account exists | FR-12 |
| T-7 | **Every endpoint is owner-scoped; cross-user access is impossible** | SR-5 — mandatory |
| T-8 | A new endpoint added without explicit authorization is unreachable by default | SR-3 |
| T-9 | Passwords are stored with an adaptive hash, never plaintext or fast hashes | SR-7 — mandatory |
| T-10 | State-changing requests without a valid CSRF token are rejected | SR-9 |
| T-11 | Rate limits are enforced per credential and per account | SR-10 |
| T-12 | **A limit is enforced consistently across the Telegram and web interfaces** | FR-36 |
| T-13 | An API key secret is shown once and is stored hashed | SR-11 — mandatory |
| T-14 | A revoked key stops working immediately | FR-33 |
| T-15 | A key exceeding its scope is rejected | SR-12 |
| T-16 | No secret appears in any response, log, or error message | SR-13 |
| T-17 | **XSS payloads in user or model content are escaped everywhere, including exports** | SR-14 — mandatory |
| T-18 | SQL injection attempts in API parameters are parameterized, not executed | SR-14 — mandatory |
| T-19 | **A web upload is subject to the same type, size, and path-traversal controls as Telegram upload** | SR-15 — mandatory |
| T-20 | A document containing injection text is isolated identically on the web path | SR-15 |
| T-21 | Only organization members can access shared research | FR-42, SR-16 |
| T-22 | Role changes and member removal take effect immediately | FR-44, SR-16 |
| T-23 | Every organizational action is attributable to a member | SR-17 |
| T-24 | Account deletion removes all data, including stored files | SR-18 — mandatory |
| T-25 | **An integration path cannot bypass service-layer authorization** | SR-19 — mandatory |
| T-26 | CORS is explicitly configured and rejects disallowed origins | SR-20 |
| T-27 | Security headers are present on every response | SR-21 |
| T-28 | The bot and the API return identical results for the same service call | Shared domain layer |
| T-29 | The API documentation is generated from the implementation and does not drift | FR-26 |
| T-30 | Dependency vulnerability scanning runs in CI | SR-22 |

**Mandatory tests:** T-1, T-2, T-3, T-4, T-7, T-9, T-13, T-17, T-18, T-19, T-24, T-25. This phase makes the product publicly reachable; authentication, identity linking, cross-user isolation, secret handling, and upload parity are the defining security properties.

---

## 9. Documentation Requirements

- [ ] The system architecture documented, showing the bot, the API, the worker, the frontend, and the shared domain layer.
- [ ] The account and identity model documented, including the linking model and its security rationale.
- [ ] The authentication and session policy documented.
- [ ] The authorization model documented, including the default-deny rule.
- [ ] **The API documented, generated from the implementation.** Every endpoint, its authentication requirement, its authorization rule, and its rate limit.
- [ ] The API key model documented, including creation, scoping, revocation, and the show-once property.
- [ ] The organization and roles model documented, with a permission matrix.
- [ ] The rate limit and quota policy documented, including how limits apply across interfaces.
- [ ] The deployment topology documented.
- [ ] The threat model documented for the web surface.
- [ ] **Every hosting, cloud, and framework selection recorded in the Architecture Decision Log** with its justification.
- [ ] `docs/PROJECT_PLAN.md` maturity table, phase status, and decision log updated.
- [ ] `docs/ARCHITECTURE.md` updated to reflect both the bot and the web platform.

---

## 10. Acceptance Criteria

### Accounts and identity
- [ ] An account exists independently of Telegram.
- [ ] Telegram identity links to an account with proof of both identities.
- [ ] A Telegram user's existing research appears under their web account.
- [ ] One external identity cannot be linked to two accounts.
- [ ] An account can be deleted along with all associated data.

### Authentication
- [ ] Passwords use a modern adaptive hash.
- [ ] Sessions are httpOnly, secure, revocable, and expire per a defined policy.
- [ ] State-changing requests require CSRF protection.
- [ ] Authentication failures do not disclose account existence.
- [ ] MFA is implemented or its deferral is recorded as a decision.

### Web interface
- [ ] Projects, runs, sources, documents, reports, and job status are all browsable.
- [ ] Research history is searchable and filterable.
- [ ] The UI reads the same data model as the Telegram interface.

### API
- [ ] A versioned REST API exposes the platform's resources.
- [ ] Every endpoint is authenticated and authorized.
- [ ] Every endpoint is user-scoped; cross-user access is impossible.
- [ ] Documentation is generated from the implementation.
- [ ] Errors follow a consistent documented format.
- [ ] Every list endpoint is paginated.

### API keys
- [ ] Keys can be created, named, scoped, and revoked.
- [ ] A key secret is shown once and stored hashed.
- [ ] Key usage is visible to the user.

### Teams
- [ ] Organizations can be created, with owner, member, and read-only roles.
- [ ] Membership is verified on every access; removal takes effect immediately.
- [ ] Organizational actions are attributable.

### Usage
- [ ] Usage is metered per account and visible to the user.
- [ ] **Limits are enforced consistently across Telegram and web.**

### Security
- [ ] No unauthenticated access to any user data.
- [ ] Authorization is default deny and enforced server-side.
- [ ] No secret appears in any response, log, or error message.
- [ ] Injection defenses hold across SQL, XSS, and upload paths.
- [ ] Web uploads are subject to controls at least as strict as Telegram uploads.
- [ ] CORS and security headers are explicitly configured.
- [ ] Dependency vulnerability scanning runs in CI.
- [ ] All twelve mandatory tests pass.

### Quality
- [ ] The bot and the API share one service layer with no duplicated logic.
- [ ] Documentation is updated, including the threat model and the API reference.
- [ ] Every platform selection is recorded in the Architecture Decision Log.
- [ ] No payment or subscription functionality was implemented.

---

## 11. Dependencies

### Prerequisites

| Phase | Item | Why this phase needs it |
|---|---|---|
| Phase 1 | Service layer and dependency injection | The REST API and the bot must call the same services |
| Phase 1 | Error handling, logging, health checks | An internet-facing API needs real health checks and structured logs |
| Phase 2 | User records, token accounting, rate limits | Accounts extend these; quotas are metered here |
| Phase 2 | **User model extensible to an account layer** | Verifying this during Phase 2 avoids reworking every table |
| Phase 3 | Research runs, sources, citations | The primary web resource |
| Phase 4 | Documents | Managed through the web UI |
| Phase 5 | Projects, sessions, tags, bookmarks | The workspace model this phase renders |
| Phase 6 | Agent run traces | Potentially surfaced in the UI |
| Phase 7 | Reports, exports, jobs | Managed and downloaded through the web |

### Infrastructure

- A deployment target supporting the API, worker pool, PostgreSQL, Redis, and frontend. **Undecided.**
- A TLS termination point.
- A secret store for signing keys, database credentials, and provider credentials. **Technology undecided.**

### Blocks

- **Phase 9** monetization requires accounts, organizations, API keys, and usage metering — all delivered here.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **The product becomes internet-facing with no authentication** | Catastrophic data exposure | Authentication and authorization are this phase's core; mandatory tests T-1, T-7 |
| **Identity linking without proof of both identities** | An attacker claims a victim's identity and reads their research | Proof of both identities; unique `(provider, subject)`; mandatory tests T-3, T-4 |
| Authorization enforced per-handler instead of centrally | A new endpoint ships unprotected | Default-deny middleware; mandatory test T-8 |
| **Web upload path weaker than the Telegram path** | The same document becomes exploitable through the easier route | Identical controls on both paths; mandatory test T-19 |
| XSS through user or model content rendered in the dashboard | Session theft, account takeover | Framework escaping, no raw HTML; mandatory test T-17 |
| API keys stored in plaintext | A database breach yields every key | Store hashed; show once; mandatory test T-13 |
| Duplicated logic between the bot and the API | Divergent behavior, double defect surface | One service layer, both interfaces; mandatory test T-28 |
| Rate limits applied per interface rather than per account | Users bypass limits by switching channels | Account-scoped quotas; mandatory test T-12 |
| Premature cloud or hosting commitment | Cost lock-in before requirements are known | Record the decision when made; keep deployment configuration externalized |
| Organization data model without role granularity | Privilege escalation between members | Explicit role model and permission matrix; test role changes take effect immediately |
| Web frontend built before the API contract is stable | Rework as the contract changes | API generated from the implementation; UI consumes the contract |
| Phase 2 user model not extensible to accounts | Reworking every user-scoped table | Verify account extensibility during Phase 2 and record the outcome |

---

## 13. Definition of Done

Phase 8 is complete when:

1. Every acceptance criterion in § 10 is checked and evidenced.
2. All twelve mandatory security tests pass.
3. A user can create an account, link Telegram, and see their existing research.
4. The dashboard exposes projects, runs, sources, documents, reports, and job status.
5. A versioned, documented, generated REST API exists and is fully authenticated and authorized.
6. API keys work with show-once secrets, scoping, and revocation.
7. Organizations with roles function, with immediate effect on removal and attributable actions.
8. The bot and the API share one service layer, verified by test.
9. The threat model and the API reference are documented.
10. Every platform selection is recorded in the Architecture Decision Log.
11. No payment or subscription functionality was implemented.
12. Status is updated in this document and in `docs/PROJECT_PLAN.md`.

---

## 14. Status

**Current status: Not Started**

**Phase status in `docs/PROJECT_PLAN.md`: Not Started.**

**Blocked by:** Phase 2 (data layer and user model), Phase 5 (workspace model), Phase 7 (reports and exports).

No Phase 8 work has been implemented. The repository contains a FastAPI application of 24 lines serving one static health route, no authentication, no account model, no multi-tenancy, no REST API, and no frontend.

**Hosting provider: Open.**
**Cloud provider: Open.**
**Deployment topology: Open.**
**Secret store technology: Open.**

**Upstream action required:** verify during Phase 2 that the user model is extensible to an account and identity layer. Retrofitting accounts across every user-scoped table after Phase 5 and 7 have landed would be substantially more expensive.
