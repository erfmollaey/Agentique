"""The search provider selection point (Phase 3, P3.2 — AD-036).

``SEARCH_PROVIDER`` selects the implementation. This function is the only place
that decision exists in code, mirroring ``create_llm_client`` for the AI
provider (FR-16). Adding a provider means adding a branch here; the research
domain, the persistence layer, and every future stage are unaffected (T-3).

The selection is deliberately *one* provider, not a chain. A fallback chain is
an option the abstraction permits (§ 15.4) and not something to build at the
outset: with one adapter there is nothing to fall back to, and a chain that
cannot be exercised is a chain whose behaviour is unknown.
"""

from __future__ import annotations

from app.core.config import Settings
from app.domain.research.ports import SearchProvider
from app.infrastructure.search.brave import BraveSearchAdapter
from app.infrastructure.search.tavily import TavilySearchAdapter

#: Every provider name the factory recognises, mapped to the adapter that
#: implements it. Kept as one table so a new provider cannot be half-registered
#: (named but not built, or built but not named).
_ADAPTERS: dict[str, type] = {
    "brave": BraveSearchAdapter,
    "tavily": TavilySearchAdapter,
}

#: Every provider name the factory recognises. Kept beside the branches so a
#: new provider cannot be half-registered.
SUPPORTED_SEARCH_PROVIDERS = frozenset(_ADAPTERS)


def create_search_provider(settings: Settings, provider: str | None = None) -> SearchProvider:
    """Build the configured search provider.

    Raises ``ValueError`` for an unrecognised name rather than silently
    falling back: a typo in configuration must not quietly change where research
    results come from. Raises
    :class:`app.domain.errors.SearchAuthenticationError` when the selected
    provider has no credential configured.
    """
    name = (provider or settings.SEARCH_PROVIDER or "tavily").strip().lower()

    if name not in SUPPORTED_SEARCH_PROVIDERS:
        raise ValueError(
            f"Unknown SEARCH_PROVIDER {name!r}. "
            f"Supported: {', '.join(sorted(SUPPORTED_SEARCH_PROVIDERS))}. "
            "Add a branch here to register another provider (AD-036)."
        )

    adapter: SearchProvider = _ADAPTERS[name](settings)
    return adapter