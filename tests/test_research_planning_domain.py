"""Research planning domain, serialization, and graph validation (P3.6).

Covers FR-1 (decomposition into sub-questions, search strategies, expected source
types), FR-3 (a degenerate plan fails clearly), and the round-trip behind FR-4.
Pure domain: no infrastructure, no model, no database.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.domain.errors import (
    PlanCycleError,
    PlanDependencyError,
    PlanValidationError,
)
from app.domain.research.planning import (
    MAX_STEPS_CEILING,
    PLAN_SCHEMA_VERSION,
    ResearchPlan,
    ResearchStep,
    SourceType,
    StepKind,
    longest_dependency_chain,
    validate_acyclic,
    validate_depth,
)


def search_step(step_id: str = "s1", **overrides) -> ResearchStep:
    defaults = {
        "id": step_id,
        "question": "Which libraries exist?",
        "query": "python async http client",
        "expected_source_types": (SourceType.TECHNICAL,),
    }
    return ResearchStep(**{**defaults, **overrides})


def plan(**overrides) -> ResearchPlan:
    defaults = {
        "objective": "Assess the current state of Python async HTTP clients",
        "steps": (search_step(),),
        "language": "en",
    }
    return ResearchPlan(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# FR-1 — the plan shape
# ---------------------------------------------------------------------------

class TestValidPlan:
    def test_a_minimal_plan_is_accepted(self):
        built = plan()
        assert built.objective
        assert len(built.steps) == 1

    def test_step_kind_defaults_to_search(self):
        assert search_step().kind is StepKind.SEARCH

    def test_plan_exposes_sub_questions(self):
        """FR-1's "sub-questions" as a reader would want them."""
        built = plan(
            steps=(
                search_step("a", question="What exists?"),
                search_step("b", question="How do they compare?"),
            )
        )
        assert built.sub_questions == ("What exists?", "How do they compare?")

    def test_repeated_sub_questions_are_deduplicated(self):
        built = plan(
            steps=(
                search_step("a", question="What exists?"),
                search_step("b", question="What exists?"),
            )
        )
        assert len(built.sub_questions) == 1
        assert len(built.steps) == 2

    def test_plan_exposes_search_queries_for_the_search_stage(self):
        """FR-7 consumes the plan's queries; analyze steps must not appear."""
        built = plan(
            steps=(
                search_step("a", query="first query"),
                ResearchStep(id="b", kind=StepKind.ANALYZE, question="compare",
                             depends_on=("a",)),
            )
        )
        assert built.search_queries == ("first query",)

    def test_every_step_carries_a_rationale_when_supplied(self):
        built = plan(steps=(search_step(rationale="Establish the landscape"),))
        assert built.steps[0].rationale == "Establish the landscape"

    def test_rationale_is_optional(self):
        assert search_step().rationale is None

    def test_language_may_be_absent(self):
        assert plan(language=None).language is None

    def test_schema_version_defaults_to_the_current_one(self):
        assert plan().schema_version == PLAN_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# FR-3 — degeneracy is rejected
# ---------------------------------------------------------------------------

class TestDegeneratePlanRejected:
    def test_missing_objective_is_rejected(self):
        # An absent required field is pydantic's own absence error, not a plan
        # verdict; a *present but empty* objective is the plan verdict.
        with pytest.raises(ValidationError):
            ResearchPlan(steps=(search_step(),))

    def test_empty_objective_is_rejected(self):
        with pytest.raises(PlanValidationError):
            plan(objective="   ")

    def test_whitespace_only_objective_is_rejected(self):
        with pytest.raises(PlanValidationError):
            plan(objective="\n\t  ")

    def test_a_plan_with_no_steps_is_rejected(self):
        with pytest.raises(PlanValidationError):
            plan(steps=())

    def test_a_plan_with_no_search_step_is_degenerate(self):
        """A plan that reasons but never retrieves cannot produce a cited answer."""
        with pytest.raises(PlanValidationError, match="no search step"):
            plan(steps=(ResearchStep(id="a", kind=StepKind.ANALYZE, question="q"),))

    def test_a_plan_of_only_analyze_steps_is_rejected(self):
        with pytest.raises(PlanValidationError):
            plan(
                steps=(
                    ResearchStep(id="a", kind=StepKind.ANALYZE, question="q1"),
                    ResearchStep(id="b", kind=StepKind.ANALYZE, question="q2",
                                 depends_on=("a",)),
                )
            )

    def test_an_empty_step_is_rejected(self):
        with pytest.raises(PlanValidationError):
            plan(steps=(search_step(question=""),))

    def test_a_step_without_a_question_is_rejected(self):
        with pytest.raises(PlanValidationError):
            plan(steps=(search_step(question="   "),))

    def test_a_search_step_without_a_query_is_rejected(self):
        with pytest.raises(PlanValidationError, match="must carry a query"):
            plan(steps=(search_step(query=None),))

    def test_a_search_step_without_expected_source_types_is_rejected(self):
        with pytest.raises(PlanValidationError, match="expected source type"):
            plan(steps=(search_step(expected_source_types=()),))

    def test_an_analyze_step_must_not_carry_a_query(self):
        """It retrieves nothing, so a query on it would imply a capability it lacks."""
        with pytest.raises(PlanValidationError, match="must not carry a query"):
            ResearchStep(id="a", kind=StepKind.ANALYZE, question="q", query="x")

    def test_an_analyze_step_must_not_name_source_types(self):
        with pytest.raises(PlanValidationError, match="must not name"):
            ResearchStep(
                id="a", kind=StepKind.ANALYZE, question="q",
                expected_source_types=(SourceType.NEWS,),
            )

    def test_an_over_long_objective_is_rejected(self):
        with pytest.raises(PlanValidationError):
            plan(objective="x" * 5_000)

    def test_more_steps_than_the_ceiling_is_rejected(self):
        steps = tuple(
            search_step(f"s{i}", query=f"query {i}") for i in range(MAX_STEPS_CEILING + 1)
        )
        with pytest.raises(PlanValidationError, match="at most"):
            plan(steps=steps)

    def test_exactly_the_ceiling_is_accepted(self):
        steps = tuple(
            search_step(f"s{i}", query=f"query {i}") for i in range(MAX_STEPS_CEILING)
        )
        assert len(plan(steps=steps).steps) == MAX_STEPS_CEILING


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------

class TestStepIdentifiers:
    def test_an_empty_id_is_rejected(self):
        with pytest.raises(PlanValidationError):
            search_step(id="")

    def test_an_id_with_whitespace_is_rejected(self):
        """An id is a label other steps reference; a space makes it ambiguous."""
        with pytest.raises(PlanValidationError):
            search_step(id="step one")

    def test_an_id_with_a_newline_is_rejected(self):
        with pytest.raises(PlanValidationError):
            search_step(id="step\none")

    def test_an_over_long_id_is_rejected(self):
        with pytest.raises(PlanValidationError):
            search_step(id="s" * 200)

    def test_duplicate_ids_are_rejected(self):
        """Two steps sharing an id makes every dependency reference ambiguous."""
        with pytest.raises(PlanValidationError, match="duplicate step ids"):
            plan(
                steps=(
                    search_step("same", query="first"),
                    search_step("same", query="second"),
                )
            )

    def test_ids_are_not_required_to_be_ascii(self):
        """Persian identifiers are legal: the schema has no reason to require Latin."""
        step = search_step(id="گام۱")
        assert step.id == "گام۱"
        built = plan(steps=(step,), language="fa")
        assert built.step_by_id("گام۱") is not None

    def test_a_mixed_script_id_is_legal(self):
        step = search_step(id="step-گام")
        assert step.id == "step-گام"

    def test_an_unknown_id_is_not_found(self):
        assert plan().step_by_id("nope") is None


# ---------------------------------------------------------------------------
# Dependencies and the graph
# ---------------------------------------------------------------------------

class TestDependencyGraph:
    def test_a_dependency_must_reference_an_existing_step(self):
        with pytest.raises(PlanDependencyError, match="unknown step"):
            plan(steps=(search_step("a", depends_on=("ghost",)),))

    def test_a_self_dependency_is_rejected(self):
        with pytest.raises(PlanDependencyError, match="depends on itself"):
            search_step("a", depends_on=("a",))

    def test_a_two_step_cycle_is_rejected(self):
        with pytest.raises(PlanCycleError):
            plan(
                steps=(
                    search_step("a", depends_on=("b",)),
                    search_step("b", depends_on=("a",)),
                )
            )

    def test_a_three_step_cycle_is_rejected(self):
        with pytest.raises(PlanCycleError):
            plan(
                steps=(
                    search_step("a", depends_on=("c",)),
                    search_step("b", depends_on=("a",)),
                    search_step("c", depends_on=("b",)),
                )
            )

    def test_a_self_loop_is_reported_as_a_self_dependency_not_a_cycle(self):
        """A self-reference is a naming mistake, so it gets its own precise error.

        Checked before the acyclicity walk, so the caller learns *which* mistake it
        is rather than being told a cycle exists when there is only one bad edge.
        """
        with pytest.raises(PlanDependencyError, match="depends on itself"):
            plan(steps=(search_step("a", depends_on=("b",)), search_step("b", depends_on=("b",))))

    def test_the_cycle_message_names_the_offending_steps(self):
        with pytest.raises(PlanCycleError) as excinfo:
            plan(
                steps=(
                    search_step("a", depends_on=("b",)),
                    search_step("b", depends_on=("a",)),
                )
            )
        assert "a" in str(excinfo.value) and "b" in str(excinfo.value)

    def test_a_diamond_dependency_is_accepted(self):
        """a → b, a → c, then b and c → d is a DAG, not a cycle."""
        built = plan(
            steps=(
                search_step("a"),
                search_step("b", depends_on=("a",)),
                search_step("c", depends_on=("a",)),
                ResearchStep(id="d", kind=StepKind.ANALYZE, question="q",
                             depends_on=("b", "c")),
            )
        )
        assert len(built.steps) == 4

    def test_duplicate_dependencies_are_deduplicated(self):
        step = search_step("b", depends_on=("a", "a", " a "))
        assert step.depends_on == ("a",)

    def test_blank_dependencies_are_dropped(self):
        step = search_step("b", depends_on=("", "  "))
        assert step.depends_on == ()

    def test_a_single_dependency_may_be_a_bare_string(self):
        """Models routinely emit a string where a one-element list was asked for."""
        assert search_step("b", depends_on="a").depends_on == ("a",)

    def test_execution_order_satisfies_dependencies(self):
        built = plan(
            steps=(
                ResearchStep(id="d", kind=StepKind.ANALYZE, question="q",
                             depends_on=("b", "c")),
                search_step("b", depends_on=("a",)),
                search_step("c", depends_on=("a",)),
                search_step("a"),
            )
        )
        order = [step.id for step in built.execution_order()]
        assert order.index("a") < order.index("b") < order.index("d")
        assert order.index("a") < order.index("c") < order.index("d")

    def test_execution_order_is_deterministic_for_independent_steps(self):
        """Ties are broken by id, so the order cannot depend on set iteration."""
        built = plan(steps=(search_step("z"), search_step("m"), search_step("a")))
        assert [step.id for step in built.execution_order()] == ["a", "m", "z"]
        assert [step.id for step in built.execution_order()] == [
            step.id for step in built.execution_order()
        ]

    def test_dependency_depth_counts_steps(self):
        assert plan().dependency_depth == 1
        built = plan(
            steps=(
                search_step("a"),
                search_step("b", depends_on=("a",)),
                ResearchStep(id="c", kind=StepKind.ANALYZE, question="q",
                             depends_on=("b",)),
            )
        )
        assert built.dependency_depth == 3

    def test_longest_dependency_chain_is_zero_for_no_steps(self):
        assert longest_dependency_chain(()) == 0

    def test_validate_acyclic_accepts_a_dag(self):
        assert validate_acyclic((search_step("a"), search_step("b", depends_on=("a",)))) is None

    def test_validate_acyclic_rejects_a_missing_reference(self):
        with pytest.raises(PlanDependencyError):
            validate_acyclic((search_step("a", depends_on=("ghost")),))

    def test_validate_depth_rejects_too_deep_a_chain(self):
        steps = [search_step("a")]
        previous = "a"
        for index in range(2, 7):
            steps.append(search_step(f"s{index}", depends_on=(previous,)))
            previous = f"s{index}"
        with pytest.raises(PlanValidationError, match="depth"):
            validate_depth(tuple(steps), 3)

    def test_validate_depth_accepts_a_shallow_chain(self):
        steps = [search_step("a"), search_step("b", depends_on=("a",))]
        assert validate_depth(tuple(steps), 5) is None

    def test_validate_depth_requires_a_positive_bound(self):
        with pytest.raises(ValueError, match="at least 1"):
            validate_depth((search_step(),), 0)


# ---------------------------------------------------------------------------
# Enums and typing
# ---------------------------------------------------------------------------

class TestEnums:
    def test_an_unknown_step_kind_is_rejected(self):
        with pytest.raises(ValidationError):
            search_step(kind="teleport")

    def test_step_kinds_are_the_closed_set(self):
        assert {kind.value for kind in StepKind} == {"search", "analyze"}

    def test_source_types_are_the_closed_set(self):
        assert {s.value for s in SourceType} == {
            "academic", "news", "official", "technical", "community", "other",
        }

    def test_source_types_are_coerced_from_strings(self):
        step = search_step(expected_source_types=["Technical", "news"])
        assert step.expected_source_types == (SourceType.TECHNICAL, SourceType.NEWS)

    def test_an_unrecognised_source_type_becomes_other(self):
        """An honest 'something else' must not be rejected; it must not be wrong either."""
        step = search_step(expected_source_types=["blogs"])
        assert step.expected_source_types == (SourceType.OTHER,)

    def test_duplicate_source_types_are_collapsed_preserving_order(self):
        step = search_step(expected_source_types=["news", "technical", "news"])
        assert step.expected_source_types == (SourceType.NEWS, SourceType.TECHNICAL)

    def test_a_single_source_type_may_be_a_bare_string(self):
        assert search_step(expected_source_types="news").expected_source_types == (
            SourceType.NEWS,
        )

    def test_a_single_source_type_that_is_a_string_but_invalid_becomes_other(self):
        assert search_step(expected_source_types="blogs").expected_source_types == (
            SourceType.OTHER,
        )

    def test_expected_source_types_must_be_a_list(self):
        with pytest.raises(PlanValidationError):
            search_step(expected_source_types={"technical": True})

    def test_steps_must_not_be_a_bare_string(self):
        with pytest.raises(PlanValidationError):
            plan(steps="not steps")

    def test_steps_must_not_be_a_mapping(self):
        with pytest.raises(PlanValidationError):
            plan(steps={"id": "a"})

    def test_depends_on_must_not_be_a_mapping(self):
        with pytest.raises(PlanValidationError):
            search_step(depends_on={"a": 1})

    def test_a_non_string_question_is_rejected(self):
        with pytest.raises(PlanValidationError):
            search_step(question=123)

    def test_a_non_string_objective_is_rejected(self):
        with pytest.raises(PlanValidationError):
            plan(objective=["a"])

    def test_an_invalid_language_tag_is_rejected(self):
        with pytest.raises(PlanValidationError):
            plan(language="fa_IR!!")

    def test_language_tags_are_normalized_to_lowercase(self):
        assert plan(language="EN-GB").language == "en-gb"

    def test_an_empty_language_becomes_none(self):
        assert plan(language="   ").language is None


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_whitespace_in_the_objective_is_collapsed(self):
        assert plan(objective="line one\n\n  line   two").objective == (
            "line one line two"
        )

    def test_whitespace_in_a_query_is_collapsed(self):
        assert search_step(query="  python\n  asyncio  ").query == "python asyncio"

    def test_a_query_over_the_search_bound_is_rejected(self):
        """A plan must never hold a query `SearchRequest` would refuse to run."""
        with pytest.raises(PlanValidationError):
            search_step(query="q" * 600)

    def test_step_ids_are_stripped(self):
        assert search_step(id="  s1  ").id == "s1"

    def test_dependencies_are_stripped(self):
        assert search_step("b", depends_on=("  a  ",)).depends_on == ("a",)

    def test_a_query_is_not_lowercased(self):
        """Lowercasing a query would rewrite a case-sensitive term the user chose."""
        assert search_step(query="Python Asyncio").query == "Python Asyncio"

    def test_punctuation_is_not_stripped(self):
        assert search_step(query="چرا؟ بله.").query == "چرا؟ بله."


# ---------------------------------------------------------------------------
# Serialization (FR-4)
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_payload_contains_every_meaningful_field(self):
        built = plan(
            steps=(
                search_step("a", rationale="why", depends_on=()),
                ResearchStep(id="b", kind=StepKind.ANALYZE, question="compare",
                             depends_on=("a",), rationale="synthesize"),
            )
        )
        payload = built.to_payload()
        assert payload["schema_version"] == PLAN_SCHEMA_VERSION
        assert payload["objective"] == built.objective
        assert payload["language"] == "en"
        first, second = payload["steps"]
        assert first["id"] == "a"
        assert first["kind"] == "search"
        assert first["question"] == built.steps[0].question
        assert first["query"] == built.steps[0].query
        assert first["expected_source_types"] == ["technical"]
        assert first["depends_on"] == []
        assert first["rationale"] == "why"
        assert second["kind"] == "analyze"
        assert second["query"] is None
        assert second["depends_on"] == ["a"]

    def test_round_trip_through_a_payload_is_lossless(self):
        built = plan(
            steps=(
                search_step("a", rationale="why"),
                ResearchStep(id="b", kind=StepKind.ANALYZE, question="q",
                             depends_on=("a",)),
            )
        )
        assert ResearchPlan.from_payload(built.to_payload()) == built

    def test_round_trip_through_json_is_lossless(self):
        built = plan(steps=(search_step("a", rationale="why"),))
        assert ResearchPlan.from_json(built.to_json()) == built

    def test_round_trip_through_real_json_text_is_lossless(self):
        """The payload must survive actual JSON encoding, not just dict copying."""
        built = plan(steps=(search_step("a"),))
        assert ResearchPlan.from_json(json.dumps(built.to_payload())) == built

    def test_the_payload_is_json_encodable(self):
        json.dumps(plan().to_payload())

    def test_serialization_is_deterministic(self):
        built = plan(steps=(search_step("a"), search_step("b", depends_on=("a",))))
        assert built.to_json() == built.to_json()
        assert built.to_payload() == built.to_payload()

    def test_two_identical_plans_serialize_identically(self):
        assert plan().to_json() == plan().to_json()

    def test_persian_is_not_escaped_into_escape_sequences(self):
        """Persian must round-trip as Persian, not as \\uXXXX escapes."""
        built = plan(objective="قیمت نفت", steps=(search_step(query="نفت"),), language="fa")
        text = built.to_json()
        assert "قیمت نفت" in text
        assert "\\u" not in text
        assert ResearchPlan.from_json(text) == built

    def test_step_order_is_preserved_through_serialization(self):
        built = plan(
            steps=(
                search_step("z", query="z query"),
                search_step("a", query="a query"),
                search_step("m", query="m query"),
            )
        )
        assert [s["id"] for s in built.to_payload()["steps"]] == ["z", "a", "m"]
        assert [s.id for s in ResearchPlan.from_payload(built.to_payload()).steps] == [
            "z", "a", "m",
        ]

    def test_a_rebuilt_plan_recomputes_derived_properties(self):
        built = plan(steps=(search_step("a"), search_step("b", depends_on=("a",))))
        rebuilt = ResearchPlan.from_payload(built.to_payload())
        assert rebuilt.dependency_depth == built.dependency_depth
        assert rebuilt.execution_order() == built.execution_order()
        assert rebuilt.search_queries == built.search_queries


class TestDeserializationIsRevalidated:
    """A plan read from storage is validated, not trusted."""

    def test_a_payload_that_is_not_a_mapping_is_rejected(self):
        with pytest.raises(PlanValidationError):
            ResearchPlan.from_payload(["not", "a", "plan"])  # type: ignore[arg-type]

    def test_a_payload_with_a_cycle_is_rejected(self):
        payload = plan().to_payload()
        payload["steps"][0]["depends_on"] = ["s1"]
        with pytest.raises(PlanValidationError):
            ResearchPlan.from_payload(payload)

    def test_a_payload_missing_steps_is_rejected(self):
        payload = plan().to_payload()
        del payload["steps"]
        with pytest.raises(PlanValidationError):
            ResearchPlan.from_payload(payload)

    def test_a_payload_with_no_objective_is_rejected(self):
        payload = plan().to_payload()
        del payload["objective"]
        with pytest.raises(PlanValidationError):
            ResearchPlan.from_payload(payload)

    def test_a_payload_with_a_duplicate_id_is_rejected(self):
        payload = plan(steps=(search_step("a"),)).to_payload()
        payload["steps"].append(dict(payload["steps"][0]))
        with pytest.raises(PlanValidationError):
            ResearchPlan.from_payload(payload)

    def test_a_payload_with_an_unknown_dependency_is_rejected(self):
        payload = plan().to_payload()
        payload["steps"][0]["depends_on"] = ["ghost"]
        with pytest.raises(PlanDependencyError):
            ResearchPlan.from_payload(payload)

    def test_a_degenerate_payload_is_rejected(self):
        payload = plan().to_payload()
        payload["steps"] = [
            {
                "id": "a", "kind": "analyze", "question": "q",
                "query": None, "expected_source_types": [], "depends_on": [],
                "rationale": None,
            }
        ]
        with pytest.raises(PlanValidationError, match="no search step"):
            ResearchPlan.from_payload(payload)

    def test_a_payload_from_another_schema_version_is_rejected(self):
        payload = plan().to_payload()
        payload["schema_version"] = "research-plan-v0"
        with pytest.raises(PlanValidationError, match="schema version"):
            ResearchPlan.from_payload(payload)

    def test_a_payload_with_no_schema_version_is_accepted(self):
        """A plan written before versioning existed is readable rather than fatal."""
        payload = plan().to_payload()
        del payload["schema_version"]
        assert ResearchPlan.from_payload(payload).schema_version == PLAN_SCHEMA_VERSION

    def test_invalid_json_text_is_rejected(self):
        with pytest.raises(PlanValidationError, match="not valid JSON"):
            ResearchPlan.from_json("{not json")

    def test_a_payload_with_a_wrongly_typed_field_is_rejected(self):
        payload = plan().to_payload()
        payload["steps"] = "a string, not steps"
        with pytest.raises(PlanValidationError):
            ResearchPlan.from_payload(payload)


# ---------------------------------------------------------------------------
# Frozen-ness
# ---------------------------------------------------------------------------

class TestImmutability:
    def test_a_plan_cannot_be_mutated(self):
        built = plan()
        with pytest.raises(ValidationError):
            built.objective = "changed"  # type: ignore[misc]

    def test_a_step_cannot_be_mutated(self):
        step = search_step()
        with pytest.raises(ValidationError):
            step.query = "changed"  # type: ignore[misc]

    def test_the_steps_tuple_cannot_be_appended_to(self):
        built = plan()
        with pytest.raises(ValidationError):
            built.steps = ()  # type: ignore[misc]

    def test_two_identically_built_plans_are_equal(self):
        assert plan() == plan()

    def test_a_plan_is_hashable_as_a_frozen_model(self):
        assert len({plan(), plan()}) == 1