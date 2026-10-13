"""Research run outcome and cooperative deadline (Phase 3, P3.9-D — AD-045).

Two pure contracts, both of them things the orchestrator must decide but must not
invent at the point of use:

* :class:`RunOutcomeKind` — the closed set a terminal research run can be classified
  into. § 15.9 requires the failure categories to be *distinguishable* (FR-43) and
  § 15.1 requires exactly one terminal message that says which of them happened. Five
  values, and each corresponds to something a user can act on.
* :class:`CooperativeDeadline` — the AD-045 mechanism. A clock-reading value object,
  not a signal, not a task, not an ``asyncio`` primitive: the orchestrator asks it
  whether time has run out at a **safe boundary**, and the answer is a plain fact
  that any code path can consult and act on without unwinding a coroutine.

**Why a value object rather than ``asyncio.timeout``.** ``asyncio.timeout`` cancels
the enclosing cancellation scope — which is right for "stop waiting for this
provider call", and the fetch stage already uses it for its own total deadline
(AD-031, § M.4). It is the wrong tool for "stop the *pipeline* and write what you
have", because the pipeline must keep running long enough to *persist* a partial
result. A deadline that cancels the very transaction needed to record the outcome
cannot produce one. This type therefore only reports; the orchestrator decides what
to stop, and where.

**No number is invented here.** The deadline value is injected from configuration and
defaults to *no deadline at all*, because no measurement exists (audit § S.7) and a
number chosen to look deliberate would be unmeasurable. A run with no cooperative
deadline is an honest, louder state than a run with a made-up one: it is bounded by
the per-provider timeouts, the candidate bound, and Celery's own backstop, and it says
so in the audit rather than pretending.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic


class RunOutcomeKind(enum.StrEnum):
    """The terminal classification of one research run (§ 15.1, FR-41 … FR-44).

    Frozen at five values because the notification layer has to tell them apart and
    the lifecycle has to be able to write each one:

    ``COMPLETED``
        A verified answer was synthesized and nothing degraded. Written to
        ``completed``.
    ``PARTIAL``
        A verified answer was synthesized, but the run recorded degradation — some
        candidate failed to fetch or extract, some source fell below the quality
        threshold, or some verified evidence did not fit the synthesis budget.
        Written to ``partial``.
    ``NO_EVIDENCE``
        The run reached its stages and produced **no** verified evidence worth an
        answer: nothing was retrieved, every source failed, or every candidate
        claim was rejected. Not the same as ``FAILED`` — nothing went wrong that a
        retry would fix, and the honest answer is "I found nothing I could stand
        behind" (FR-41, SR-12). Written to ``failed`` with a distinct message.
    ``FAILED``
        A stage could not run: planning refused, a provider was unreachable, the
        answer failed citation verification, or persistence broke. Written to
        ``failed`` with a distinct message.
    ``INTERRUPTED``
        The cooperative deadline expired. Written to ``partial`` when verified
        evidence already exists, and to ``failed`` with ``NO_EVIDENCE`` semantics
        when it does not (AD-045, § 15.9 "Cancellation/deadline: N,
        F-with-partial-state").

    A second dimension is recorded per run in ``failure_summary``: *what* degraded
    and how much. The kind says whether to tell the user; the summary says what
    actually happened, in counts rather than in URLs, hostnames, or provider text.
    """

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    NO_EVIDENCE = "no_evidence"
    INTERRUPTED = "interrupted"

    def run_status(self) -> str:
        """The ``ResearchRunStatus`` this outcome writes to storage.

        ``INTERRUPTED`` has no status of its own — no ``cancelled`` state exists,
        and adding one is a schema change the contract does not ask for (§ R.5). It
        is recorded as ``partial`` (with, or without, an answer) or ``failed``, and
        the distinction is carried in ``failure_summary``.

        ``COMPLETED`` and ``PARTIAL`` map to themselves, and ``FAILED`` and
        ``NO_EVIDENCE`` both map to ``failed`` — the two differ in the message and
        in the summary, not in the status, because a user cares which of them
        happened and the schema's CHECK deliberately carries no more.
        """
        if self is RunOutcomeKind.PARTIAL:
            return "partial"
        if self is RunOutcomeKind.FAILED or self is RunOutcomeKind.NO_EVIDENCE:
            return "failed"
        return "completed"


@dataclass(slots=True)
class _LatchedExpiry:
    """One-shot record that this deadline's budget was spent.

    A mutable field inside an otherwise frozen value object, which is the point:
    expiry is a fact about a *run* rather than about the clock at the moment of
    asking, so it has to persist across readings. Frozen would mean ``replace()``,
    which every caller would have to remember to do. The field is written once, from
    a single method, and is never read back as caller-visible state — the class is
    private to this module and has no other accessor.
    """

    spent: bool = False


@dataclass(frozen=True, slots=True)
class CooperativeDeadline:
    """A cooperative run deadline: a start time, a budget, and a clock (AD-045).

    Clock-dependent inputs are parameters, never ambient calls, for the same reason
    P3.5's ranking policy takes ``reference_time`` rather than reading
    ``datetime.now()``: a deadline that cannot be controlled in a test is a deadline
    whose behaviour is asserted by accident.

    Four properties follow from the AD-045 decision:

    * **Cheap and non-mutating.** :meth:`expired` reads a counter and compares. It
      does not raise, does not cancel, does not touch the event loop, and may be
      called at any point — including between two statements of the same stage,
      which is exactly where the pipeline uses it.
    * **It latches.** Once the budget is gone, it stays gone. ``elapsed`` is clamped
      at zero and the budget is fixed, so without a latch a clock that jumped
      backwards would report the whole budget as remaining again and let a run spend
      its time twice. Latching makes the one property a deadline exists to guarantee
      — that a run does not get more of a finite budget than it was given — true
      even under a clock adjustment.
    * **Unbounded means unbounded.** :meth:`unbounded` builds a deadline that never
      expires; the orchestrator treats an unconfigured budget as "run to the
      provider timeouts and the candidate bound" rather than silently substituting
      a default. See the audit § S.7 for why no number is chosen here.
    * **The report is the caller's.** This type does not know what a partial answer
      is, what a stage is, or how to write ``ResearchRunStatus.PARTIAL``. It says
      whether time has run out; the orchestrator, which owns sequencing and outcome
      classification, decides what that means.
    """

    budget_seconds: float | None
    started_at: float
    _clock: Callable[[], float] = monotonic
    _spent: _LatchedExpiry = field(default_factory=_LatchedExpiry)

    @classmethod
    def unbounded(cls, *, clock: Callable[[], float] = monotonic) -> CooperativeDeadline:
        """The deadline used when no budget is configured.

        Not an accident and not a workaround: no defensible number exists (§ S.7),
        so the pipeline runs without one and the audit records that it does. A run
        with an unbounded deadline is still bounded — by ``FETCH_TOTAL_TIMEOUT_
        SECONDS``, ``SEARCH_TIMEOUT_SECONDS``, the LLM's own timeout, and the
        candidate bound of AD-047 — but not by a *pipeline* budget, and it must not
        be described as though it were.
        """
        return cls(budget_seconds=None, started_at=clock(), _clock=clock)

    @classmethod
    def from_budget(
        cls, budget_seconds: float | None, *, clock: Callable[[], float] = monotonic
    ) -> CooperativeDeadline:
        """A deadline of ``budget_seconds``, or unbounded when it is ``None``/0."""
        return cls(
            budget_seconds=budget_seconds if budget_seconds and budget_seconds > 0 else None,
            started_at=clock(),
            _clock=clock,
        )

    def elapsed(self) -> float:
        """Seconds consumed so far, per the injected clock."""
        if self.budget_seconds is None:
            return 0.0
        return max(self._clock() - self.started_at, 0.0)

    def remaining(self) -> float | None:
        """Seconds left, or ``None`` when unbounded.

        ``0.0`` once the budget is spent — including after the latch — so a caller
        that reads the number rather than the flag cannot mistake an expired run for
        a run with time to spare.
        """
        if self.budget_seconds is None:
            return None
        if self._spent.spent:
            return 0.0
        return max(self.budget_seconds - self.elapsed(), 0.0)

    def expired(self) -> bool:
        """Whether the run's time budget is spent, latching the first time it is.

        ``False`` for an unbounded deadline, always. Hence "no deadline" means every
        stage may start, and the only remaining protections are the per-provider
        timeouts and the work bound — a state the audit records explicitly.
        """
        if self._spent.spent:
            return True
        remaining = self.remaining()
        if remaining is not None and remaining <= 0.0:
            self._spent.spent = True
            return True
        return False


__all__ = ["CooperativeDeadline", "RunOutcomeKind", "summarize_run"]


def summarize_run(
    *,
    outcome: RunOutcomeKind,
    candidates: int = 0,
    fetched: int = 0,
    extracted: int = 0,
    unusable: int = 0,
    excluded: int = 0,
    selected: int = 0,
    claims: int = 0,
    citations: int = 0,
) -> str:
    """A free-text, count-bearing, non-revealing run record (FR-42, § R.10).

    P3.9's § R.10 froze the shape of this record *before* the implementation
    existed: what the schema already holds are ``research_runs.failure_summary``
    (free text) plus per-source ``status``/``failure_reason``; no structured
    counter column exists and AD-034 does not add one. Free text is therefore not a
    shortcut here — it is the only thing the frozen model permits — and the
    discipline is to make the text a *report* rather than a comment: fixed words,
    fixed order, counts only.

    **What it contains.** The number of candidates the plan produced, how many were
    fetched, how many extracted, how many were unusable or excluded below the
    quality threshold, how many were selected for evidence, and how many claims and
    citations the run produced. Plus one phrase naming the outcome class.

    **What it does not contain**, deliberately and irreversibly:

    * no URL and no hostname — a URL is attacker-supplied and a hostname is internal
      network topology, and this column is read back for run history;
    * no provider text, status code, or transport message — the domain error
      families are written so their text is safe, but a *summary* that copied them
      would become a second place that leaks if an adapter ever changed;
    * no source id, run id, or claim id — identifiers are internal, and a user has
      no way to act on them;
    * no model prose of any kind.

    The outcome phrase is what makes the row self-describing after the fact: the
    same counts read as "degraded" for ``PARTIAL``, "no usable evidence" for
    ``NO_EVIDENCE``, and "stopped at the configured time budget" for
    ``INTERRUPTED`` — the one that has no status of its own (§ R.5). That is the
    honest-completeness property SR-12 asks for, bought without a column.
    """
    head = f"candidates {candidates}, fetched {fetched}, extracted {extracted}, "
    middle = f"unusable {unusable}, excluded {excluded}, selected {selected}, "
    tail = f"claims {claims}, citations {citations}"

    verdict = {
        RunOutcomeKind.COMPLETED: "completed",
        RunOutcomeKind.PARTIAL: "completed with degraded sources",
        RunOutcomeKind.FAILED: "failed",
        RunOutcomeKind.NO_EVIDENCE: "failed with no usable evidence",
        RunOutcomeKind.INTERRUPTED: "stopped at the configured time budget",
    }[outcome]
    return f"summary: {head}{middle}{tail}; result: {verdict}"
