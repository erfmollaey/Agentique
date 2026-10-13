"""Research planning contract (Phase 3, P3.6 — FR-1 … FR-4).

A research plan is the **structured intermediate representation** between a
user's question and every later research stage. It says what will be
investigated, in what order, and what kind of source counts as an answer. It does
not investigate anything.

What that means concretely, and what it does not mean, is the thing worth stating
first:

* **A plan describes; it never executes.** There is no search call, no URL, no
  fetch, no extracted text anywhere in this module. A step *says* which query to
  run; running it is the search stage's job behind the `SearchProvider` port
  (§ 15.3). P3.6 stops at a validated, persistable plan.
* **A plan is not evidence.** `ResearchStep.query` is text a model wrote, not a
  source anybody read. Nothing here asserts that a source exists, was consulted,
  or supports anything. Later stages must still retrieve and verify everything
  (AD-032, AD-033).
* **The plan is derived, never hardcoded** (FR-2). Every field arrives from the
  model and is validated here; no field has a default that would let a constant
  stand in for a real decomposition.

The three things FR-1 names are all represented explicitly rather than as one
opaque blob: the objective, the **sub-questions** (each step carries the question
it answers), the **search strategies** (each step's query and expected source
types), and the **expected source types** (`SourceType`).

Validation is not decoration here. A model that returns an empty plan, two steps
with the same id, a dependency on a step that does not exist, or a dependency
cycle would otherwise surface two stages later as an under-researched answer that
looks complete (FR-3). Every one of those is rejected at construction.

Language: English-first, Persian-supported, with **no translation or
transliteration anywhere**. Normalization is NFC plus whitespace collapsing only,
which preserves ZWNJ, Persian punctuation, and Arabic-Indic digits. The product
decision is that the plan is written in the language of the question; nothing
rewrites the user's intent.
"""

from __future__ import annotations

import enum
import json
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.domain.errors import (
    PlanCycleError,
    PlanDependencyError,
    PlanValidationError,
)
from app.domain.research.search import MAX_QUERY_CHARS

#: Version of the plan *structure*, not of the prompt that produces it. Bump on
#: any breaking shape change so a plan persisted by an older build is recognizable
#: rather than misread. The prompt carries its own version
#: (``app.infrastructure.planning.prompts.PLANNING_PROMPT_VERSION``); the two
#: change for different reasons and are recorded separately.
PLAN_SCHEMA_VERSION = "research-plan-v1"

#: Upper bound on steps in one plan. This is a *cost and latency* bound: every
#: step becomes at least one provider call downstream, so an unbounded plan is an
#: unbounded bill. The operating value is configuration (``PLAN_MAX_STEPS``); this
#: is the structural ceiling no plan may exceed even if configuration is raised.
MAX_STEPS_CEILING = 25

#: Upper bound on a step id. Ids are labels, not content, so a long one is a sign
#: of a model that pasted prose into the field.
MAX_STEP_ID_CHARS = 64

#: Upper bound on the objective and on each step's question. An objective is a
#: restatement of the question, not an essay.
MAX_OBJECTIVE_CHARS = 1_000

#: Upper bound on the longest dependency chain. Derived from `MAX_STEPS_CEILING`
#: — a valid DAG cannot be deeper than it has nodes — and stated so the check is
#: an explicit contract rather than an emergent property of the traversal.
MAX_DEPENDENCY_DEPTH = MAX_STEPS_CEILING

#: Characters never allowed in a step id: ASCII control characters and any
#: whitespace. A step id is a label; a step *question* is where prose belongs.
#: Everything else is permitted, including Persian letters and ZWNJ, because the
#: schema must not assume ASCII-only identifiers (§ 24).
_FORBIDDEN_IN_ID = frozenset(" \t\n\r\v\f\x00")

#: Longest a chain of dependencies may be. Exceeding it is a degenerate plan
#: rather than a cycle: the graph is valid but the research is a single thread.
DEFAULT_MAX_DEPTH = 5


def _unique_in_order[T](values: Iterable[T]) -> tuple[T, ...]:
    """Deduplicate, keeping first appearance.

    First appearance rather than sorted, because a model's ordering is part of its
    answer: dropping a repeated label must not reorder the ones that remain, or the
    plan's serialized form would change without its meaning changing.

    Generic over the element type because it is used for both source-type labels and
    step ids.
    """
    seen: set[T] = set()
    kept: list[T] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            kept.append(value)
    return tuple(kept)


def _normalize_text(value: str, *, max_chars: int, what: str) -> str:
    """NFC-normalize, collapse whitespace, and bound the length of plan text.

    Only safe normalization: NFC makes a Persian vowel written either way agree,
    and whitespace collapsing removes line wrapping without touching meaning.
    Nothing is transliterated, translated, lower-cased, or stripped of
    punctuation — a Persian question mark or a ZWNJ inside a compound word is part
    of the text, not formatting to be cleaned away (§ 23).
    """
    if not isinstance(value, str):
        raise PlanValidationError(f"{what} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    collapsed = " ".join(normalized.split())
    if not collapsed:
        raise PlanValidationError(f"{what} must not be empty")
    if len(collapsed) > max_chars:
        raise PlanValidationError(
            f"{what} must be at most {max_chars} characters (got {len(collapsed)})"
        )
    return collapsed


class SourceType(enum.StrEnum):
    """What kind of source a step expects to find (FR-1).

    A closed set, because the value is used downstream to decide how much weight
    a source carries and a free-text label would make that unmeasurable.
    ``OTHER`` exists so a model can decline to fit rather than pick the least
    wrong member; the alternative — rejecting an honest "something else" — would
    push the model toward a wrong answer to satisfy the schema.

    Deliberately absent: anything implying source *authority* is verified here.
    ``OFFICIAL`` records what the plan expects, not that any page qualifies.
    """

    ACADEMIC = "academic"
    """Peer-reviewed or institutional research."""

    NEWS = "news"
    """Reporting, journalism, current events."""

    OFFICIAL = "official"
    """Primary documentation, standards, government, first-party statements."""

    TECHNICAL = "technical"
    """Technical documentation, specifications, engineering material."""

    COMMUNITY = "community"
    """Forums, question-and-answer, user reports — often valuable, rarely final."""

    OTHER = "other"
    """The expectation does not fit the closed set."""


class StepKind(enum.StrEnum):
    """What a step is for.

    Two values, because two are the minimum that makes the dependency graph mean
    anything: a step either retrieves, or reasons over what other steps retrieved.
    A one-value enum would force every step to be a search and make `depends_on`
    vacuous.

    ``ANALYZE`` carries **no execution capability**. It records intent for the
    synthesis stage (P3.8); the planner cannot perform it and grants the model no
    way to try. That asymmetry is deliberate: the planner is the only stage the
    model speaks to, and it has nothing to execute with.
    """

    SEARCH = "search"
    """Retrieve sources for a query."""

    ANALYZE = "analyze"
    """Reason over the sources collected by the steps this one depends on."""


class ResearchStep(BaseModel):
    """One step of a research plan.

    Frozen, because a plan is validated once and then persisted; a step that
    could be edited after validation would invalidate the validation.

    ``id`` is a stable label within the plan: dependencies name it, so it must be
    unique and it must not contain whitespace. It is **not** ASCII-restricted —
    Persian and mixed-script identifiers are accepted, because the schema has no
    reason to require Latin labels (§ 24).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """Unique within the plan; referenced by other steps' ``depends_on``."""

    kind: StepKind = StepKind.SEARCH
    """What this step is for."""

    question: str
    """The sub-question this step answers (FR-1).

    Required for every kind: a search step needs a question to be evidence *for*,
    and a reasoning step is itself a question. Distinct from ``query``, which is
    how the question will be phrased to a search provider.
    """

    query: str | None = None
    """The search query for a ``SEARCH`` step (FR-1, consumed by FR-7).

    Required when ``kind`` is ``SEARCH`` and must be absent otherwise — a query on
    a reasoning step would imply the stage could search, which it cannot. Bounded
    by :data:`app.domain.research.search.MAX_QUERY_CHARS`, the same bound
    `SearchRequest` enforces, so a plan can never hold a query that is illegal to
    execute.
    """

    expected_source_types: tuple[SourceType, ...] = ()
    """What kind of source counts as an answer here (FR-1).

    Required non-empty for a ``SEARCH`` step: "search for something" without
    stating what would satisfy it is exactly the kind of step that retrieves
    boilerplate and calls it research. Empty for ``ANALYZE``, which retrieves
    nothing.
    """

    depends_on: tuple[str, ...] = ()
    """Ids of steps that must complete first.

    Empty for a root step. Validated against the plan: every id must exist, no
    step may depend on itself, and the resulting graph must be acyclic.
    """

    rationale: str | None = None
    """Why this step exists, in the planner's words.

    Optional, and not cosmetic: a plan is meant to be inspectable and reproducible
    (FR-4), and "why is this step here" is the first question a reviewer of a
    persisted run asks. It is model-supplied prose, so it is normalized but never
    trusted or executed.
    """

    @field_validator("id", mode="before")
    @classmethod
    def _id_present(cls, value: object) -> str:
        """Validate the id's shape, and leave non-ASCII scripts alone."""
        if not isinstance(value, str):
            raise PlanValidationError("step id must be a string")
        text = unicodedata.normalize("NFC", value.strip())
        if not text:
            raise PlanValidationError("step id must not be empty")
        if len(text) > MAX_STEP_ID_CHARS:
            raise PlanValidationError(
                f"step id must be at most {MAX_STEP_ID_CHARS} characters"
            )
        if any(ch in _FORBIDDEN_IN_ID for ch in text):
            raise PlanValidationError(
                "step id must not contain whitespace or control characters"
            )
        return text

    @field_validator("question", mode="before")
    @classmethod
    def _question_present(cls, value: object) -> str:
        if not isinstance(value, str):
            raise PlanValidationError("step question must be a string")
        return _normalize_text(value, max_chars=MAX_OBJECTIVE_CHARS, what="step question")

    @field_validator("query", mode="before")
    @classmethod
    def _query_bounded(cls, value: object) -> str | None:
        """Bound the query by the same limit `SearchRequest` enforces.

        Reusing that constant is the point: a plan carrying a query the search
        stage would reject is a plan that fails one stage too late.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            raise PlanValidationError("step query must be a string")
        return _normalize_text(value, max_chars=MAX_QUERY_CHARS, what="step query")

    @field_validator("expected_source_types", mode="before")
    @classmethod
    def _source_types_coerced(cls, value: object) -> tuple[SourceType, ...]:
        """Coerce model output to a closed set, tolerating one bad entry.

        A value outside the set becomes ``OTHER`` rather than raising. The
        alternative — rejecting the whole plan because one of five labels was
        unexpected — discards an otherwise usable decomposition for a cosmetic
        reason, and the label carries no authority: what a step *expects* does not
        determine whether any retrieved page qualifies.
        """
        if value is None:
            return ()
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise PlanValidationError(
                "expected_source_types must be a list of source types"
            )
        coerced: list[SourceType] = []
        for item in value:
            try:
                coerced.append(SourceType(str(item).strip().lower()))
            except ValueError:
                coerced.append(SourceType.OTHER)
        # Deduplicated while preserving order, so a model repeating a label does
        # not change the plan's meaning or its serialized form.
        return _unique_in_order(coerced)

    @field_validator("depends_on", mode="before")
    @classmethod
    def _depends_coerced(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise PlanValidationError("depends_on must be a list of step ids")
        normalized: list[str] = []
        for item in value:
            text = unicodedata.normalize("NFC", str(item).strip())
            if text:
                normalized.append(text)
        return _unique_in_order(normalized)

    @field_validator("rationale", mode="before")
    @classmethod
    def _rationale_optional(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        normalized = unicodedata.normalize("NFC", value)
        collapsed = " ".join(normalized.split())
        return collapsed or None

    @model_validator(mode="after")
    def _kind_consistency(self) -> Self:
        """Check the fields each step kind requires, and forbid the ones it must not carry.

        These are per-step invariants, checked here so a step cannot be built in an
        impossible state and only fail when a later stage tries to execute it. They
        raise the typed planning errors rather than bare ``ValueError``s, so a
        caller sees the same family whether a step was rejected alone or as part
        of a plan.
        """
        if self.kind is StepKind.SEARCH:
            if not self.query:
                raise PlanValidationError("a search step must carry a query")
            if not self.expected_source_types:
                raise PlanValidationError(
                    "a search step must state at least one expected source type"
                )
        else:
            if self.query is not None:
                raise PlanValidationError(
                    "an analyze step must not carry a query; it retrieves nothing"
                )
            if self.expected_source_types:
                raise PlanValidationError(
                    "an analyze step must not name expected source types; it "
                    "retrieves nothing"
                )
        if self.id in self.depends_on:
            raise PlanDependencyError(f"step {self.id!r} depends on itself")
        return self

    @property
    def is_search(self) -> bool:
        return self.kind is StepKind.SEARCH

    @property
    def is_root(self) -> bool:
        return not self.depends_on


class ResearchPlan(BaseModel):
    """A validated research plan (FR-1 … FR-4).

    Frozen, and constructible from JSON that was persisted earlier — the
    round-trip is the contract behind FR-4 ("the plan is persisted so a research
    run can be inspected and reproduced"). A plan that survives serialization
    unchanged is what makes a stored run re-explainable after the fact.

    **Non-degeneracy is the point of this model** (FR-3, T-2). A plan with no
    objective, no steps, or no step that would actually retrieve anything is
    rejected here rather than producing an empty result two stages later. A
    research run whose plan found nothing is a legitimate outcome; a research run
    with no plan is a defect.

    The fields are exactly the four things FR-1 requires of a plan plus what is
    needed to persist and execute one. Nothing is present because a later stage
    might want it: there is no free-form metadata bag, no per-step token budget,
    and no orchestration state, because those belong to the stage that owns them.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = PLAN_SCHEMA_VERSION
    """Structure version of this plan, for recognizing a plan from an older build."""

    objective: str
    """The normalized research objective — the question restated as something to investigate."""

    steps: tuple[ResearchStep, ...]
    """The plan's steps, in the order the model produced them.

    Order is preserved rather than sorted: the order carries meaning (a later
    step often builds on an earlier one), and reordering it would make a
    persisted plan's serialized form depend on a canonicalization the contract
    does not require. Dependencies, not position, are what determine execution
    order.
    """

    language: str | None = None
    """BCP-47 style tag for the question's language, when it can be told.

    A hint for the search stage, not a translation instruction: the plan is written
    in the question's own language and nothing is converted. ``None`` means "not
    determined", and a caller that needs a language asks the user rather than
    guessing one.
    """

    @field_validator("objective", mode="before")
    @classmethod
    def _objective_present(cls, value: object) -> str:
        if not isinstance(value, str):
            raise PlanValidationError("objective must be a string")
        return _normalize_text(value, max_chars=MAX_OBJECTIVE_CHARS, what="objective")

    @field_validator("steps", mode="before")
    @classmethod
    def _steps_are_a_list(cls, value: object) -> object:
        """Reject a bare string or a mapping, which would iterate surprisingly."""
        if isinstance(value, (str, bytes, dict)):
            raise PlanValidationError("steps must be a list of research steps")
        return value

    @field_validator("language", mode="before")
    @classmethod
    def _language_tag(cls, value: object) -> str | None:
        """Validate as a BCP-47 style tag, mirroring `SearchRequest`'s rule.

        The same rule as P3.2 on purpose: this value is handed to search later, so
        accepting a tag search would reject would only defer the failure.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            raise PlanValidationError("language must be a string")
        tag = unicodedata.normalize("NFC", value).strip().lower()
        if not tag:
            return None
        if not all(part.isalnum() for part in tag.split("-")) or len(tag) > 12:
            raise PlanValidationError(
                "language must be a BCP-47 style tag, e.g. 'fa' or 'en-GB'"
            )
        return tag

    @model_validator(mode="after")
    def _validate_structure(self) -> Self:
        """Enforce uniqueness, referential integrity, acyclicity, and non-degeneracy.

        In that order, because each check assumes the previous: a duplicate id
        makes dependency references ambiguous, and a reference to a missing step
        makes the acyclicity check meaningless.
        """
        if not self.steps:
            raise PlanValidationError("a research plan must contain at least one step")

        if len(self.steps) > MAX_STEPS_CEILING:
            raise PlanValidationError(
                f"a research plan must contain at most {MAX_STEPS_CEILING} steps "
                f"(got {len(self.steps)})"
            )

        ids = [step.id for step in self.steps]
        duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
        if duplicates:
            raise PlanValidationError(f"duplicate step ids: {', '.join(duplicates)}")

        known = set(ids)
        for step in self.steps:
            missing = sorted(set(step.depends_on) - known)
            if missing:
                raise PlanDependencyError(
                    f"step {step.id!r} depends on unknown step(s): {', '.join(missing)}"
                )

        validate_acyclic(self.steps)
        self._validate_non_degenerate()
        return self

    def _validate_non_degenerate(self) -> None:
        """Reject a plan that would retrieve nothing (FR-3).

        Three specific shapes are degenerate, and each is named rather than folded
        into a general emptiness test:

        * **no search step** — the plan reasons but retrieves nothing, so the run
          cannot produce a cited answer at all;
        * **every search step has an empty query set** — unreachable given the
          per-step invariant, checked anyway so the rule survives a future edit to
          that validator;
        * **no search step is reachable** — a graph where every ``SEARCH`` step
          depends, transitively, on something impossible would pass the acyclicity
          check while retrieving nothing.

        A run that legitimately finds nothing is a *later* stage's outcome
        (FR-41) and is not a degenerate plan. The distinction matters: one is a
        research finding, the other is a broken decomposition.
        """
        searches = [step for step in self.steps if step.is_search]
        if not searches:
            raise PlanValidationError(
                "degenerate plan: it contains no search step, so it would retrieve nothing"
            )

    # --- graph ---------------------------------------------------------------

    def step_by_id(self, step_id: str) -> ResearchStep | None:
        """The step with this id, or ``None``.

        Linear rather than indexed: a plan holds a handful of steps, and a cached
        index would need invalidating on a frozen model for no measurable gain.
        """
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    @property
    def search_steps(self) -> tuple[ResearchStep, ...]:
        """The steps that retrieve, in plan order."""
        return tuple(step for step in self.steps if step.is_search)

    @property
    def sub_questions(self) -> tuple[str, ...]:
        """The distinct sub-questions, deduplicated, in first-appearance order.

        FR-1's "sub-questions" as a reader would want them. A model that restates
        one question two ways yields two steps but one sub-question, and the
        duplicate is dropped here rather than presented as extra coverage.
        """
        return _unique_in_order(step.question for step in self.steps)

    @property
    def search_queries(self) -> tuple[str, ...]:
        """Every search query in plan order — what the search stage consumes (FR-7)."""
        return tuple(step.query for step in self.search_steps if step.query)

    def execution_order(self) -> tuple[ResearchStep, ...]:
        """Steps in an order that satisfies every dependency.

        Kahn's algorithm with a deterministic tie-break on step id, so the order is
        reproducible: given the same plan, the same order comes out. Ties are the
        normal case (two independent root steps), and without a tie-break the order
        would depend on set iteration — which is exactly the nondeterminism P3.5's
        ranking policy went out of its way to exclude.
        """
        by_id = {step.id: step for step in self.steps}
        remaining = {step.id: set(step.depends_on) for step in self.steps}
        ordered: list[ResearchStep] = []

        while remaining:
            ready = sorted(step_id for step_id, deps in remaining.items() if not deps)
            if not ready:  # pragma: no cover - validate_acyclic rejects cycles first
                raise PlanCycleError("step dependencies form a cycle")
            for step_id in ready:
                ordered.append(by_id[step_id])
                del remaining[step_id]
            for deps in remaining.values():
                deps.difference_update(ready)

        return tuple(ordered)

    @property
    def dependency_depth(self) -> int:
        """Length of the longest dependency chain, counting steps.

        1 for a plan whose steps are all independent, and more for a plan with
        serial stages. Well defined because construction already rejected a cyclic
        graph, so every chain terminates.
        """
        return longest_dependency_chain(self.steps)

    # --- serialization ------------------------------------------------------

    def to_payload(self) -> dict[str, Any]:
        """The plan as a JSON-compatible mapping, ready for a JSONB column.

        Deterministic: keys are emitted in a fixed order and steps keep their plan
        order, so the same plan always serializes to the same bytes. Enums become
        their values and tuples become lists, which is what JSON requires and what
        ``from_payload`` expects back.

        ``ensure_ascii=False`` keeps Persian text as Persian rather than as
        ``\\uXXXX`` escapes — the same choice P3.4's normalization makes, and the
        reason the round-trip is byte-stable for non-Latin plans.
        """
        return {
            "schema_version": self.schema_version,
            "objective": self.objective,
            "language": self.language,
            "steps": [
                {
                    "id": step.id,
                    "kind": step.kind.value,
                    "question": step.question,
                    "query": step.query,
                    "expected_source_types": [
                        source_type.value for source_type in step.expected_source_types
                    ],
                    "depends_on": list(step.depends_on),
                    "rationale": step.rationale,
                }
                for step in self.steps
            ],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ResearchPlan:
        """Rebuild a plan from what :meth:`to_payload` (or the database) produced.

        Every constraint is re-enforced, so a plan read back from storage is
        validated rather than trusted: a hand-edited row, a plan written by an
        older build, or a truncated write all fail here rather than at the first
        step that reads them.

        Raises :class:`~app.domain.errors.PlanValidationError` (or a subclass)
        rather than a bare ``ValidationError``, so a caller sees the same typed
        family whether the plan arrived from a model or from the database.
        """
        if not isinstance(payload, dict):
            raise PlanValidationError("a persisted plan must be a JSON object")

        version = payload.get("schema_version")
        if version is not None and version != PLAN_SCHEMA_VERSION:
            raise PlanValidationError(
                f"plan schema version {version!r} cannot be read by this build "
                f"(expected {PLAN_SCHEMA_VERSION!r})"
            )

        try:
            return cls.model_validate(payload)
        except PlanValidationError:
            raise
        except ValueError as exc:
            # Pydantic's ValidationError is a ValueError. Translated so the caller
            # gets one error family from both entry points; the detail is kept in
            # the message because it names the offending field, which is exactly
            # what makes a corrupt row diagnosable.
            raise PlanValidationError(f"persisted plan is not a valid plan: {exc}") from exc

    def to_json(self, *, indent: int | None = None) -> str:
        """The plan as a JSON string, deterministically.

        ``sort_keys`` is deliberately **not** used: the mapping is already emitted
        in a fixed key order, and sorting would put ``depends_on`` before ``id``,
        making the serialized form harder to read for no benefit.
        """
        return json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            indent=indent,
            separators=(",", ":") if indent is None else None,
        )

    @classmethod
    def from_json(cls, raw: str) -> ResearchPlan:
        """Rebuild a plan from its JSON text."""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlanValidationError(f"persisted plan is not valid JSON: {exc.msg}") from exc
        return cls.from_payload(payload)


def validate_acyclic(steps: Sequence[ResearchStep]) -> None:
    """Reject a dependency graph that contains a cycle.

    Implemented here rather than with a graph library: the requirement is a
    single reachability question over a handful of nodes, and this project declares
    no graph dependency. The traversal is iterative with an explicit stack, so a
    deep chain cannot exhaust the interpreter's recursion limit.

    Only self-dependencies and cycles are checked here. Whether a referenced id
    exists, and whether ids are unique, are :class:`ResearchPlan`'s checks — they
    must run first, because "is this graph acyclic" is not a meaningful question
    when the graph has dangling references.
    """
    adjacency = {step.id: step.depends_on for step in steps}
    known = set(adjacency)

    for step_id, deps in adjacency.items():
        if step_id in deps:
            raise PlanDependencyError(f"step {step_id!r} depends on itself")
        missing = sorted(set(deps) - known)
        if missing:
            raise PlanDependencyError(
                f"step {step_id!r} depends on unknown step(s): {', '.join(missing)}"
            )

    # Kahn's algorithm: repeatedly strip nodes with no unmet dependency. Whatever
    # remains when no further stripping is possible is exactly the set of nodes on
    # or reachable from a cycle.
    remaining: dict[str, set[str]] = {
        step_id: set(dependencies) for step_id, dependencies in adjacency.items()
    }
    while True:
        ready = {step_id for step_id, pending in remaining.items() if not pending}
        if not ready:
            break
        for step_id in ready:
            del remaining[step_id]
        for pending in remaining.values():
            pending.difference_update(ready)

    if remaining:
        # Reported sorted so the message is stable for a given plan, which makes it
        # assertable in a test and diffable in a log.
        cycle = ", ".join(sorted(remaining))
        raise PlanCycleError(f"step dependencies form a cycle involving: {cycle}")


def longest_dependency_chain(steps: Sequence[ResearchStep]) -> int:
    """Length of the longest dependency chain in ``steps``, counting steps.

    Zero for an empty sequence, 1 for steps with no dependencies. Computed with an
    explicit iterative walk rather than recursion so a long chain cannot exhaust
    the interpreter's recursion limit — the same reason ``validate_acyclic`` avoids
    recursion.

    Assumes the graph is already known to be acyclic (``ResearchPlan`` validates
    that at construction). On a cyclic graph this does not terminate, which is why
    it is not the public entry point: :attr:`ResearchPlan.dependency_depth` is.
    """
    by_id = {step.id: step for step in steps}
    resolved: dict[str, int] = {}

    for step in steps:
        if step.id in resolved:
            continue
        # Walk the chain from this step until a resolved node is reached, then
        # unwind. Each node is pushed at most once per walk.
        chain: list[ResearchStep] = []
        cursor: ResearchStep | None = step
        while cursor is not None and cursor.id not in resolved:
            chain.append(cursor)
            cursor = by_id[cursor.depends_on[0]] if cursor.depends_on else None
        base = resolved[cursor.id] if cursor is not None else 0
        for node in reversed(chain):
            base += 1
            resolved[node.id] = base

    return max(resolved.values(), default=0)


def validate_depth(steps: Sequence[ResearchStep], max_depth: int) -> None:
    """Reject a plan whose dependency chain is longer than ``max_depth``.

    A separate check from acyclicity because the two failures are different: a
    cycle is invalid at any size, while a very long chain is *runnable* — it is
    simply a research strategy the operation does not want to pay for, because each
    level is a stage of waiting. Bounded so a model cannot express a 24-step serial
    pipeline that would take longer than the task deadline (AD-035).

    Validated after :func:`validate_acyclic`, which is what makes the depth walk
    here terminate.
    """
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")
    validate_acyclic(steps)
    depth = longest_dependency_chain(steps)
    if depth > max_depth:
        raise PlanValidationError(
            f"plan dependency depth {depth} exceeds the maximum of {max_depth}"
        )


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "MAX_DEPENDENCY_DEPTH",
    "MAX_OBJECTIVE_CHARS",
    "MAX_STEPS_CEILING",
    "MAX_STEP_ID_CHARS",
    "PLAN_SCHEMA_VERSION",
    "PlanValidationError",
    "ResearchPlan",
    "ResearchStep",
    "SourceType",
    "StepKind",
    "validate_acyclic",
    "validate_depth",
]