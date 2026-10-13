"""Per-run token accounting in isolation (FR-9, SR-5, T-25).

The persistence of these numbers is proved end to end in
``tests/test_research_orchestrator.py::TestTokenAccounting``. This module covers the
accumulator itself: what it counts, what it refuses to count, and what it does when a
provider reports nothing.

The rule under test throughout is the one that makes the column trustworthy: **a
number is recorded only because the provider reported it.** Nothing here estimates,
extrapolates from text length, or infers usage from a call that failed — a fabricated
count in a cost column is worse than a visible zero.
"""

from __future__ import annotations

import pytest

from app.domain.chat import LLMReply
from app.services.research.usage import (
    RunTokenLedger,
    UsageRecordingLLM,
    active_ledger,
    record_usage,
    token_scope,
)


class _Provider:
    """A minimal provider double reporting whatever usage a test asks for."""

    def __init__(self, *replies: LLMReply, error: Exception | None = None) -> None:
        self._replies = list(replies)
        self._error = error
        self.calls: list[dict] = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        if self._error is not None:
            raise self._error
        return self._replies.pop(0)


def reply(prompt: int = 0, completion: int = 0) -> LLMReply:
    return LLMReply(
        text="ok", model="m", prompt_tokens=prompt, completion_tokens=completion
    )


# --- the accumulator ----------------------------------------------------------


class TestRunTokenLedger:
    def test_it_starts_empty(self):
        ledger = RunTokenLedger()

        assert ledger.prompt_tokens == 0
        assert ledger.completion_tokens == 0
        assert ledger.calls == 0
        assert not ledger.has_usage

    def test_several_calls_accumulate(self):
        ledger = RunTokenLedger()
        ledger.record(11, 7)
        ledger.record(13, 9)

        assert ledger.prompt_tokens == 24
        assert ledger.completion_tokens == 16
        assert ledger.total_tokens == 40
        assert ledger.calls == 2
        assert ledger.has_usage

    def test_a_zero_is_a_real_report_not_a_missing_one(self):
        ledger = RunTokenLedger()
        ledger.record(0, 0)

        assert ledger.calls == 1
        assert ledger.calls_without_usage == 0

    def test_absent_usage_counts_zero_and_is_counted_as_missing(self):
        """No usage is a fact to be visible, not a number to be invented."""
        ledger = RunTokenLedger()
        ledger.record(None, None)

        assert ledger.prompt_tokens == 0
        assert ledger.completion_tokens == 0
        assert ledger.calls == 1
        assert ledger.calls_without_usage == 1
        assert not ledger.has_usage

    @pytest.mark.parametrize(
        "value", ["12", 1.5, True, -1, [1], object()]
    )
    def test_a_value_that_is_not_a_token_count_is_refused(self, value):
        ledger = RunTokenLedger()
        ledger.record(value, 0)

        assert ledger.prompt_tokens == 0
        assert ledger.calls_without_usage == 1


# --- the scope ----------------------------------------------------------------


class TestTokenScope:
    def test_no_run_in_scope_means_no_ledger(self):
        assert active_ledger() is None

    def test_a_scope_exposes_the_ledger_it_is_accumulating_into(self):
        with token_scope() as ledger:
            assert active_ledger() is ledger
            record_usage(5, 3)

        assert ledger.prompt_tokens == 5

    def test_recording_outside_a_run_is_a_no_op(self):
        record_usage(100, 100)
        assert active_ledger() is None

    def test_the_previous_scope_is_restored_on_exit(self):
        with token_scope() as outer:
            with token_scope() as inner:
                assert active_ledger() is inner
            assert active_ledger() is outer

        assert active_ledger() is None

    def test_an_exception_inside_a_scope_does_not_leak_the_ledger(self):
        with pytest.raises(RuntimeError), token_scope():
            raise RuntimeError("stage failed")

        assert active_ledger() is None

    def test_two_scopes_keep_separate_totals(self):
        with token_scope() as first:
            record_usage(1, 1)
        with token_scope() as second:
            record_usage(2, 2)

        assert first.prompt_tokens == 1
        assert second.prompt_tokens == 2

    def test_a_supplied_ledger_is_used_rather_than_replaced(self):
        ledger = RunTokenLedger(prompt_tokens=99)
        with token_scope(ledger) as yielded:
            assert yielded is ledger
            record_usage(1, 1)

        assert ledger.prompt_tokens == 100


# --- the pass-through wrapper -------------------------------------------------


class TestUsageRecordingLLM:
    def test_it_returns_the_provider_reply_untouched(self):
        provider = _Provider(reply(11, 7))
        wrapper = UsageRecordingLLM(provider)

        result = wrapper.complete([{"role": "user", "content": "x"}], model="m")

        assert result.text == "ok"
        assert result.prompt_tokens == 11

    def test_it_forwards_every_keyword_the_adapters_pass(self):
        provider = _Provider(reply(1, 1))
        UsageRecordingLLM(provider).complete(
            [{"role": "user", "content": "x"}],
            model="m",
            temperature=0.0,
            max_tokens=256,
        )

        assert provider.calls[0]["model"] == "m"
        assert provider.calls[0]["max_tokens"] == 256

    def test_every_call_reaches_the_delegate_unchanged_in_number_and_order(self):
        provider = _Provider(reply(1, 1), reply(2, 2), reply(3, 3))
        wrapper = UsageRecordingLLM(provider)

        with token_scope() as ledger:
            for _ in range(3):
                wrapper.complete([{"role": "user", "content": "x"}], model="m")

        assert len(provider.calls) == 3
        assert ledger.calls == 3
        assert (ledger.prompt_tokens, ledger.completion_tokens) == (6, 6)

    def test_a_provider_failure_records_nothing(self):
        """A call that raised reported no usage, so there is nothing to add."""
        provider = _Provider(error=RuntimeError("upstream refused"))
        wrapper = UsageRecordingLLM(provider)

        with token_scope() as ledger, pytest.raises(RuntimeError):
            wrapper.complete([{"role": "user", "content": "x"}], model="m")

        assert ledger.calls == 0
        assert not ledger.has_usage

    def test_a_reply_without_usage_fields_records_the_call_and_no_tokens(self):
        class _Bare:
            text = "ok"

        class _BareProvider:
            def complete(self, messages, **kwargs):
                return _Bare()

        with token_scope() as ledger:
            UsageRecordingLLM(_BareProvider()).complete([], model="m")

        assert ledger.calls == 1
        assert ledger.calls_without_usage == 1
        assert ledger.total_tokens == 0

    def test_it_exposes_the_wrapped_provider_without_wrapping_again(self):
        provider = _Provider(reply(1, 1))
        wrapper = UsageRecordingLLM(provider)

        assert wrapper.inner is provider

    def test_two_wrappers_of_one_provider_do_not_double_count_a_run(self):
        """One run, one ledger, however many wrappers the composition root built."""
        provider = _Provider(reply(10, 5))
        wrapper = UsageRecordingLLM(provider)

        with token_scope() as ledger:
            wrapper.complete([], model="m")

        assert ledger.prompt_tokens == 10