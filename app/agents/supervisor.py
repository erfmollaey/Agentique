"""Query decomposition agent.

Retained from the original codebase. The module-level ``client`` singleton was
a direct cause of audit C-3 and has been removed: the client is now injected
(P1-6). Both methods delegate to :mod:`app.infrastructure.llm`.

``generate_final_answer`` still has no callers in Phase 1 (audit M-1). It is
kept rather than deleted so no existing capability is removed by a
stabilization phase; it is wired in Phase 3.
"""

from __future__ import annotations

from app.core.config import get_settings
from app.domain.schemas import QueryAnalysis
from app.infrastructure.llm import LLMClient, OpenAICompatibleClient


class Supervisor:
    """Decomposes a user question into a summary and sub-questions."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        # Default to a real client only when one is not supplied, so the class
        # is usable standalone without reintroducing an import-time singleton.
        self._llm = llm or OpenAICompatibleClient(get_settings())

    def analyze_query(self, user_query: str) -> QueryAnalysis:
        """Stage 1 — analyse the question and produce sub-questions."""
        return self._llm.analyze_query(user_query)

    def generate_final_answer(self, user_query: str, sub_answers: list[str]) -> str:
        """Stage 2 — combine sub-answers into one response. Not yet wired."""
        return self._llm.generate_final_answer(user_query, sub_answers)
