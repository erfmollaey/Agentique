"""The AD-026 decision: research planning versus ``Supervisor.analyze_query``.

P3.6 had to resolve whether Phase 3 planning reuses ``Supervisor.analyze_query``
or supersedes it. It supersedes it, and these tests are the evidence for that
decision and for the boundary it draws:

* the two produce different artifacts with different contracts (FR-1's sub-questions
  + search strategies + expected source types versus a summary + sub-questions);
* research planning does not touch the chat path (Phase 2 regression);
* the retained ``Supervisor`` seam is still functional, so nothing was broken or
  removed;
* the two do not share a prompt, a schema, or a code path, so neither can silently
  change the other's behaviour.
"""

from __future__ import annotations

import inspect

from app.agents.supervisor import Supervisor
from app.domain.research.planning import ResearchPlan
from app.infrastructure.llm import _ANALYSIS_PROMPT, LLMClient, OpenAICompatibleClient
from app.infrastructure.planning import planner as planner_module
from app.infrastructure.planning.prompts import (
    PLANNING_SYSTEM_PROMPT,
    build_planning_messages,
)
from app.services.prompts import CHAT_SYSTEM_PROMPT


class TestTheDecisionIsOptionB:
    """A dedicated Phase 3 planner supersedes ``analyze_query`` for planning."""

    def test_research_planning_does_not_call_analyze_query(self):
        """Option B, stated structurally: no research-planning code path reaches it."""
        assert planner_module.__file__ is not None
        with open(planner_module.__file__, encoding="utf-8") as handle:
            text = handle.read()
        assert "analyze_query" not in text
        assert "Supervisor" not in text

    def test_the_research_planner_does_not_import_the_supervisor(self):
        import ast

        assert planner_module.__file__ is not None
        with open(planner_module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not any("supervisor" in name for name in imported)
        assert "app.agents.supervisor" not in imported

    def test_the_planner_is_not_a_thin_wrapper_over_analyze_query(self):
        """It has its own provider protocol and its own prompt."""
        assert hasattr(planner_module.LLMPlanner, "plan")
        assert "you plan web research" in PLANNING_SYSTEM_PROMPT.lower()
        assert PLANNING_SYSTEM_PROMPT is not CHAT_SYSTEM_PROMPT

    def test_the_two_prompts_are_independent_constants(self):
        assert PLANNING_SYSTEM_PROMPT != _ANALYSIS_PROMPT
        assert PLANNING_SYSTEM_PROMPT != CHAT_SYSTEM_PROMPT


class TestTheTwoContractsDiffer:
    """FR-1 needs more than a summary and a list of sub-questions."""

    def test_analyze_query_yields_a_summary_and_sub_questions_only(self):
        from app.domain.schemas import QueryAnalysis

        analysis = QueryAnalysis(summary="s", sub_questions=["a", "b"])
        assert not hasattr(analysis, "steps")
        assert not hasattr(analysis, "objective")

    def test_a_plan_carries_what_analyze_query_cannot_express(self):
        """Search strategies, expected source types, and dependencies.

        FR-1 requires all three; ``QueryAnalysis`` has no field for any of them, so
        reusing it would have meant widening a Phase 2 schema that the chat path
        also depends on.
        """
        from app.domain.schemas import QueryAnalysis

        plan_fields = set(ResearchPlan.model_fields)
        analysis_fields = set(QueryAnalysis.model_fields)
        for required in ("objective", "steps"):
            assert required in plan_fields
            assert required not in analysis_fields

    def test_a_planning_step_expresses_a_search_strategy(self):
        from app.domain.research.planning import ResearchStep, SourceType

        step = ResearchStep(
            id="s", question="q", query="a search query",
            expected_source_types=(SourceType.NEWS,),
        )
        assert step.query is not None
        assert step.expected_source_types


class TestPhase2IsUntouched:
    def test_the_chat_system_prompt_is_still_chat_v1(self):
        from app.services.prompts import CHAT_SYSTEM_PROMPT_VERSION

        assert CHAT_SYSTEM_PROMPT_VERSION == "chat-v1"

    def test_the_chat_prompt_still_denies_internet_access(self):
        assert "no access to the internet" in CHAT_SYSTEM_PROMPT

    def test_the_llm_client_still_exposes_analyze_query(self):
        """Not removed: AD-026 retained the seam and P3.6 did not delete it."""
        assert hasattr(LLMClient, "analyze_query")
        assert hasattr(OpenAICompatibleClient, "analyze_query")

    def test_the_supervisor_still_decomposes_a_query(self):
        from app.domain.schemas import QueryAnalysis

        recorded: list[str] = []

        class FakeLLM:
            def analyze_query(self, user_query: str) -> QueryAnalysis:
                recorded.append(user_query)
                return QueryAnalysis(summary="s", sub_questions=["a"])

            def generate_final_answer(self, user_query: str, sub_answers: list[str]) -> str:
                return "final"

        assert Supervisor(FakeLLM()).analyze_query("a question").sub_questions == ["a"]
        assert recorded == ["a question"]

    def test_the_supervisor_analyze_query_signature_is_unchanged(self):
        signature = inspect.signature(Supervisor.analyze_query)
        assert list(signature.parameters) == ["self", "user_query"]

    def test_the_research_prompt_does_not_leak_into_the_llm_client_module(self):
        """Planning added no prompt to the shared client module."""
        from app.infrastructure import llm as llm_module

        assert llm_module.__file__ is not None
        with open(llm_module.__file__, encoding="utf-8") as handle:
            text = handle.read()
        assert PLANNING_SYSTEM_PROMPT not in text
        assert "research-planning-v1" not in text

    def test_chat_messages_do_not_carry_planning_delimiters(self):
        """The research delimiters are planning's alone; chat has no such region."""
        assert "RESEARCH_QUESTION" not in CHAT_SYSTEM_PROMPT


class TestPlanningMessagesAreNotChatMessages:
    """The planning prompt is a research prompt, not a chat prompt."""

    def test_planning_uses_two_messages_not_a_history(self):
        messages = build_planning_messages("q", max_steps=6)
        assert len(messages) == 2

    def test_planning_tells_the_model_it_has_no_web_access(self):
        messages = build_planning_messages("q", max_steps=6)
        assert "no web access" in messages[0].content

    def test_the_chat_prompt_tells_the_model_it_has_no_memory(self):
        assert "no memory outside this conversation" in CHAT_SYSTEM_PROMPT