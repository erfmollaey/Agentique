"""ResearchRun lifecycle policy (Phase 3, P3.9-B, B4).

Explicit, testable state-machine rules for ResearchRun status transitions.

The contract is derived from the frozen Phase 3 status set
(ResearchRunStatus: queued, running, completed, partial, failed) and the
frozen lifecycle described in PHASE-03-RESEARCH-ENGINE.md § 15.1:

    accepted → queued → running → completed | partial | failed

No new statuses are introduced. No transition out of a terminal status is
permitted by this policy. The policy is separate from orchestration concerns
(B5 deadline policy, B7 retry/restart guarantees) — it only validates that a
requested transition is representable by the current phase contract.

Ownership and concurrency are enforced at the repository layer, not here.
This module only decides *whether* a transition is allowed; the repository
decides *whether it actually happened* (atomic, ownership-scoped, row-locked
where needed).
"""

from __future__ import annotations

import enum

from app.domain.research.types import ResearchRunStatus

#: Allowed transitions, derived from the frozen Phase 3 lifecycle contract.
#: Each key is a source status; each value is the set of statuses the source
#: may transition to. Terminal statuses have no outgoing edges.
_ALLOWED_TRANSITIONS: dict[ResearchRunStatus, frozenset[ResearchRunStatus]] = {
    ResearchRunStatus.QUEUED: frozenset({
        ResearchRunStatus.RUNNING,
        ResearchRunStatus.FAILED,
    }),
    ResearchRunStatus.RUNNING: frozenset({
        ResearchRunStatus.COMPLETED,
        ResearchRunStatus.PARTIAL,
        ResearchRunStatus.FAILED,
    }),
}

#: Human-readable description of each allowed transition, for logs and tests.
_TRANSITION_DESCRIPTIONS: dict[tuple[ResearchRunStatus, ResearchRunStatus], str] = {}


def _build_descriptions() -> None:
    """Populate _TRANSITION_DESCRIPTIONS from _ALLOWED_TRANSITIONS."""
    for src, dsts in _ALLOWED_TRANSITIONS.items():
        for dst in dsts:
            _TRANSITION_DESCRIPTIONS[(src, dst)] = _describe(src, dst)


def describe(kind: TransitionErrorKind) -> str | None:
    """Public helper for the ownership-mismatch description (used by tests)."""
    return ResearchRunLifecycle.describe(kind)


def _describe(src: ResearchRunStatus, dst: ResearchRunStatus) -> str:
    pairs: dict[tuple[ResearchRunStatus, ResearchRunStatus], str] = {
        (ResearchRunStatus.QUEUED, ResearchRunStatus.RUNNING): "run started",
        (ResearchRunStatus.QUEUED, ResearchRunStatus.FAILED): "run failed before starting",
        (ResearchRunStatus.RUNNING, ResearchRunStatus.COMPLETED): "run completed with verified answer",
        (ResearchRunStatus.RUNNING, ResearchRunStatus.PARTIAL): "run completed partially",
        (ResearchRunStatus.RUNNING, ResearchRunStatus.FAILED): "run failed",
    }
    return pairs.get((src, dst), f"transition from {src.value} to {dst.value}")


_build_descriptions()


class TransitionErrorKind(enum.StrEnum):
    """Closed set of reasons a transition was refused."""

    INVALID_TRANSITION = "invalid_transition"
    """The requested transition is not in the allowed set."""

    TERMINAL_STATE = "terminal_state"
    """The run is already in a terminal status and cannot be reopened."""

    SAME_STATE = "same_state"
    """The requested status is the current status."""

    OWNERSHIP_MISMATCH = "ownership_mismatch"
    """The caller does not own the run being transitioned."""

    STATELESS = "stateless"
    """A stateless validator cannot certify a stateful ownership check."""


class TransitionError(Exception):
    """A requested lifecycle transition was refused.

    Carries the kind, the current status, the requested status, and a message
    safe for logs. Unlike a raw ValueError, this type is catchable by name and
    the kind is aggregateable by an orchestrator (FR-42).The owner identity is
    included only when the refusal is an ownership mismatch.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: TransitionErrorKind,
        current_status: ResearchRunStatus,
        requested_status: ResearchRunStatus,
        owner_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.current_status = current_status
        self.requested_status = requested_status
        self.owner_id = owner_id


class TransitionResult:
    """The outcome of a validated transition attempt.

    The repository reports one of these after attempting the atomic, ownership-
    scoped update. A ``TransitionResult.accepted`` means the transition was
    allowed by policy *and* the row was updated. A ``rejected`` means the
    transition was refused by policy (before any database write).
    """

    def __init__(
        self,
        *,
        accepted: bool,
        error: TransitionError | None = None,
    ) -> None:
        self.accepted = accepted
        self.error = error

    @property
    def is_accepted(self) -> bool:
        return self.accepted

    @property
    def is_rejected(self) -> bool:
        return not self.accepted

    @classmethod
    def accepted_result(cls) -> TransitionResult:
        return TransitionResult(accepted=True)

    @classmethod
    def rejected_result(
        cls,
        kind: TransitionErrorKind,
        current_status: ResearchRunStatus,
        requested_status: ResearchRunStatus,
        *,
        owner_id: int | None = None,
    ) -> TransitionResult:
        message = _rejection_message(
            kind, current_status, requested_status, owner_id=owner_id
        )
        return TransitionResult(
            accepted=False,
            error=TransitionError(
                message,
                kind=kind,
                current_status=current_status,
                requested_status=requested_status,
            ),
        )


def _rejection_message(
    kind: TransitionErrorKind,
    current_status: ResearchRunStatus,
    requested_status: ResearchRunStatus,
    *,
    owner_id: int | None = None,
) -> str:
    if kind is TransitionErrorKind.TERMINAL_STATE:
        return (
            f"cannot transition run from terminal status "
            f"{current_status.value} to {requested_status.value}"
        )
    if kind is TransitionErrorKind.SAME_STATE:
        return (
            f"run is already in status {current_status.value}; "
            f"no transition to {requested_status.value} is needed"
        )
    if kind is TransitionErrorKind.INVALID_TRANSITION:
        return (
            f"transition from {current_status.value} to {requested_status.value} "
            f"is not permitted by the current lifecycle contract"
        )
    if kind is TransitionErrorKind.OWNERSHIP_MISMATCH:
        owner_phrase = (
            f"the requested owner id {owner_id}"
            if owner_id is not None
            else "the requested owner"
        )
        return (
            f"cannot transition research run {current_status.value}: "
            f"{owner_phrase} does not match the run's owner"
        )
    if kind is TransitionErrorKind.STATELESS:
        return (
            "stateless lifecycle validator cannot certify a stateful check "
            "such as ownership or the database write itself"
        )
    return f"transition refused: {kind}"


class ResearchRunLifecycle:
    """Stateless lifecycle validator for ResearchRun status transitions.

    Pure function, no I/O, no infrastructure. The same policy instance is used
    by the repository (to validate before writing) and by tests (to assert the
    contract independently of any database).

    This validator only models the stateless status contract: `queued
    -> running/failed`, `running -> completed/partial/failed`, and no
    transition out of a terminal state. The repository carries the remaining
    stateful rules: ownership and the concrete database effect. The two are
    deliberately kept separate so the status policy can be tested without a
    database and the ownership check can be tested atomically with the write.
    """

    @staticmethod
    def validate(
        current_status: ResearchRunStatus,
        requested_status: ResearchRunStatus,
    ) -> TransitionResult:
        """Return the policy outcome for a proposed transition.

        Does **not** touch the database. The caller (repository or service) is
        responsible for making the transition atomic and ownership-scoped.
        """
        if current_status is requested_status:
            return TransitionResult.rejected_result(
                TransitionErrorKind.SAME_STATE,
                current_status,
                requested_status,
                owner_id=None,
            )

        if current_status.terminal:
            return TransitionResult.rejected_result(
                TransitionErrorKind.TERMINAL_STATE,
                current_status,
                requested_status,
                owner_id=None,
            )

        allowed = _ALLOWED_TRANSITIONS.get(current_status)
        if allowed is None or requested_status not in allowed:
            return TransitionResult.rejected_result(
                TransitionErrorKind.INVALID_TRANSITION,
                current_status,
                requested_status,
                owner_id=None,
            )

        return TransitionResult.accepted_result()

    @staticmethod
    def describe_transition(
        current_status: ResearchRunStatus, requested_status: ResearchRunStatus
    ) -> str | None:
        """Human-readable description of an allowed transition, or None."""
        return _TRANSITION_DESCRIPTIONS.get((current_status, requested_status))

    @staticmethod
    def describe(kind: TransitionErrorKind) -> str | None:
        """Human-readable description of a refusal kind, or None.

        This is intentionally stateless: it describes a refusal *kind*, not
        a refused transition that included an owner id.
        """
        if kind is TransitionErrorKind.OWNERSHIP_MISMATCH:
            return "caller does not own the run being transitioned"
        return None

    @classmethod
    def allowed_from(cls, status: ResearchRunStatus) -> frozenset[ResearchRunStatus]:
        """The set of statuses ``status`` may transition to, per the contract."""
        if status.terminal:
            return frozenset()
        return _ALLOWED_TRANSITIONS.get(status, frozenset())
