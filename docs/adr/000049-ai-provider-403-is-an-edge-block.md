# AD-049 — AI-provider 403 is an edge block; do not retry it

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-10-11 |
| **Supersedes** | nothing |
| **Touches** | no production code |
| **Does not touch** | `app/infrastructure/llm.py`, the SSRF boundary |

## Context

A `/research` run failed during evidence extraction with `LLMServiceError`
marked `retryable=False`, and the run terminated. The observed error was an
HTTP `403` with body `{"error":{"message":"Forbidden"}}` from
`https://api.groq.com/openai/v1/chat/completions`.

The intuitive reading is one of two things — a rate limit, or a bad credential —
and each has an obvious fix. Neither reading survives contact with the evidence.

## Investigation

The classification was settled with a controlled comparison inside the worker
container, because the status code alone does not distinguish "the provider
refused you" from "something in front of the provider refused you".

```
configured key + configured model   -> 403 {"error":{"message":"Forbidden"}}
configured key + a different model  -> 403 {"error":{"message":"Forbidden"}}
deliberately-bogus key              -> 403 {"error":{"message":"Forbidden"}}   <-- the tell
GET /v1/models, configured key      -> 403 {"error":{"message":"Forbidden"}}
GET /v1/models, bogus key           -> 403 {"error":{"message":"Forbidden"}}
```

A **deliberately invalid credential produces a byte-identical response to the
configured one.** Groq's authentication never runs, so the request is refused at
the edge before the credential is examined. Corroborating evidence:

* the TLS peer certificate is genuinely `CN=api.groq.com`, issued by Google Trust
  Services, so the chain is real — this is not a downgrade or an impostor;
* the response carries `server: cloudflare` and a `cf-ray` header, so the refusal
  is Cloudflare's, in front of Groq;
* `api.groq.com` resolves to globally-routable addresses (`188.114.99.0`), so
  this is **not** the fake-IP interception recorded in AD-048 — that problem
  produces `FetchBlockedDestinationError` at a different layer and, in this same
  environment, eventually made the DoH endpoints unreachable entirely.

`GET /v1/models` failing identically is the strongest signal: the models endpoint
is a read that no rate limit or quota would block while the configured key was
otherwise working.

## Decision

**No code change.** The two fixes the situation invites are both wrong here.

**Not backoff or retry.** A 403 is a determination, not a refusal-to-serve. The
client distinguishes it correctly already:

* `app/infrastructure/llm.py` treats `403` as non-transient and raises
  `LLMServiceError(retryable=False)`;
* the Celery annotations carry `retry_backoff: True` and `retry_jitter: True`,
  so the retryable cases (429/408/409) are already retried with backoff *and*
  jitter, and the OpenAI SDK itself retries those statuses internally
  (`LLM_MAX_RETRIES`) before the client ever sees them.

Adding backoff for a 403 would convert a fast, honest failure into a slow one
that spends the run's budget and eventually reports the same thing.

**Not a credential fix in code.** The configured credential is not the problem —
the server never looked at it. Rotating it is an operational action, and the
correct next step is to inspect the Cloudflare edge policy for this egress, not
to change the application.

The existing classification is asserted by tests in
`tests/test_llm_provider_and_fallback.py` — 403 in the non-retryable set,
429/408/409 in the retryable set — so the behaviour is covered.

## Consequences

* A 403 fails fast, marks the run terminal, and tells the user once. That is the
  desired outcome: the run stops, the operator is left a signal worth reading,
  and no budget is re-spent.
* A genuine 429 still retries at the SDK level and again at the task level, with
  jitter at the task level.
* **This does not prove the research pipeline can complete end to end.** With
  the provider blocked, no run can pass planning, so the terminal-delivery path
  remains unverified (see ADR-048). Item 1's measurement of a full pipeline is
  likewise bounded by the same block.

## Limits

The diagnosis is an edge-policy problem on *this* network path. It is not a
statement about Groq's availability in general, and it is not a statement about
the code. If the block clears, the same classification logic needs no change;
if a *different* status appears, it should be classified on its own merits
rather than assumed to be this one.
