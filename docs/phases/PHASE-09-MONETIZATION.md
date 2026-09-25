# Phase 9 — Monetization

**Status:** Not Started
**Blocked by:** Phase 8 — SaaS / Web Platform
**Evidence base:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md)

> **Nothing in this phase is implemented.** The repository has no billing code, no subscription model, no entitlement system, no metering, and no pricing.
>
> **This document makes no pricing claims and proposes no price points.** Tiers are described structurally. Pricing, packaging, and entitlement values require market validation and are outside the scope of engineering planning.

---

## 1. Objective

Establish a sustainable business model: define tiers, define what each tier can do, meter actual usage, enforce entitlements reliably, and process payments safely.

This phase is the last in the chain because it depends on everything it meters. Entitlements can only be enforced against capabilities that exist, and usage can only be metered against an account and identity model.

---

## 2. Current State

### Verified absences

| Capability | Status | Evidence |
|---|---|---|
| Billing model | Not Implemented | No billing code, no plan or tier model |
| Subscription management | Not Implemented | No subscription entity |
| Payment integration | Not Implemented | No payment provider |
| Entitlement enforcement | Not Implemented | No entitlement checks anywhere |
| Usage metering for billing | Not Implemented | Token usage recording is Phase 2 work; no billing-grade metering exists |
| Invoice generation | Not Implemented | No invoicing code |
| Tax handling | Not Implemented | No tax logic |
| Proration | Not Implemented | No billing period logic |
| Dunning — failed-payment handling | Not Implemented | No retry or grace-period logic |
| Webhook handling from a payment provider | Not Implemented | The application exposes no webhook endpoint; the only route is `GET /health` |
| Admin or operator tooling | Not Implemented | No administrative interface |
| Pricing | Not decided | No pricing research or validation exists |

### What will exist by the time this phase begins

- Accounts, identities, and organizations — Phase 8.
- API keys with scoping — Phase 8.
- Usage metering per account across Telegram and web — Phase 2 and Phase 8.
- A defined capability set — Phases 2 through 7.

**Monetization is a policy layer over capabilities that already exist.** It creates no new product capability.

### An important consequence of the current code

`GET /health` at `app/main.py:11-13` is currently the only HTTP route, and `docker-compose.yml` publishes Redis and PostgreSQL to the host with default credentials (audit S-4). Neither is a monetization concern, but a payment webhook endpoint will be the first endpoint reachable from the public internet at scale and handled by a third party. **Its security posture will be set by the Phase 1 and Phase 8 work, not by this phase.** The webhook must be treated as an internet-facing, untrusted input surface from the moment it is created.

---

## 3. Scope

- A tier and plan model.
- Entitlement definitions mapping tiers to capabilities and limits.
- Subscription lifecycle: creation, upgrade, downgrade, cancellation, renewal.
- Usage metering accurate enough to bill against.
- Entitlement enforcement on every capability-gated operation.
- Payment provider integration, behind an abstraction.
- Webhook handling from the payment provider.
- Invoice generation and delivery.
- Tax handling where required.
- Proration for mid-cycle changes.
- Dunning for failed payments.
- Customer-facing management: view plan, view usage, upgrade, cancel.
- Operator tooling for support and manual intervention.

---

## 4. Out of Scope

- **New product capability.** Phases 2 through 8. This phase gates and meters existing capabilities.
- **New research, document, agent, or report capability.**
- **A new user interface.** Phase 8. This phase adds billing surfaces to the existing one.
- **Changing the Telegram bot's conversational behavior** beyond entitlement responses.
- **Fixing Phase 1–8 defects.** Report them; do not absorb them.

**Scope note:** building a product feature because a tier needs something to sell is a scope violation in reverse. The tier model describes existing capabilities. If a capability is missing, it belongs to its own phase.

---

## 5. Functional Requirements

### Plan and tier model
- FR-1 Plans are defined as data: an identifier, a display name, a set of entitlements, and a set of limits.
- FR-2 At minimum, the tier structure distinguishes Free, Pro, and Team.
- FR-3 Adding a plan does not require a code change.
- FR-4 Entitlements and limits are data, not conditional expressions scattered through the codebase.
- FR-5 Every gated capability references a named entitlement.

### Entitlements
- FR-6 An account has exactly one plan at a time, except a Team plan which carries an organization.
- FR-7 An entitlement check is available to every interface — Telegram and web alike.
- FR-8 A denied action produces a clear message naming the required plan. It never fails silently.
- FR-9 Entitlement checks are enforced server-side. A hidden button is not enforcement.
- FR-10 Enforcement happens in the service layer so every interface behaves identically.

### Limits and metering
- FR-11 Limits are defined per plan and cover requests, tokens, documents, storage, concurrent jobs, and API calls.
- FR-12 Usage is metered per account and is queryable by the user.
- FR-13 Usage is metered accurately enough to bill against, with a documented accuracy characteristic.
- FR-14 Usage is metered consistently across the Telegram and web interfaces.
- FR-15 A user approaching a limit is informed before it is reached, where feasible.
- FR-16 Exceeding a hard limit is enforced. Exceeding a soft limit warns.
- FR-17 Metering is not bypassable by switching interfaces or by using an API key.

### Subscription lifecycle
- FR-18 A subscription can be created, upgraded, downgraded, and cancelled.
- FR-19 A downgrade takes effect at the period boundary, not mid-cycle, unless the user chooses immediate effect.
- FR-20 Cancellation takes effect at the end of the paid period; access does not vanish immediately unless requested.
- FR-21 Renewal is handled automatically.
- FR-22 Subscription state is reconciled against the payment provider, not assumed to be correct locally.

### Payments
- FR-23 Payment processing is behind a provider abstraction. The provider is not hardcoded throughout.
- FR-24 The payment provider is **not selected by this document** and is chosen during implementation.
- FR-25 No full card data is handled by this system. Card collection is delegated to the provider's hosted surface or components.
- FR-26 Webhook handling is signature-verified before any state change.
- FR-27 Webhook handling is idempotent. A duplicated delivery does not double-apply a change.
- FR-28 Webhook payloads are validated as untrusted input.
- FR-29 Webhook secrets are configuration, never source literals.
- FR-30 An invalid or unsigned webhook is rejected and logged without changing state.

### Invoicing and tax
- FR-31 Invoices are generated and made available to the user.
- FR-32 Tax is calculated where legally required, by the provider or by a documented approach.
- FR-33 Proration is applied on mid-cycle plan changes and is explained to the user.
- FR-34 Currency handling is explicit and unambiguous.

### Dunning
- FR-35 A failed payment enters a defined dunning sequence with clear customer notification.
- FR-36 A grace period exists before premium access is restricted.
- FR-37 The user is always notified before access is restricted, with a path to resolve it.
- FR-38 Service resumes automatically once payment succeeds.

### Customer-facing management
- FR-39 A user can view their current plan, entitlements, limits, and usage.
- FR-40 A user can upgrade, downgrade, and cancel.
- FR-41 A user can access their billing history and invoices.
- FR-42 All of the above are available from Telegram as well as the web, consistent with the product's primary interface.

### Operator tooling
- FR-43 Support staff can look up an account's plan, usage, and subscription state.
- FR-44 Manual plan changes and credits are possible and are **recorded in an audit trail**.
- FR-45 Operator access to customer data is itself authorized and audited.

---

## 6. Technical Requirements

### Data model

```text
Plan             — key, name, entitlements, limits, active     (data, not code)
Subscription     — account id or organization id, plan id, provider customer id,
                   provider subscription id, status, current period start/end,
                   cancel_at_period_end
Entitlement      — key, description                              (named, referenced)
UsageRecord      — account id, metric, quantity, period, source interface
EntitlementCheck — service, not a table: a single enforcement point
Invoice          — subscription id, provider reference, amount, currency, status,
                   period, document reference
WebhookEvent     — provider event id (unique), type, received_at, processed_at,
                   payload reference, outcome
```

`Plan` is data. Adding or changing a plan is a configuration and migration exercise, not a release.

### Enforcement architecture

```text
Every capability-gated operation
        ↓
EntitlementService.check(account, entitlement_key)
        ↓
Allow  → proceed
Deny   → clear message naming the required plan
```

**This is the single enforcement point.** It lives in the service layer, so the Telegram handler, the REST API, the Celery worker, and any future integration all get identical behavior. A capability must be reachable only through a path that consults it. A capability with no check is an unbilled capability.

### Metering

- Usage is recorded at the point work is actually performed, not estimated afterward.
- Records are attributed to an account, a metric, a period, and the interface that incurred them.
- Metering is **append-only**. Corrected usage is recorded as an adjustment, never by mutating history, so the record is auditable.
- Idempotency matters: a retried Celery task must not be billed twice. The Phase 1 idempotency work and the Phase 7 job checkpointing both apply here.

### Webhook handling

The webhook is a public endpoint receiving third-party data. It must be treated as hostile input:

- Signature verification against a configured secret **before** parsing or acting.
- Timestamp tolerance to reject replays.
- Idempotency on the provider's event identifier.
- Schema validation of the payload.
- No state change on an unverifiable request.
- Replay of a valid event is safe.

### Payment provider — undecided

**This document does not select a payment provider.** Selection criteria during implementation: supported countries and currencies, tax handling, subscription and proration support, webhook reliability, fee structure, and the compliance obligations attaching to the chosen model. The choice sits behind the abstraction in FR-23.

### Security infrastructure

- Webhook secrets and API credentials live in a secret store — technology undecided — not in environment files committed to the repository.
- All provider calls are server-side. No payment secret ever reaches a browser or a Telegram message.
- The client receives only opaque references and hosted-page redirects.

### Auditability

Billing state must be reconstructable. Every plan change, manual adjustment, credit, and entitlement override is recorded with actor, timestamp, reason, and previous value.

---

## 7. Security Requirements

- **SR-1 The webhook is untrusted input.** Verify the signature before parsing or acting. Reject anything unverifiable without changing state.
- **SR-2 Webhook idempotency.** A duplicated delivery must not double-apply a state change or double-charge.
- **SR-3 Replay protection.** Enforce a timestamp tolerance window.
- **SR-4 No card data handled by this system.** Payment details are collected by the provider. Storing, logging, or transmitting raw card data is a prohibited implementation.
- **SR-5 Secrets never reach the client.** No payment credential, webhook secret, or provider API key appears in a browser response, a Telegram message, a log, or an error.
- **SR-6 Entitlement enforcement is server-side and centralized.** Client-side checks are advisory. A missing check is a revenue defect and a policy defect.
- **SR-7 Metering is tamper-resistant.** Usage is derived server-side from actual work. Client-supplied usage claims are never trusted.
- **SR-8 **A user cannot bypass limits by switching interface or using an API key.** Account-scoped metering and enforcement.
- **SR-9 Webhook payloads are stored safely.** Raw payloads may contain personal data; retention is bounded and access is controlled.
- **SR-10 Operator access is authorized and audited.** Manual plan changes and credits are privileged actions with a recorded actor and reason.
- **SR-11 Financial data at rest is protected.** Billing records are access-controlled and encrypted appropriately.
- **SR-12 Financial data in transit is protected.** All provider communication is over TLS, with certificate verification intact.
- **SR-13 PII is minimized.** Store the provider's customer reference rather than duplicating personal details the provider already holds.
- **SR-14 Audit logs are append-only and retained** for the period required to reconstruct billing history.
- **SR-15 Webhook endpoints are rate-limited** so they cannot be used for denial-of-service against the application.
- **SR-16 No pricing or entitlement logic trusts client-supplied values.** Price, plan, and entitlement are resolved server-side from the plan table.

---

## 8. Testing Requirements

| ID | Test | Verifies |
|---|---|---|
| T-1 | An unsigned webhook is rejected and changes no state | SR-1 — mandatory |
| T-2 | **A duplicated webhook delivery applies the change exactly once** | SR-2 — mandatory |
| T-3 | A webhook outside the timestamp tolerance is rejected | SR-3 — mandatory |
| T-4 | A malformed webhook payload is rejected without a crash | SR-1 |
| T-5 | Adding a plan requires no code change | FR-3, FR-4 |
| T-6 | Every named entitlement is enforced on the operation that references it | FR-6, FR-9 — mandatory |
| T-7 | **The same entitlement check applies identically on Telegram, web, and API** | FR-7, FR-10, SR-8 — mandatory |
| T-8 | A denied action names the required plan | FR-8 |
| T-9 | An account exceeding a hard limit is blocked; a soft limit warns | FR-16 |
| T-10 | **Usage is metered identically regardless of interface** | FR-14, SR-8 — mandatory |
| T-11 | **A retried task is not billed twice** | Idempotency — mandatory |
| T-12 | Usage records are append-only; corrections are adjustments | Auditability |
| T-13 | A downgrade takes effect at the period boundary | FR-19 |
| T-14 | Cancellation preserves access until the period end | FR-20 |
| T-15 | Subscription state reconciles against the provider after a missed webhook | FR-22 |
| T-16 | **No card data is stored, logged, or transmitted by this system** | SR-4 — mandatory |
| T-17 | **No payment secret appears in any response, log, or error** | SR-5 — mandatory |
| T-18 | No provider secret reaches a browser or a Telegram message | SR-5 |
| T-19 | Proration is calculated correctly and explained to the user | FR-33 |
| T-20 | A failed payment enters dunning with notification and a grace period | FR-35, FR-36 |
| T-21 | Access is restricted only after notification and the grace period | FR-37 |
| T-22 | Access resumes automatically after a successful payment | FR-38 |
| T-23 | A user can view plan, limits, usage, and invoices | FR-39, FR-41 |
| T-24 | Plan management is available from Telegram as well as the web | FR-42 |
| T-25 | **Operator actions are authorized and recorded in the audit trail** | SR-10 — mandatory |
| T-26 | A non-operator cannot reach operator tooling | SR-10 |
| T-27 | Webhook payloads are retained for a bounded period and access-controlled | SR-9 |
| T-28 | The webhook endpoint is rate-limited | SR-15 |
| T-29 | Price and plan are resolved server-side, never from client input | SR-16 |
| T-30 | Billing history is reconstructable from the audit trail | SR-14 |

**Mandatory tests:** T-1, T-2, T-3, T-6, T-7, T-10, T-11, T-16, T-17, T-25. Webhook verification and idempotency, centralized enforcement, cross-interface metering, metering idempotency, card-data prohibition, secret containment, and operator auditability are the defining security and correctness properties of this phase.

---

## 9. Documentation Requirements

- [ ] The plan and tier model documented, with the complete entitlement and limit matrix.
- [ ] The entitlement enforcement architecture documented, including the single enforcement point.
- [ ] **The entitlement-to-capability mapping documented**, so it is verifiable that no capability is ungated.
- [ ] The metering model documented, including metric definitions and the accuracy characteristic.
- [ ] The subscription lifecycle documented, including downgrade, cancellation, and proration.
- [ ] The dunning sequence documented with the customer notification schedule.
- [ ] Webhook handling documented, including signature verification, idempotency, and replay protection.
- [ ] The data-retention policy documented, particularly for webhook payloads and personal data.
- [ ] The operator runbook documented, including manual plan changes and credits.
- [ ] **The payment provider selection recorded in the Architecture Decision Log** with the criteria and its justification. See AD-009.
- [ ] Tax and compliance obligations documented, with a clear note of what requires professional advice.
- [ ] `docs/PROJECT_PLAN.md` maturity table, phase status, and decision log updated.
- [ ] `docs/ARCHITECTURE.md` updated.

### A note on legal and tax matters

Tax treatment, invoicing requirements, consumer protection, and payment-provider compliance obligations vary by jurisdiction and carry professional-advisory implications. This document specifies only the technical mechanisms. It does not provide legal or tax advice, and those questions are referred to qualified professionals during implementation.

---

## 10. Acceptance Criteria

### Plans and entitlements
- [ ] Plans are defined as data; adding a plan requires no code change.
- [ ] Free, Pro, and Team tiers exist structurally.
- [ ] Entitlements are named and referenced by the operations that require them.
- [ ] **Every capability-gated operation is enforced through a single service-layer check.**
- [ ] A denied action clearly names the required plan.
- [ ] Enforcement is identical across Telegram, web, and API.

### Limits and metering
- [ ] Limits cover requests, tokens, documents, storage, concurrent jobs, and API calls.
- [ ] Usage is metered per account and visible to the user.
- [ ] Usage is metered consistently across interfaces.
- [ ] **A retried task is never billed twice.**
- [ ] Hard limits are enforced; soft limits warn.
- [ ] Usage records are append-only and auditable.

### Subscriptions and payments
- [ ] Subscriptions can be created, upgraded, downgraded, and cancelled.
- [ ] Downgrade and cancellation follow the documented boundary policy.
- [ ] Subscription state reconciles against the provider.
- [ ] Payment processing sits behind a provider abstraction.
- [ ] **The webhook signature is verified before any state change.**
- [ ] **A duplicated webhook delivery applies exactly once.**
- [ ] Replays outside the tolerance window are rejected.
- [ ] **No card data is stored, logged, or transmitted by this system.**
- [ ] **No payment secret reaches a client, a log, or an error message.**
- [ ] Invoices are generated and available to the user.
- [ ] Proration is calculated and explained.
- [ ] A failed payment enters dunning, notifies the customer, and restores access on success.

### Customer and operator surfaces
- [ ] A user can view plan, entitlements, limits, usage, and invoices.
- [ ] Plan management is available from Telegram as well as the web.
- [ ] **Operator tooling is authorized and every privileged action is audited.**

### Documentation
- [ ] The entitlement-to-capability mapping is documented and verifiable.
- [ ] The payment provider decision is recorded in the Architecture Decision Log.
- [ ] The operator runbook exists.
- [ ] The data-retention policy is documented.
- [ ] The ten mandatory tests pass.

---

## 11. Dependencies

### Prerequisites

**Phase 8 must be complete.** Specifically required:

| Phase 8 item | Why Phase 9 needs it |
|---|---|
| Accounts and identities | A subscription attaches to an account |
| Organizations and roles | A Team plan attaches to an organization |
| API keys with scoping | API usage is billable and must be attributable |
| Usage metering | The quantity being billed |
| Rate limits and quotas | The limits being sold |
| Authenticated web surface | Plan management and invoices |
| Default-deny authorization | Entitlement checks integrate with existing authorization |

### Also required

- Phase 2 token accounting, which is the base metering signal.
- Phase 6 cost attribution per agent, so agent-heavy operations are meterable.
- Phase 7 job and capability definitions, since premium capabilities are the substance of higher tiers.

### Infrastructure

- A secret store for webhook secrets and provider credentials — **technology undecided**.
- A public HTTPS endpoint for webhooks, with TLS termination and signature verification.
- Persistent storage for subscriptions, invoices, usage records, and the audit trail.
- **Payment provider — undecided.**

### Blocks

None. This is the terminal phase of the current roadmap.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **A capability ships with no entitlement check** | Unbilled usage; revenue loss discovered late | Central single enforcement point; the entitlement-to-capability mapping is documented and reviewed; mandatory test T-6 |
| **Duplicate webhook delivery double-charges a user** | Direct financial harm to a customer | Idempotency on the provider event identifier; mandatory test T-2 |
| **Unsigned or forged webhook accepted** | Fraudulent plan changes | Verify before acting; mandatory test T-1 |
| **Card data handled in this system** | Severe compliance and breach exposure | Delegate collection to the provider; mandatory test T-16 |
| **A payment secret leaks to a client or a log** | Account compromise, financial exposure | Server-side only; secret store; mandatory test T-17 |
| **Limits bypassed by switching interface or using an API key** | Unbounded cost exposure | Account-scoped metering and enforcement; mandatory test T-7, T-10 |
| **A retried task is billed twice** | Customer billing disputes | Idempotent metering keyed to work actually performed; mandatory test T-11 |
| **Metering is inaccurate** | Unbilled usage or disputed charges | Meter at the point of work, append-only with adjustments, documented accuracy characteristic |
| **Webhook endpoint becomes a denial-of-service vector** | Availability impact | Rate limiting; mandatory test T-28 |
| **Operator tooling becomes an unmonitored backdoor** | Unauthorized plan changes, revenue loss | Privileged, authorized, audited; mandatory test T-25 |
| **Entitlement logic scattered through the codebase** | Inconsistent enforcement, unmaintainable | Single service-layer enforcement point; plans as data |
| **Downgrade or cancellation mishandled** | Customer loss, chargeback risk | Explicit boundary policy, notification, documented behavior |
| **Payment provider lock-in** | Costly migration | Provider abstraction, as in Phases 3, 4, and 6 |
| **Tax and legal obligations underestimated** | Compliance exposure | Refer to qualified professionals; document obligations as a runbook item, not an engineering assumption |

---

## 13. Definition of Done

Phase 9 is complete when:

1. Every acceptance criterion in § 10 is checked and evidenced.
2. All ten mandatory tests pass.
3. Plans, entitlements, and limits exist as data, enforced through a single service-layer check.
4. **The entitlement-to-capability mapping is documented and reviewed, with no ungated capability.**
5. Usage is metered per account, consistently across interfaces, and is not double-charged on retry.
6. Webhook handling is signature-verified, idempotent, and replay-resistant.
7. **No card data is handled and no payment secret reaches a client or a log.**
8. Subscription lifecycle, dunning, invoicing, and proration work and are documented.
9. Operator tooling is authorized and audited.
10. The payment provider decision is recorded in the Architecture Decision Log.
11. Documentation is updated, including the operator runbook and retention policy.
12. Status is updated in this document and in `docs/PROJECT_PLAN.md`.

---

## 14. Status

**Current status: Not Started**

**Phase status in `docs/PROJECT_PLAN.md`: Not Started.**

**Blocked by:** Phase 8 — SaaS / Web Platform.

No Phase 9 work has been implemented. The repository has no billing model, no subscription entity, no entitlement enforcement, no usage metering for billing, no payment integration, and no pricing.

**Payment provider: Open.** Recorded as AD-009 in the Architecture Decision Log. No provider is selected and none is assumed by this document.

**Pricing: Not decided.** No price points are proposed in this document. Pricing requires market validation and is outside the scope of engineering planning.

**A note on the existing HTTP surface:** `GET /health` at `app/main.py:11-13` is currently the only route. The payment webhook will be the first endpoint exposed at scale to third-party traffic. Its security posture is established by Phase 1 and Phase 8 work, not by this phase, and it must be treated as hostile input from the moment it is created.
