"""Provider-independent search contract (Phase 3, P3.2).

The types here are what the Research Engine reasons about: *a request to find
pages about X*, and *a set of candidate pages the provider offered, in its own
order*. No provider SDK type, response field name, HTTP client type, or
provider authentication object appears in this module, so replacing the search
vendor cannot reach domain code (FR-5, FR-6, § 15.4, T-3).

Three distinctions are load-bearing and are stated here rather than implied:

``SearchResult`` ≠ ``Source`` ≠ ``SourceChunk``
    A ``SearchResult`` is what a provider *advertised*: a URL and a snippet it
    wrote, for a page nobody in this process has read. A ``Source``
    (:mod:`app.domain.research.types`) is the persisted record of that
    candidate, and it starts at ``SourceStatus.PENDING`` — still unfetched. A
    ``SourceChunk`` is text extracted from the page at P3.4. P3.2 produces only
    the first of the three, and treats it accordingly.

**Results are untrusted external input.** They are preserved as data. No URL
here is validated, resolved, fetched, or treated as safe: scheme checks, DNS
resolution, SSRF rejection, redirect validation, content-type checks and size
limits all belong to the fetch boundary at P3.3 (AD-031, § 15.5). Duplicating
any of that here would create a second, weaker boundary.

Relevance is preserved, not computed. ``SearchResult.rank`` is the provider's
own ordering, and ``SearchResult.relevance`` is a relevance number when — and
only when — the provider exposes one. No ranking, re-ordering, or deduplication
happens here; that is the selection policy's job at P3.5.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Upper bound on one query's length. A search query is a question fragment,
#: not a document; a longer input is either a programming error or an attempt to
#: spend provider budget on one request.
MAX_QUERY_CHARS = 512

#: Upper bound on results per query. This is *our* resource bound (SR-5:
#: bounded result counts), not a provider limit — an adapter clamps further if
#: its provider is stricter, and a caller may ask for fewer.
MAX_RESULTS_LIMIT = 20


class SearchFreshness(enum.StrEnum):
    """A relative recency window for results.

    Relative rather than absolute because the pipeline's need is "how recent is
    this" (FR-7, § 15.2 freshness), and because providers express absolute
    windows differently. A caller that needs an absolute window expresses it in
    the query text.
    """

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"

    @classmethod
    def coerce(cls, value: object) -> SearchFreshness:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown freshness window: {value!r}") from exc


class SafeSearchLevel(enum.StrEnum):
    """Provider-independent adult-content filtering level.

    Justified because the query text originates from an arbitrary Telegram user
    (SR-2: a user supplies a question), so the filtering decision cannot be left
    to each caller's discretion. Providers that do not implement a level map
    this to their own equivalent; one that cannot honour it is not a candidate.
    """

    OFF = "off"
    MODERATE = "moderate"
    STRICT = "strict"

    @classmethod
    def coerce(cls, value: object) -> SafeSearchLevel:
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"unknown safe search level: {value!r}") from exc


def _optional_text(value: object) -> str | None:
    """Absent, null and empty text all normalize to ``None``.

    Providers disagree about which of the three they send for "nothing here", and
    the three are indistinguishable to a later stage.
    """
    if value is None:
        return None
    return " ".join(str(value).split()) or None


def _optional_datetime(value: object) -> datetime | None:
    """Interpret a publication timestamp, or return ``None``.

    A naive datetime is read as UTC; an ISO-8601 string is parsed; anything else
    — including a provider's human phrasing such as "3 days ago" — becomes
    ``None``. Recency belongs in the query (``SearchRequest.freshness``) or in
    P3.5's ranking policy; a guessed date would be worse than no date.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def domain_of(url: str) -> str:
    """The registrable-looking host of ``url``, lowercased, without a port.

    Deliberately simple and deliberately *not* a public-suffix implementation:
    the value is a display and grouping label (source diversity, dedup grouping
    at P3.5), not a security or identity decision. ``urlsplit`` cannot raise on
    the shapes a provider can return, but it can produce an empty host, so an
    unusable value raises ``ValueError`` and the caller drops the result rather
    than inventing a domain.
    """
    host = (urlsplit(url).hostname or "").strip().lower()
    host = host.rstrip(".")
    if not host or " " in host:
        raise ValueError(f"url has no usable host: {url!r}")
    return host


class SearchRequest(BaseModel):
    """One search, described in terms the domain owns.

    Small on purpose. Only the controls this pipeline actually needs are
    represented: the query, how many results are wanted, an optional language
    and market hint, an optional recency window, and safe-search level. A
    provider capability that cannot be expressed generically belongs in the
    adapter or is not used — it does not become a domain field (AD-036).
    """

    model_config = ConfigDict(frozen=True)

    query: str
    max_results: int = 8
    language: str | None = None
    country: str | None = None
    freshness: SearchFreshness | None = None
    safe_search: SafeSearchLevel | None = None

    @field_validator("query")
    @classmethod
    def _query_usable(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        collapsed = " ".join(value.split())
        if not collapsed:
            raise ValueError("must be a non-empty query")
        if len(collapsed) > MAX_QUERY_CHARS:
            raise ValueError(f"must be at most {MAX_QUERY_CHARS} characters")
        return collapsed

    @field_validator("max_results")
    @classmethod
    def _bounded(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must request at least one result")
        if value > MAX_RESULTS_LIMIT:
            raise ValueError(f"must request at most {MAX_RESULTS_LIMIT} results")
        return value

    @field_validator("language")
    @classmethod
    def _language_tag(cls, value: str | None) -> str | None:
        if value is None:
            return None
        tag = value.strip().lower()
        if not tag:
            return None
        if not all(part.isalnum() for part in tag.split("-")) or len(tag) > 12:
            raise ValueError("must be a BCP-47 style language tag, e.g. 'fa' or 'en-GB'")
        return tag

    @field_validator("country")
    @classmethod
    def _country_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        code = value.strip().upper()
        if not code:
            return None
        if len(code) != 2 or not code.isalpha():
            raise ValueError("must be a two-letter country code, e.g. 'IR'")
        return code

    @field_validator("freshness", mode="before")
    @classmethod
    def _coerce_freshness(cls, value: object) -> SearchFreshness | None:
        return None if value is None else SearchFreshness.coerce(value)

    @field_validator("safe_search", mode="before")
    @classmethod
    def _coerce_safe_search(cls, value: object) -> SafeSearchLevel | None:
        return None if value is None else SafeSearchLevel.coerce(value)


class SearchResult(BaseModel):
    """One candidate page the provider offered, in the provider's order.

    Untrusted (see the module docstring). ``url`` is preserved verbatim —
    normalized only by whitespace trimming — because rewriting it here would
    create a second, unreviewed URL policy outside the P3.3 boundary.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    domain: str
    rank: int
    title: str | None = None
    snippet: str | None = None
    published_at: datetime | None = None
    relevance: float | None = None

    @field_validator("url", "domain")
    @classmethod
    def _identity_present(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()

    @field_validator("title", "snippet", mode="before")
    @classmethod
    def _normalize_text(cls, value: object) -> object:
        return _optional_text(value)

    @field_validator("rank")
    @classmethod
    def _rank_is_position(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be a 1-based position")
        return value

    @field_validator("published_at", mode="before")
    @classmethod
    def _timezone_aware(cls, value: object) -> object:
        return _optional_datetime(value)

    @classmethod
    def from_provider_fields(
        cls,
        *,
        url: object,
        rank: int,
        title: object = None,
        snippet: object = None,
        published_at: object = None,
        relevance: object = None,
    ) -> SearchResult:
        """Build a result from a provider payload, deriving ``domain``.

        This is the whole normalization rule, and it lives in the domain so it is
        identical for every provider and testable without a provider. Every field
        arrives as ``object`` on purpose: a provider payload is untyped data, and
        each field is checked here rather than trusted. Raises ``ValueError`` when
        the fields cannot form a usable result; the adapter decides what to do
        with an unusable one.

        ``published_at`` accepts a ``datetime`` or a parsable string and nothing
        else. An unparseable date becomes ``None`` rather than an exception,
        because a page with a badly formatted date is still a usable source.
        """
        text_url = url.strip() if isinstance(url, str) else ""
        if not text_url:
            raise ValueError("result has no url")
        score = float(relevance) if isinstance(relevance, (int, float)) else None
        return cls(
            url=text_url,
            domain=domain_of(text_url),
            rank=rank,
            title=_optional_text(title),
            snippet=_optional_text(snippet),
            published_at=_optional_datetime(published_at),
            relevance=score,
        )


class SearchResponse(BaseModel):
    """The provider's answer to one :class:`SearchRequest`, normalized.

    ``provider`` is recorded because a research run must record which provider
    served it, so a result can be re-explained afterwards (§ 6 determinism).
    ``results`` keeps provider order; ordering is the provider's, never ours
    at this stage.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    provider: str
    results: tuple[SearchResult, ...] = ()
    # When the provider answered, not when this object was constructed: an
    # orchestrator may hold a response for a while before persisting it, and
    # reproducibility (§ 6) needs the provider's timestamp.
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("query", "provider")
    @classmethod
    def _present(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()

    @field_validator("retrieved_at", mode="before")
    @classmethod
    def _default_retrieved_at(cls, value: object) -> object:
        if value is None:
            return datetime.now(UTC)
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @field_validator("results")
    @classmethod
    def _unique_ranks(cls, value: tuple[SearchResult, ...]) -> tuple[SearchResult, ...]:
        ranks = [result.rank for result in value]
        if len(set(ranks)) != len(ranks):
            raise ValueError("each result must occupy a distinct position")
        return value

    @property
    def is_empty(self) -> bool:
        return not self.results

    @property
    def domains(self) -> tuple[str, ...]:
        """Distinct hosts, in first-appearance order. A diversity measure, not a filter."""
        seen: dict[str, None] = {}
        for result in self.results:
            seen.setdefault(result.domain, None)
        return tuple(seen)