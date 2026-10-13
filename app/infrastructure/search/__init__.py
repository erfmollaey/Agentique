"""Search provider adapters (Phase 3, P3.2).

Which provider is configured (``SEARCH_PROVIDER``) is resolved by
:func:`create_search_provider` — the same single-selection-point shape as
``create_llm_client`` for the LLM (FR-16, T-3).

Registering another provider means adding a module here and a branch in that
factory. It does not mean touching ``app/domain/research/``, which reasons only
in :mod:`app.domain.research.search` types.
"""

from __future__ import annotations

from app.domain.research.ports import SearchProvider
from app.infrastructure.search.brave import BraveSearchAdapter
from app.infrastructure.search.factory import create_search_provider
from app.infrastructure.search.tavily import TavilySearchAdapter

__all__ = [
    "BraveSearchAdapter",
    "SearchProvider",
    "TavilySearchAdapter",
    "create_search_provider",
]