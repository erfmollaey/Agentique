"""Per-run LLM token accounting (FR-9, SR-5, T-25).

The research pipeline makes several provider calls per run — planning, per-source
summarization, per-source claim extraction, and exactly one synthesis call — and
every one of them costs money. Nothing measured that cost until this module existed:
``research_runs.prompt_tokens`` / ``completion_tokens`` had columns the repository
would write and **no caller ever passed a value**, so the run ledger stayed at zero
and no per-run budget could be derived from it.

**What is recorded, and what is not.** Only what the provider *reported*. The
adapters already parse provider usage into
:class:`~app.domain.chat.LLMReply`; this module forwards that number and adds
nothing. Nothing is estimated — an invented count in a column an operator might later
budget from is worse than a visible zero.

One honest limit on what "missing usage" means here: the Phase 2 client normalises an
absent provider ``usage`` block to ``0`` at parse time
(``app/infrastructure/llm.py``), so a provider that reported nothing arrives at this
layer as a genuine zero and is indistinguishable from a call that really used no
tokens. ``calls_without_usage`` therefore counts calls whose **reply** carried no
token fields at all, which is what a provider adapter that stops normalising — or a
test double — produces. Counting upstream absence would mean changing the Phase 2
client's contract, which is outside this change; the limitation is recorded instead of
worked around.

**Retries.** Each *successful* ``complete()`` call records once. A call that raised
is not recorded, because no usage was reported for it. Retries inside the provider
client (its own bounded ``max_retries``) are opaque to this layer: it counts what the
provider hands back, once, and that limitation is stated rather than papered over.

**No double counting on repeated persistence.** ``set_status`` *sets* the ledger
columns rather than incrementing them, so a run whose terminal write is retried
converges on the same total instead of accumulating it.

Scope boundary: this module **records and persists usage; it enforces no budget.**
Cost-budget enforcement, per-user spend ceilings, and rate-limit numbers remain
deliberately undecided (AD-047; audit § U.4.2) and are not introduced here. No
pricing is modelled and no limit is invented.

ScopeVar versus a per-run object: the ledger is held in a :class:`~contextvars.ContextVar`
rather than passed to every stage, because the four research provider adapters are
built once in the composition root and shared across runs. A context variable scopes
the accumulator to the executing run without threading it through the planner, the
evidence service, the synthesis service, and each adapter — and keeps two concurrent
runs from sharing a counter.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel, ConfigDict

log = logging.getLogger(__name__)


class RunTokenLedger(BaseModel):
    """Accumulated provider-reported usage for one research run.

    Mutable by design: it is a running counter, not a record. What it is *not* is a
    budget — no field here expresses a ceiling, because no ceiling has been decided
    (AD-047).
    """

    model_config = ConfigDict(frozen=False)

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    calls_without_usage: int = 0

    def record(self, prompt_tokens: Any, completion_tokens: Any) -> None:
        """Add one call's reported usage.

        Values that are absent or not a non-negative integer count as zero and
        increment ``calls_without_usage``. They are never estimated.
        """
        self.calls += 1
        prompt = _as_token_count(prompt_tokens)
        completion = _as_token_count(completion_tokens)
        if prompt is None or completion is None:
            self.calls_without_usage += 1
        self.prompt_tokens += prompt or 0
        self.completion_tokens += completion or 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def has_usage(self) -> bool:
        """Whether any call reported usage at all."""
        return self.calls > self.calls_without_usage


def _as_token_count(value: Any) -> int | None:
    """A provider-reported token count, or ``None`` when there isn't one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


#: The ledger the *currently executing* research run accumulates into. ``None`` means
#: no run is in scope, which is every context that is not a research pipeline
#: execution — chat, tests, the API process.
_ACTIVE: contextvars.ContextVar[RunTokenLedger | None] = contextvars.ContextVar(
    "research_token_ledger", default=None
)


def active_ledger() -> RunTokenLedger | None:
    """The ledger in scope, or ``None`` outside a research run."""
    return _ACTIVE.get()


def record_usage(prompt_tokens: Any, completion_tokens: Any) -> None:
    """Record one call against the run in scope; a no-op when none is.

    Called by :class:`UsageRecordingLLM` after each provider call. Outside a research
    run the tokens are simply not accounted anywhere — deliberately, because the chat
    path keeps its own per-turn accounting (``app/services/chat.py``) and must not
    have research's totals mixed into it.
    """
    ledger = _ACTIVE.get()
    if ledger is None:
        return
    ledger.record(prompt_tokens, completion_tokens)


@contextmanager
def token_scope(ledger: RunTokenLedger | None = None) -> Iterator[RunTokenLedger]:
    """Accumulate provider usage into ``ledger`` for the duration of the block.

    Yields the ledger so the caller can read the totals afterwards. A new ledger is
    created when none is supplied. The previous value is restored on exit, including
    on an exception, so a nested or abandoned scope cannot leave a stale accumulator
    behind for an unrelated caller.
    """
    active = ledger if ledger is not None else RunTokenLedger()
    token = _ACTIVE.set(active)
    try:
        yield active
    finally:
        _ACTIVE.reset(token)


class UsageRecordingLLM:
    """A pass-through ``LLMProvider`` that records reported usage into the active run.

    Wrapping rather than editing each adapter is what keeps this small and complete:
    the planner, the source summarizer, the claim extractor, and the synthesizer all
    reach the provider the same way (``llm.complete(...)``), so one wrapper covers
    every research model call and none of the four adapters changes.

    It is a **pass-through**: it returns the provider's reply untouched, adds no
    retry, interprets nothing, and makes no decision. A recording wrapper that altered
    its delegate's output would be a second, unaccounted provider behaviour.

    It satisfies ``LLMProvider`` structurally — the container passes it wherever the
    real client is expected — and it deliberately does not claim the full protocol,
    because it exists to serve the research pipeline's four adapters, not to be a
    general provider replacement.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    @property
    def inner(self) -> Any:
        """The wrapped provider, for a caller that needs the real thing."""
        return self._inner

    def complete(self, messages, **kwargs: Any):
        """Delegate to the provider, then record whatever usage it reported."""
        reply = self._inner.complete(messages, **kwargs)
        record_usage(
            getattr(reply, "prompt_tokens", None),
            getattr(reply, "completion_tokens", None),
        )
        return reply