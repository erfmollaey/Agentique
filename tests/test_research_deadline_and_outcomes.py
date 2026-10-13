"""B5 — the cooperative deadline and the outcome classification (Phase 3, P3.9-D).

The deadline and the outcome kinds are *pure* contracts, so they are tested pure:
the deadline has a clock injected rather than read from the process, and the
outcome-to-status mapping is asserted against every value. This is the same
discipline P3.5 applied to ``reference_time`` and for the same reason — a deadline
whose time cannot be controlled is a deadline whose behaviour is untested.

What is proved here:

* an unbounded deadline never expires, and says so rather than substituting a number;
* a budgeted deadline reports expiry and only expiry, and never raises or cancels;
* the mapping from outcome to ``ResearchRunStatus`` is total and one-directional;
* the ``failure_summary`` the orchestrator writes is deterministic, count-bearing,
  and free of any identifier, URL, hostname, or provider text.

What is deliberately **not** proved here: that the *pipeline* stops at a boundary.
That is the orchestrator's contract and is covered in
``test_research_orchestration.py``.
"""

from __future__ import annotations

import pytest

from app.domain.research.outcome import CooperativeDeadline, RunOutcomeKind


class _Clock:
    """A controllable monotonic clock, as the injectable seam intends."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- the deadline contract ---------------------------------------------------


class TestCooperativeDeadline:
    def test_unbounded_never_expires(self):
        clock = _Clock()
        deadline = CooperativeDeadline.unbounded(clock=clock)

        clock.advance(10_000_000)
        assert deadline.expired() is False
        assert deadline.remaining() is None
        assert deadline.elapsed() == 0.0, "an unbounded deadline reports no elapsed work"

    def test_no_budget_configuration_means_no_deadline(self):
        """The documented consequence of the blocked numeric value.

        No measurement exists (audit § S.7), so the deadline is *not* given a number
        to fall back on. A run with no cooperative budget is a state the audit
        records, and this assertion is what stops a default from appearing quietly.
        """
        for value in (None, 0, 0.0, -1):
            clock = _Clock()
            deadline = CooperativeDeadline.from_budget(value, clock=clock)
            clock.advance(999_999)
            assert deadline.expired() is False, f"budget={value!r} should be unbounded"

    def test_expiry_is_reported_never_raised(self):
        """The property that distinguishes this from ``asyncio.timeout``.

        AD-045 requires the pipeline to persist what it has, so the deadline must not
        unwind the caller. Raising nothing and cancelling nothing is what leaves the
        orchestrator free to decide where to stop.
        """
        clock = _Clock()
        deadline = CooperativeDeadline.from_budget(5.0, clock=clock)
        clock.advance(5.0)

        # Reporting the fact is the entire contract; both calls are plain.
        assert deadline.expired() is True
        assert deadline.expired() is True, "expiry must be idempotent, not one-shot"

    def test_the_boundary_itself_is_not_expired(self):
        clock = _Clock()
        deadline = CooperativeDeadline.from_budget(30.0, clock=clock)
        clock.advance(29.999)
        assert deadline.expired() is False
        clock.advance(0.001)
        assert deadline.expired() is True

    def test_the_remaining_time_is_bounded_below(self):
        clock = _Clock()
        deadline = CooperativeDeadline.from_budget(10.0, clock=clock)
        assert deadline.remaining() == pytest.approx(10.0)
        clock.advance(13.0)
        assert deadline.remaining() == 0.0
        assert deadline.expired() is True

    def test_a_backwards_clock_cannot_resurrect_an_expired_deadline(self):
        """A wall-clock adjustment cannot launder time the run already spent.

        Latching is the property, and it is deliberate. ``elapsed`` is clamped at
        zero and the budget is fixed, so without a latch a clock that jumps
        backwards would report the *whole* budget as remaining again — and a run
        that had spent its time would start spending it a second time. Once the
        budget is gone it stays gone: every stage that asked "may I continue?" gets
        the same answer for the rest of the run.
        """
        clock = _Clock()
        deadline = CooperativeDeadline.from_budget(10.0, clock=clock)
        clock.advance(11.0)
        assert deadline.expired() is True

        # A jump backwards, past the start and far past the end, changes nothing.
        clock.now = -1_000.0
        assert deadline.expired() is True
        assert deadline.remaining() == 0.0


# --- the outcome contract ----------------------------------------------------


class TestRunOutcomeKind:
    def test_every_outcome_maps_to_a_persisted_status(self):
        """The mapping is total: no outcome is unrepresentable in the schema.

        § R.5 records that no ``cancelled`` state exists and that adding one would
        be a schema change. This assertion is what keeps that statement true: the
        five outcomes cover the CHECK constraint's five values through two
        two-to-one collapses, and nothing falls off the end.
        """
        expected = {
            RunOutcomeKind.COMPLETED: "completed",
            RunOutcomeKind.PARTIAL: "partial",
            RunOutcomeKind.FAILED: "failed",
            RunOutcomeKind.NO_EVIDENCE: "failed",
            RunOutcomeKind.INTERRUPTED: "completed",
        }
        for kind, status in expected.items():
            assert kind.run_status() == status

    def test_no_outcome_maps_outside_the_status_check(self):
        """Guards the ``run_status_values`` CHECK, not just a name.
        """
        allowed = {"queued", "running", "completed", "partial", "failed"}
        for kind in RunOutcomeKind:
            assert kind.run_status() in allowed

    def test_the_terminal_failures_share_a_status_by_design(self):
        """``NO_EVIDENCE`` is not ``FAILED`` — but it writes the same status.

        A caller that needs to distinguish them must classify before it writes, not
        read the row back. The message and the summary carry the difference, which
        is the split the schema already supports and the one § R.10 asks for.
        """
        assert (
            RunOutcomeKind.NO_EVIDENCE.run_status()
            == RunOutcomeKind.FAILED.run_status()
        )
        assert RunOutcomeKind.NO_EVIDENCE is not RunOutcomeKind.FAILED


# --- the failure summary -----------------------------------------------------


class TestFailureSummary:
    """The run-level degradation record (§ R.10, FR-42, SR-12).

    ``failure_summary`` is free text, so the only way to know it is safe is to
    constrain what the orchestrator puts in it. These tests pin the shape: counts
    and stage words, no URL, no hostname, no provider text, no identifier.
    """

    def test_counts_only_and_stage_words(self):
        from app.domain.research.outcome import summarize_run

        summary = summarize_run(
            outcome=RunOutcomeKind.PARTIAL,
            candidates=9,
            fetched=6,
            extracted=4,
            unusable=2,
            excluded=1,
            selected=3,
            claims=5,
            citations=5,
        )
        assert "candidates 9" in summary
        assert "fetched 6" in summary
        assert "extracted 4" in summary
        assert "excluded 1" in summary
        assert "selected 3" in summary
        assert "degraded" in summary, "a partial run must say that it degraded"

    def test_no_url_hostname_or_identifier_reaches_the_summary(self):
        """SR-12 / FR-42: the summary is the run-level record a user may see.

        A hostname is internal network topology and a URL is attacker-supplied, so
        neither appears in the text that lands in ``research_runs.failure_summary``.
        """
        from app.domain.research.outcome import summarize_run

        summary = summarize_run(
            outcome=RunOutcomeKind.PARTIAL,
            candidates=3,
            fetched=1,
            extracted=0,
            unusable=1,
            excluded=0,
            selected=0,
            claims=0,
            citations=0,
        )
        for forbidden in ("http", "://", ".com", "example", "provider", "LLM", "sql"):
            assert forbidden not in summary.lower(), f"{forbidden!r} leaked into the summary"

    def test_the_summary_names_the_failure_the_outcome_says(self):
        from app.domain.research.outcome import summarize_run

        assert "time budget" in summarize_run(
            outcome=RunOutcomeKind.INTERRUPTED,
            candidates=1,
            fetched=0,
            extracted=0,
            unusable=0,
            excluded=0,
            selected=0,
            claims=0,
            citations=0,
        ).lower()
        assert "no usable evidence" in summarize_run(
            outcome=RunOutcomeKind.NO_EVIDENCE,
            candidates=1,
            fetched=0,
            extracted=0,
            unusable=0,
            excluded=0,
            selected=0,
            claims=0,
            citations=0,
        ).lower()

    def test_the_summary_of_a_clean_run_says_nothing_degraded(self):
        """The word "degraded" appears only when the run actually degraded."""
        from app.domain.research.outcome import summarize_run

        clean = summarize_run(
            outcome=RunOutcomeKind.COMPLETED,
            candidates=2,
            fetched=2,
            extracted=2,
            unusable=0,
            excluded=0,
            selected=1,
            claims=1,
            citations=1,
        )
        assert "degraded" not in clean

        degraded = summarize_run(
            outcome=RunOutcomeKind.PARTIAL,
            candidates=2,
            fetched=2,
            extracted=2,
            unusable=0,
            excluded=0,
            selected=1,
            claims=1,
            citations=1,
        )
        assert "degraded" in degraded
