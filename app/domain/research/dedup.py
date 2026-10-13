"""Deduplication and ranking domain (Phase 3, P3.5).

Deterministic duplicate detection, canonical representative selection, and
deterministic ranking over ``SearchResult`` (P3.2) + ``ExtractedContent`` (P3.4)
candidates.

What this module decides, stated up front because every rule below follows from
it:

* **URL canonicalization is not URL validation.** It compares two values; it
  never fetches, resolves, or approves anything. The SSRF boundary stays at P3.3
  (AD-031) and this module does not weaken it — it opens no socket, creates no
  HTTP client, and treats every URL as untrusted data (SR-1 … SR-8).
* **A tracking parameter is removed only when its name is in the explicit
  :data:`TRACKING_PARAMETERS` set.** A parameter outside that set is preserved,
  including ones whose names merely look familiar, because a removed parameter
  can change what a URL points at.
* **Identity is canonical URL identity or exact normalized-content identity.**
  No embeddings, no vectors, no LLM, and no model of any kind: an exact
  fingerprint is the only content rule, so the same content always collapses and
  two genuinely different articles never do (FR-24, FR-25).
* **Ordering never reads a clock.** Freshness is scored against a
  caller-supplied ``reference_time``, so ranking a stored candidate set
  reproduces its original order (FR-28, § 15.6).

The two policies are ports (``SourceDeduplication``, ``RankingPolicy`` in
:mod:`app.domain.research.ports`), so the strategy is replaceable without
touching a caller (FR-29). The concrete implementation here is pure: no
database, no HTTP, no LLM, no clock, no randomness.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import ParseResult, parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.errors import (
    InvalidSourceCandidateError,
    RankingInputError,
    UnsupportedSourceIdentityError,
)
from app.domain.research.extraction import MIN_EXTRACTED_CHARS, ExtractedContent
from app.domain.research.search import SearchResult

# ---------------------------------------------------------------------------
# URL canonicalization
# ---------------------------------------------------------------------------

# Parameters whose only purpose is analytics/tracking. Removing these is safe
# because they do not change the content the URL points at. The set is explicit
# and reviewable; a parameter not in this set is preserved.
TRACKING_PARAMETERS = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "_gl",
    "_hsenc",
    "_hsmi",
    "__s",
    "_rdr",
    "mkt_tok",
    "ref",
    "clk",
    "click",
    "affiliate",
    "affiliate_id",
    "ref_id",
    "sessionid",
    "sid",
})

# Default ports that can be elided when the scheme matches.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

# Paths that are equivalent to "/" when empty or just a slash.
_EMPTY_PATH_NORMALIZATIONS = frozenset({".", ""})


def _percent_decode_safe(value: str) -> str:
    """Percent-decode a single path or query component, keeping UTF-8 intact.

    Only unreserved characters and a small set of safe characters are decoded to
    their literal form; everything else stays percent-encoded. This avoids
    double-decode problems on already-decoded input while still normalizing
    redundant encoding like '%20' -> ' '.
    """

    result: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "%" and i + 2 < len(value):
            hi = value[i + 1]
            lo = value[i + 2]
            if hi in "0123456789ABCDEFabcdef" and lo in "0123456789ABCDEFabcdef":
                code_point = int(value[i + 1 : i + 3], 16)
                # Unreserved characters (RFC 3986) + space + a few safe ones.
                if (
                    (0x2D <= code_point <= 0x2F)
                    or (0x30 <= code_point <= 0x39)
                    or (0x41 <= code_point <= 0x5A)
                    or (0x5F <= code_point <= 0x7A)
                    or (0x7B <= code_point <= 0x7E)
                    or code_point in {0x20}
                ):
                    result.append(chr(code_point))
                else:
                    result.append(value[i : i + 3])
                i += 3
                continue
        result.append(ch)
        i += 1
    return "".join(result)


def canonical_url(url: str) -> str:
    """Deterministic canonical form of a URL, for comparison only.

    Two URLs with the same canonical form are *treated as the same source* by
    deduplication; nothing else follows from that equivalence. This function is
    emphatically not validation: it does not check the scheme against a
    safety allowlist, does not resolve a hostname, does not open a socket, and
    does not approve anything for retrieval. That remains the P3.3 boundary
    (AD-031). A canonical URL here is a comparison key, never a permission.

    The rules, each narrow enough to state why it is safe:

    * **scheme and host lowercased** — both are case-insensitive per RFC 3986.
    * **trailing host dot removed** — ``example.com.`` and ``example.com`` name
      the same host.
    * **default port dropped** — ``:80`` on ``http`` and ``:443`` on ``https``
      are the absence of a port. A non-default port is kept: it selects a
      different service.
    * **fragment removed** — never transmitted, so it cannot identify content.
    * **empty path becomes ``/``**, redundant slashes collapse, and a trailing
      slash is dropped from a non-root path, so ``/article`` and ``/article/``
      are one source.
    * **percent-encoding normalized** — RFC 3986 unreserved characters are
      decoded and the rest are left encoded, so ``%7Euser`` and ``~user``
      agree while a reserved character is never accidentally turned into a
      delimiter. Decoding is applied once to already-decoded input, so no
      double-decoding can occur.
    * **NFC applied** to the decoded path, so the same Persian text with a
      composed or decomposed vowel agrees.

    One policy decision is worth stating outright because it is a judgement
    rather than a rule: **the path is lowercased.** Case-sensitive paths are
    rare and ``/Article`` versus ``/article`` being one source is the more
    common expectation; the alternative, leaving case significant, would miss a
    class of duplicate rather than introduce a wrong merge. The query string is
    *not* lowercased, because query values are frequently case-sensitive data.

    Tracking parameters are removed only when named in
    :data:`TRACKING_PARAMETERS`. Everything else survives, so this function can
    never turn ``?id=7`` into ``?id=8``.

    Raises :class:`~app.domain.errors.UnsupportedSourceIdentityError` when the
    value has no scheme and authority and therefore no comparable form.
    """
    text = url.strip() if isinstance(url, str) else ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        # The value is deliberately not echoed: a URL is untrusted external input,
        # and a control character or an embedded credential must not reach a log
        # line through an exception message (the rule FetchInvalidURLError states).
        raise UnsupportedSourceIdentityError(
            "url has no usable scheme and authority, so it has no canonical form"
        )

    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if host.endswith("."):
        host = host[:-1]

    port = parsed.port
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        port = None

    path = _normalize_path(parsed.path or "/", lowercase=True)
    query = _normalize_query(parsed.query)

    # The fragment is dropped outright: it is never sent to the server, so it
    # cannot be part of what the page serves.
    return urlunparse(
        ParseResult(
            scheme=scheme,
            netloc=f"{host}:{port}" if port is not None else host,
            path=path,
            params="",
            query=query,
            fragment="",
        )
    )


def _normalize_path(path: str, lowercase: bool = False) -> str:
    """Normalize path: optional lowercase, percent-decode unreserved, NFC,
    collapse redundant slashes, deterministic trailing slash policy."""
    import re

    if lowercase:
        path = path.lower()
    # NFC normalize the percent-decoded path for Unicode stability
    decoded = _percent_decode_safe(path)
    decoded = unicodedata.normalize("NFC", decoded)
    # Collapse redundant slashes (but not the leading one)
    normalized = re.sub(r"/+", "/", decoded)
    # Trailing slash policy: remove trailing slash unless path is just "/"
    # This makes "/article" and "/article/" equivalent for dedup.
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    if not normalized:
        normalized = "/"
    return normalized


def _normalize_query(query: str) -> str:
    """Remove tracking parameters, sort remaining, normalize encoding."""
    if not query:
        return ""
    params = parse_qsl(query, keep_blank_values=True)
    kept: list[tuple[str, str]] = []
    for name, value in params:
        name_lower = name.strip().lower()
        if name_lower in TRACKING_PARAMETERS:
            continue
        decoded_name = _percent_decode_safe(unicodedata.normalize("NFC", name))
        decoded_value = _percent_decode_safe(unicodedata.normalize("NFC", value))
        kept.append((decoded_name, decoded_value))
    if not kept:
        return ""
    kept.sort(key=lambda pair: (pair[0], pair[1]))
    return urlencode(kept, doseq=True, safe=":%/?=&#")


# ---------------------------------------------------------------------------
# Content fingerprinting
# ---------------------------------------------------------------------------

def content_fingerprint(text: str) -> str:
    """Deterministic fingerprint of normalized extracted text.

    The pipeline is NFC normalization, case folding, whitespace collapsing, then
    SHA-256 over the UTF-8 bytes. Each step is there for a stated reason, and
    none of them can corrupt Persian text:

    * **NFC** makes a decomposed Persian vowel agree with its composed form, so
      two byte-different spellings of one word do not split a duplicate group.
      The ZWNJ (U+200C) is *not* affected by NFC and is not whitespace, so
      ``می‌رود`` keeps its ZWNJ and stays one token.
    * **case folding** makes ``Python`` and ``python`` one fingerprint. It is
      locale-independent and script-neutral: Persian has no case, and Persian
      text passes through unchanged.
    * **whitespace collapsing** makes a page that wraps differently hash the
      same. It collapses only whitespace; it never removes a character that
      carries meaning, so content differing by anything other than spacing stays
      a distinct fingerprint (FR-25).
    * **SHA-256** for a fixed-length, collision-resistant identity. Python's
      built-in ``hash`` is explicitly not used: it is randomized per process
      (PYTHONHASHSEED), which would make a fingerprint non-reproducible across
      runs — and P3.1 stores content hashes across runs.
    """
    normalized = unicodedata.normalize("NFC", text or "")
    folded = normalized.casefold()
    collapsed = " ".join(folded.split())
    return hashlib.sha256(collapsed.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Duplicate group
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """One set of candidates that represent the same source.

    ``key`` is the canonical identity that groups these: either a canonical URL
    or a content fingerprint. ``members`` are the original candidates in
    insertion order (stable). ``canonical`` is the selected representative.
    """
    key: str
    members: tuple[SourceCandidate, ...]
    canonical: SourceCandidate

    def __post_init__(self) -> None:
        if not self.members:
            raise ValueError("duplicate group must have at least one member")
        if self.canonical not in self.members:
            raise ValueError(
                "canonical representative must be a member of the group"
            )


@dataclass(frozen=True, slots=True)
class RankingFeatures:
    """The normalized signals behind one score (FR-27, § 15.6).

    Every component is in ``[0.0, 1.0]`` and every one is either a measurement
    or a documented derivation — none is a stand-in for a signal the provider did
    not supply. Keeping them here rather than only in the total is what makes a
    score explainable: a reader sees *which* signal put a source where it is,
    without re-deriving the policy.
    """

    provider: float
    """Provider signal: ``relevance`` when the provider exposes one, otherwise
    the position-derived ``1 / rank``. Never both blended, and never invented."""

    title_match: float
    """Fraction of the query's tokens present in the title (Jaccard)."""

    content_match: float
    """Fraction of the query's tokens present in the content (Jaccard)."""

    freshness: float
    """Recency against a caller-supplied reference time; ``0.0`` when the
    provider supplied no date, so an absent date cannot look fresh."""

    quality: float
    """Extraction quality: content depth plus metadata completeness."""

    @property
    def total(self) -> float:
        """The weighted sum of the components, per :func:`_compute_score`."""
        return (
            _PROVIDER_WEIGHT * self.provider
            + _TITLE_WEIGHT * self.title_match
            + _CONTENT_WEIGHT * self.content_match
            + _FRESHNESS_WEIGHT * self.freshness
            + _QUALITY_WEIGHT * self.quality
        )


# ---------------------------------------------------------------------------
# Source candidate
# ---------------------------------------------------------------------------

class SourceCandidate(BaseModel):
    """One candidate to deduplicate and rank.

    Frozen, and the unit of work for P3.5. It carries the identity P3.2
    supplied (``url``, ``domain``, ``rank``, optional ``relevance``,
    ``published_at``) together with the content P3.4 extracted (``title``,
    ``text``). The two are kept distinguishable on purpose: a URL identity and a
    content identity are different claims about sameness, and collapsing the
    difference would let a page whose extraction failed merge with an unrelated
    page that merely shares its URL.

    Fields a provider did not supply stay ``None`` rather than being defaulted to
    a plausible value, because a fabricated value would silently become a ranking
    signal. ``rank`` is required because P3.2's ``SearchResult`` always carries
    one; a ``0`` there means "no position" and yields a provider signal of zero
    rather than being treated as position 1.
    """

    model_config = ConfigDict(frozen=True)

    # Identity from search (P3.2)
    url: str
    domain: str
    rank: int  # provider's 1-based position

    # Provider relevance when — and only when — the provider exposes one
    # (SearchResult.relevance; Brave does not, so this is usually None and the
    # position-based signal is used instead).
    relevance: float | None = None

    # Provider publication date, verbatim. Never compared against a clock here.
    published_at: datetime | None = None

    # Extracted content from P3.4
    title: str | None = None
    text: str = ""
    description: str | None = None
    language: str | None = None

    # Extraction outcome. ``char_count`` is the extracted length in characters.
    extracted_at: str | None = None
    is_empty: bool = False
    char_count: int = 0

    @field_validator("url")
    @classmethod
    def _url_usable(cls, value: str) -> str:
        """Reject a URL with no identity rather than canonicalizing it to nothing.

        This is a *comparability* check, not a safety check. An unparseable URL
        has no canonical form to group by, and admitting it would produce a group
        keyed on ``""`` — silently merging every candidate that failed to parse
        with every other one.
        """
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            raise ValueError("url must be a non-empty string")
        parsed = urlparse(text)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("url must have a scheme and an authority")
        return text

    @field_validator("domain")
    @classmethod
    def _domain_present(cls, value: str) -> str:
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            raise ValueError("domain must be a non-empty string")
        return text

    @field_validator("rank")
    @classmethod
    def _rank_is_position(cls, value: int) -> int:
        """A provider position is 1-based, and ``0`` means "no position".

        P3.2's ``SearchResult`` already requires a 1-based rank, so a non-positive
        value does not arise from a provider result and is not an error here: it is
        treated as an absent position, which makes the provider signal ``0.0``.
        Clamping it to 1 instead would be actively wrong — it would rank an
        unordered result above every ordered one — and rejecting it would mean a
        defensive branch in the one place a missing signal should cost nothing.
        """
        return value

    @property
    def content_hash(self) -> str:
        """Fingerprint of the normalized extracted content."""
        return content_fingerprint(self.text)

    def canonical_url(self) -> str:
        """The canonical URL of this candidate."""
        return canonical_url(self.url)

    @property
    def is_usable(self) -> bool:
        """Whether this candidate carries content worth ranking.

        Requires usable extracted text. A candidate whose extraction produced
        nothing is still *ranked* — it is excluded with a recorded reason
        (FR-30) — but it can never be chosen as a group's representative, which
        is what keeps a failed extraction from hiding a usable sibling.

        The threshold is P3.4's ``MIN_EXTRACTED_CHARS``, reused rather than
        restated: a candidate that P3.4 already calls empty must not be
        considered usable here, or the two stages would disagree about the same
        text.
        """
        return not self.is_empty and self.char_count >= MIN_EXTRACTED_CHARS

    @classmethod
    def from_search_result(
        cls,
        result: SearchResult,
        extracted: ExtractedContent | None = None,
    ) -> SourceCandidate:
        """Build a candidate from a P3.2 result and its P3.4 extraction.

        The one place the two upstream contracts meet, so a later stage does not
        repeat the mapping (and get it subtly different). A result with no
        extraction is a legitimate candidate: extraction may have failed for
        that source, and it must still appear as an excluded source rather than
        vanish (FR-19, FR-30).
        """
        if extracted is None:
            return cls(
                url=result.url,
                domain=result.domain,
                rank=result.rank,
                relevance=result.relevance,
                published_at=result.published_at,
                title=result.title,
                description=result.snippet,
                is_empty=True,
                char_count=0,
            )
        return cls(
            url=result.url,
            domain=result.domain,
            rank=result.rank,
            relevance=result.relevance,
            published_at=result.published_at,
            title=extracted.title or result.title,
            text=extracted.text,
            description=extracted.description or result.snippet,
            language=extracted.language,
            extracted_at=extracted.extracted_at.isoformat(),
            is_empty=extracted.is_empty,
            char_count=extracted.char_count,
        )


@dataclass(frozen=True, slots=True)
class RankedResult:
    """One deduplicated source, scored and placed.

    Frozen, and ``position`` is the 1-based index in the ordered output, assigned
    after sorting — never the provider's own rank, which is preserved on the
    candidate as ``candidate.rank``.

    ``features`` carries the individual normalized signals behind ``score``, so
    the score is explainable without re-running the policy (FR-27) and a
    surprising order can be diagnosed rather than guessed at. ``excluded_reason``
    is set when the source falls below the configured quality threshold (FR-30);
    an excluded result stays in the output, in order, rather than disappearing —
    the stage records what it dropped and why instead of quietly dropping it.
    """

    candidate: SourceCandidate
    score: float
    position: int
    duplicate_group_key: str
    features: RankingFeatures | None = None
    excluded_reason: str | None = None

    @property
    def is_excluded(self) -> bool:
        return self.excluded_reason is not None


class DedupRankingResult(BaseModel):
    """The output of the deduplication stage (the ``SourceDeduplication`` port).

    Frozen and deterministic for identical inputs. It holds both halves of the
    stage's answer: what arrived, what collapsed, and which representative
    survived each collapse. A caller can therefore report the stage's work
    (FR-26) instead of only consuming its verdict.
    """

    model_config = ConfigDict(frozen=True)

    candidates: tuple[SourceCandidate, ...] = ()
    """Every input candidate, in input order, preserved for auditability."""

    duplicate_groups: tuple[DuplicateGroup, ...] = ()
    """Groups of two or more candidates that share an identity.

    A candidate with no duplicate is **not** here. The ranked output carries
    one entry per *canonical source*, which is a superset of these groups, so
    a distinct source is still represented exactly once downstream.
    """

    ranked_results: tuple[RankedResult, ...] = ()
    """One entry per canonical source, ordered.

    Empty when this result has not been ranked yet — the port this type is built
    for returns the ranked form, while the lower-level
    :func:`build_duplicate_groups` returns the deduplicated form alone.
    """

    @property
    def is_empty(self) -> bool:
        return not self.ranked_results

    @property
    def ranked_candidates(self) -> tuple[SourceCandidate, ...]:
        """The deduplicated candidates in rank order."""
        return tuple(r.candidate for r in self.ranked_results)

    @property
    def excluded_results(self) -> tuple[RankedResult, ...]:
        """Ranked results that failed the quality threshold, in rank order."""
        return tuple(r for r in self.ranked_results if r.is_excluded)


# ---------------------------------------------------------------------------
# Dedup policy
# ---------------------------------------------------------------------------

class DedupConfig(BaseModel):
    """The deduplication policy, as explicit configuration (FR-24 … FR-26, FR-29).

    Every knob is a documented decision rather than a constant buried in a
    function, so the policy can be read, reviewed, and replaced as a unit. The
    defaults are the conservative ones: collapse on strong evidence only.

    ``min_group_size`` is 2 and cannot go lower — a group of one is not a
    duplicate, so a smaller value would have no meaning.
    """

    model_config = ConfigDict(frozen=True)

    min_group_size: int = Field(default=2, ge=2)
    """Members required before a cluster is reported as a duplicate group."""

    group_by_url: bool = True
    """Collapse candidates whose canonical URLs are equal.

    On by default: identical canonical URLs are strong evidence of one document,
    and this is the only rule that still works when extraction failed.
    """

    group_by_content: bool = True
    """Collapse candidates whose extracted content fingerprints are equal.

    This is what catches syndication — the same article republished at several
    URLs, which no URL rule can see (FR-24). It compares the *extracted main
    content* only, never the raw page, so two distinct articles that share a
    navigation menu, cookie banner, or footer are **not** collapsed (FR-25).
    """

    fingerprint_usable_only: bool = True
    """Restrict content comparison to candidates with usable content.

    On by default, and the reason is a correctness one: the fingerprint of empty
    text is the fingerprint of every other empty text, so including unextracted
    candidates would merge every source whose extraction failed into a single
    group and lose all of them.
    """


class RankingConfig(BaseModel):
    """The ranking policy as explicit configuration (FR-27 … FR-30, § 15.6).

    Weights are *not* fields. They are module-level named constants
    (:data:`PROVIDER_WEIGHT` and its siblings), because a configurable weight
    invites a configuration that was never measured: an operator could set one to
    a value no benchmark ever justified, and the resulting order would be
    unexplainable while still looking deliberate. What *is* configurable is the
    threshold below, which is an operational decision rather than a scoring
    claim.
    """

    model_config = ConfigDict(frozen=True)

    min_quality_score: float = Field(default=0.10, ge=0.0, le=1.0)
    """Minimum quality signal a source must reach to be kept (FR-30).

    Excluded sources are **not** removed from the output; each carries an
    ``excluded_reason``. A stage that silently dropped rows would make a later
    report unable to say how many sources were seen and why none survived, which
    FR-41 and FR-42 both forbid.

    The default is deliberately low. It admits a usable source with no title and
    no snippet while rejecting one whose content is too thin to cite, which is
    the failure this threshold exists to prevent.
    """


# ---------------------------------------------------------------------------
# Representative selection
# ---------------------------------------------------------------------------

def select_canonical(members: Sequence[SourceCandidate]) -> SourceCandidate:
    """Choose the one representative for a duplicate group (FR-26).

    A lexicographic priority chain rather than a weighted score, because a
    weighted sum would let a strong third criterion *outvote* the first one —
    and the first criterion is not a preference but a precondition. A source
    whose extraction produced nothing must never represent a group that contains
    a usable copy, however well it placed with the provider.

    The chain, in order, each step decisive before the next is consulted:

    1. **Usable extracted content.** Usable beats unusable, unconditionally.
    2. **Extraction quality** — a title (2) plus a description (1), plus content
       depth in 100-character steps capped at 10 so a very long page cannot
       dominate the metadata that was actually present.
    3. **Provider position** — the lower 1-based rank wins, so the copy the
       provider ranked highest represents the group.
    4. **Canonical URL, ascending** — a total order on a value that is unique
       within the group, so the outcome never depends on input order.

    Deterministic for a given set of members regardless of the order they
    arrive in. Raises :class:`~app.domain.errors.InvalidSourceCandidateError`
    for an empty member set, which is a caller defect rather than a policy
    outcome.
    """
    if not members:
        raise InvalidSourceCandidateError(
            "cannot select a canonical representative from an empty group"
        )
    if len(members) == 1:
        return members[0]

    def _priority_prefix(candidate: SourceCandidate) -> tuple[int, int, int]:
        usable = 1 if candidate.is_usable else 0
        quality = 0
        if candidate.title:
            quality += 2
        if candidate.description:
            quality += 1
        quality += min(candidate.char_count // 100, 10)
        # Negated so a lower provider rank sorts higher, and floored so a
        # candidate can never outrank position 1 by having no position.
        rank_score = -max(candidate.rank, 1)
        return (usable, quality, rank_score)

    # Two stable passes, because the final tie-breaker runs in the *opposite*
    # direction to the others: the priority prefix is maximized, the canonical
    # URL is minimized. Sorting by the URL first and then by the prefix keeps
    # both rules without inverting the URL comparison.
    ordered = sorted(members, key=lambda c: c.canonical_url())
    return sorted(ordered, key=_priority_prefix, reverse=True)[0]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def canonical_sources(
    candidates: Sequence[SourceCandidate],
    config: DedupConfig | None = None,
) -> tuple[SourceCandidate, ...]:
    """Reduce candidates to one representative per distinct source, in input order.

    This is the deduplicated candidate set a later stage consumes. A candidate
    with no duplicate *is* its own representative, which is the case that
    dominates real result sets — a batch where most sources are distinct must
    still yield one entry per distinct source, not only the handful that
    collided.

    Returned in input order so the step is order-preserving; ranking is the
    stage that reorders.
    """
    chosen, _urls_by_root = _partition_by_identity(candidates, config or DedupConfig())
    ordered: list[SourceCandidate] = []
    for indices in chosen.values():
        members = tuple(candidates[i] for i in sorted(indices))
        ordered.append(members[0] if len(members) == 1 else select_canonical(members))
    return tuple(ordered)


def build_duplicate_groups(
    candidates: Sequence[SourceCandidate],
    config: DedupConfig | None = None,
) -> tuple[DuplicateGroup, ...]:
    """Group candidates that share an identity (FR-24 … FR-26).

    Identity is the union of two rules, and the union is what makes the result
    independent of input order: candidates are partitioned by canonical URL *and*
    by content fingerprint, then any two partitions that intersect are merged, so
    "same URL" and "same content" form one equivalence rather than two lists that
    can disagree. A candidate reachable through either rule therefore lands in the
    same group as one reached through the other.

    ``DuplicateGroup.key`` is the group's identity: the canonical URL when every
    member shares one, otherwise the shared content fingerprint. Keys are unique
    across groups, so the key is usable as a stable identifier in the persisted
    source row.

    Only clusters of ``min_group_size`` or more are returned. A distinct source
    is not a duplicate group — it is reported through
    :func:`canonical_sources` and the ranked output instead.
    """
    cfg = config or DedupConfig()
    chosen, _ = _partition_by_identity(candidates, cfg)

    result: list[DuplicateGroup] = []
    for indices in chosen.values():
        if len(indices) < cfg.min_group_size:
            continue
        members = tuple(candidates[i] for i in sorted(indices))
        canonical_urls = {m.canonical_url() for m in members}
        key = (
            members[0].canonical_url()
            if len(canonical_urls) == 1
            else members[0].content_hash
        )
        result.append(
            DuplicateGroup(key=key, members=members, canonical=select_canonical(members))
        )

    # Sorted by key so the tuple is in a stable order rather than in dict-insertion
    # order, which would otherwise depend on the input sequence.
    result.sort(key=lambda g: g.key)
    return tuple(result)


def _partition_by_identity(
    candidates: Sequence[SourceCandidate],
    config: DedupConfig,
) -> tuple[dict[int, list[int]], dict[int, str]]:
    """Partition candidate indices by URL and content identity, then merge.

    Returns ``(clusters_by_root, canonical_url_by_root)``. The clustering uses
    union-find with path halving, which is near-linear: a batch of search
    results is dominated by two dictionary lookups and one SHA-256 per candidate,
    never a pairwise comparison of every candidate against every other (§ 15.7).

    Iteration is over sorted indices and the return order is normalized by the
    caller, so no set or dict iteration order reaches the output (T-14).
    """
    url_map: dict[str, list[int]] = {}
    fingerprint_map: dict[str, list[int]] = {}
    parent: dict[int, int] = {}

    for index, candidate in enumerate(candidates):
        parent[index] = index

        if config.group_by_url:
            url_map.setdefault(candidate.canonical_url(), []).append(index)

        if (
            config.group_by_content
            and (candidate.is_usable or not config.fingerprint_usable_only)
        ):
            fingerprint_map.setdefault(candidate.content_hash, []).append(index)

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            # Always attach the higher index to the lower one, so the resulting
            # root does not depend on the order the groups were encountered.
            low, high = sorted((left_root, right_root))
            parent[high] = low

    for buckets in (url_map, fingerprint_map):
        for indices in buckets.values():
            first = indices[0]
            for index in indices[1:]:
                union(first, index)

    clusters: dict[int, list[int]] = {}
    for index in range(len(candidates)):
        clusters.setdefault(find(index), []).append(index)

    canonical_urls = {
        root: candidates[indices[0]].canonical_url() for root, indices in clusters.items()
    }
    return clusters, canonical_urls


def build_ranked_results(
    duplicate_groups: Sequence[DuplicateGroup],
    all_candidates: Sequence[SourceCandidate],
    request_query: str,
) -> tuple[RankedResult, ...]:
    """Rank the deduplicated candidates — the pre-port entry point.

    Kept because it is the shape this stage was specified in: explicit groups in,
    ordered results out. :func:`rank_sources` is the port-shaped equivalent and
    takes a ``DedupRankingResult``; this builds one from its two arguments and
    returns just the ranked half. New callers should prefer
    :func:`process_candidates`, which does deduplication and ranking together and
    returns both halves plus the groups — this signature makes it easy to rank
    *only* the reported duplicate groups and silently omit every distinct source,
    which is precisely the defect the port shape removes.
    """
    deduplicated = DedupRankingResult(
        candidates=tuple(all_candidates),
        duplicate_groups=tuple(duplicate_groups),
    )
    return rank_sources(deduplicated, request_query).ranked_results


def deduplicate(
    candidates: Sequence[SourceCandidate],
    config: DedupConfig | None = None,
) -> DedupRankingResult:
    """The ``SourceDeduplication`` implementation: candidates in, deduplicated out.

    Records every input, the groups that collapsed, and the deduplicated
    candidate set — the three things a caller needs to report the stage's work
    (FR-26) and to persist one row per distinct source with its
    duplicate-of relationship. No ranking happens here; that is the
    ``RankingPolicy`` port's job, and keeping the two apart is what lets either
    be replaced on its own.
    """
    cfg = config or DedupConfig()
    ordered = tuple(candidates)
    return DedupRankingResult(
        candidates=ordered,
        duplicate_groups=build_duplicate_groups(ordered, cfg),
    )


def rank_sources(
    result: DedupRankingResult,
    query: str,
    *,
    config: RankingConfig | None = None,
    reference_time: datetime | None = None,
) -> DedupRankingResult:
    """The ``RankingPolicy`` implementation: score, order, and threshold.

    Scores one entry per canonical source and returns the same
    ``DedupRankingResult`` with ``ranked_results`` filled in, so the caller keeps
    the candidates and duplicate groups it already had.

    ``reference_time`` is the freshness anchor and is an **input, not a clock**.
    Freshness therefore cannot change between two runs over the same data, which
    is what makes a stored ranking reproducible (§ 15.6). When it is ``None``,
    freshness contributes nothing rather than being measured against
    ``datetime.now()``: an absent anchor must not manufacture a recency signal.

    Raises :class:`~app.domain.errors.RankingInputError` when
    ``reference_time`` is naive, because comparing it against a provider's
    timezone-aware date is a bug that would otherwise surface as a wrong order
    rather than as an error.
    """
    cfg = config or RankingConfig()
    if reference_time is not None and reference_time.tzinfo is None:
        raise RankingInputError(
            "reference_time must be timezone-aware; a naive value cannot be "
            "compared with a provider publication date"
        )

    query_tokens = tokenize(query)
    scored: list[tuple[str, SourceCandidate, RankingFeatures, str | None]] = []
    for key, candidate in _canonical_entries(result):
        features = _extract_features(candidate, query_tokens, reference_time, cfg)
        scored.append((key, candidate, features, _exclusion_reason(candidate, features, cfg)))

    scored.sort(key=_sort_key)
    ranked = tuple(
        RankedResult(
            candidate=candidate,
            score=features.total,
            position=position,
            duplicate_group_key=key,
            features=features,
            excluded_reason=excluded,
        )
        for position, (key, candidate, features, excluded) in enumerate(scored, start=1)
    )
    return result.model_copy(update={"ranked_results": ranked})


def process_candidates(
    candidates: Sequence[SourceCandidate],
    query: str,
    *,
    dedup_config: DedupConfig | None = None,
    ranking_config: RankingConfig | None = None,
    reference_time: datetime | None = None,
) -> DedupRankingResult:
    """The whole stage in one call: canonicalize, group, select, score, order.

    Pure and side-effect free — no clock, no I/O, no randomness — so it is
    testable without any of the surrounding infrastructure, which is the point of
    this layer. It is also the shape a later orchestration stage consumes: one
    call, one result.
    """
    deduplicated = deduplicate(candidates, dedup_config or DedupConfig())
    return rank_sources(
        deduplicated,
        query,
        config=ranking_config,
        reference_time=reference_time,
    )


def _canonical_entries(
    result: DedupRankingResult,
) -> tuple[tuple[str, SourceCandidate], ...]:
    """One ``(group_key, representative)`` pair per distinct source, deterministically.

    A duplicate group contributes its chosen representative; every candidate
    outside any group contributes itself. That union is what makes the ranked
    output cover the whole candidate set while containing no two entries from the
    same group (T-12).

    Pairs are keyed by canonical URL and sorted, so an ungrouped candidate cannot
    depend on where it sat in the input sequence.
    """
    grouped: dict[str, SourceCandidate] = {}
    claimed_urls: set[str] = set()

    for group in result.duplicate_groups:
        grouped[group.key] = group.canonical
        claimed_urls.update(member.canonical_url() for member in group.members)

    for candidate in result.candidates:
        url = candidate.canonical_url()
        if url in claimed_urls:
            continue
        # Two candidates can share a canonical URL without having been grouped
        # only if grouping was disabled; they must not both reach the output.
        if url in grouped:
            continue
        grouped[url] = candidate

    return tuple(sorted(grouped.items(), key=lambda item: item[0]))


# ---------------------------------------------------------------------------
# Scoring model (FR-27, FR-28)
# ---------------------------------------------------------------------------
#
# Every weight below is named, documented, and normalized to the range
# [0.0, 1.0] per signal, so the score is explainable rather than a number that
# happened to come out of a function. The total is bounded by the sum of the
# weights, so it is comparable across runs and across stages.
#
#     score = PROVIDER_WEIGHT * provider
#           + TITLE_WEIGHT    * title_match
#           + CONTENT_WEIGHT  * content_match
#           + FRESHNESS_WEIGHT* freshness
#           + QUALITY_WEIGHT  * quality
#
# Each signal is a measurement or a documented derivation. None of them stands in
# for a signal the upstream contract does not provide: when a provider exposes no
# relevance number the provider signal falls back to its own ordering rather than
# inventing a score, and when there is no publication date the freshness signal is
# zero rather than assumed.

#: Provider ordering or relevance. Highest weight after the title: the provider's
#: own ordering is the only external judgement of relevance available, and it is
#: the one signal that survives when a page has no text to compare.
PROVIDER_WEIGHT = 0.30

#: Query/title overlap. Weighted highest, because a title that matches the
#: question is the strongest evidence available *before* reading the body, and
#: it is the signal least affected by page length or boilerplate.
TITLE_WEIGHT = 0.30

#: Query/content overlap. Lower than the title because the body of a page matches
#: a query incidentally far more often than its title does — a long article
#: accumulates query terms it is not actually about.
CONTENT_WEIGHT = 0.20

#: Recency. Small, deliberately: freshness is a tie-breaker between otherwise
#: comparable sources, not evidence of relevance. An undated source scores zero
#: here and is never punished relative to a dated one by more than this weight.
FRESHNESS_WEIGHT = 0.10

#: Extraction quality and metadata completeness. Smallest of the content signals:
#: it distinguishes a well-extracted page from a poorly extracted one, which
#: matters, but it must not let a long page outrank a directly relevant short one.
QUALITY_WEIGHT = 0.10

#: Content depth is credited up to this many characters; beyond it, more text adds
#: nothing to the signal. Without a cap, one very long page would saturate the
#: quality signal and the metadata component would stop mattering.
QUALITY_DEPTH_CHARS = 1000
#: Share of the quality signal attributable to content depth; the remainder is
#: metadata completeness (title, description, provider snippet).
QUALITY_DEPTH_SHARE = 0.5

#: Freshness half-life. A source this many days old scores 0.5 on the freshness
#: signal; the curve is exponential, so the weight decays smoothly and never
#: reaches zero, which keeps a very old source comparable rather than excluded.
FRESHNESS_HALFLIFE_DAYS = 90.0

# Private aliases for the public weight names. The weights are the policy's
# documented interface — the audit's formula names them — so they are public
# constants; these aliases keep the underscore-prefixed names that existed before
# the model was documented importable, rather than breaking a caller over a
# naming convention.
_PROVIDER_WEIGHT = PROVIDER_WEIGHT
_TITLE_WEIGHT = TITLE_WEIGHT
_CONTENT_WEIGHT = CONTENT_WEIGHT
_FRESHNESS_WEIGHT = FRESHNESS_WEIGHT
_QUALITY_WEIGHT = QUALITY_WEIGHT

#: Tokens shorter than this are dropped unless they are a letter, which keeps
#: single-letter words in Persian and English queries.
MIN_TOKEN_LENGTH = 2

#: Splits on everything that is not a word character, with one exception: ZWNJ.
#:
#: Python's ``\w`` is Unicode-aware by default, so Persian and Arabic letters and
#: digits already match it — no script range needs listing here. ZWNJ (U+200C) is
#: added explicitly because it is a formatting character rather than a word
#: character, and Persian writes several words joined by it; splitting on it would
#: shred exactly the compounds an English tokenizer keeps whole (``می‌رود``,
#: ``نمی‌توان``).
#:
#: The Arabic-script punctuation block is deliberately **not** whitelisted. U+060C
#: (Arabic comma) and U+061F (Arabic question mark) sit inside U+0600-U+06FF, so a
#: regex that preserved that range would keep the punctuation glued to the word
#: and ``چیست؟`` would become a token that never matches a query's
#: ``چیست``. Relying on ``\w`` gets Persian punctuation right for free.
_TOKEN_SPLIT_RE = re.compile("[^\\w\u200c]+")

#: Upper bound on the quality signal, named so the normalization contract is
#: visible at the point of use rather than implied by the arithmetic.
_MAX_QUALITY = 1.0


def _tokenize_query(query: str) -> frozenset[str]:
    """Backward-compatible alias for :func:`tokenize`.

    The P3.5 tests and any later caller may use either name; both are the same
    documented policy, and keeping one implementation means the alias cannot drift
    from the function it names.
    """
    return tokenize(query)


def _tokenize_text(text: str) -> frozenset[str]:
    """Backward-compatible alias for :func:`tokenize` (same policy)."""
    return tokenize(text)


def _compute_score(candidate: SourceCandidate, query_tokens: frozenset[str]) -> float:
    """Score one candidate against pre-tokenized query terms.

    Retained as the direct entry point for callers that already hold the tokenized
    query — scoring a batch without re-tokenizing the query per candidate. The
    policy is :meth:`RankingFeatures.total`; this is the same arithmetic.
    """
    features = _extract_features(candidate, query_tokens, None, RankingConfig())
    return features.total


def _provider_signal_legacy(candidate: SourceCandidate) -> float:
    """Alias kept for the pre-P3.5 name of the provider signal."""
    return _provider_signal(candidate)


def _title_match(title: str | None, query_tokens: frozenset[str]) -> float:
    """Overlap between a title and the query terms, in ``[0.0, 1.0]``."""
    return _token_overlap(tokenize(title or ""), query_tokens)


def _content_match(text: str, query_tokens: frozenset[str]) -> float:
    """Overlap between the content and the query terms, in ``[0.0, 1.0]``."""
    return _token_overlap(tokenize(text), query_tokens)


def _quality_signal_legacy(candidate: SourceCandidate) -> float:
    """Alias kept for the pre-P3.5 name of the quality signal."""
    return _quality_signal(candidate)


def tokenize(text: str) -> frozenset[str]:
    """Split text into the tokens the matching signals are computed over.

    The normalization policy, in order, and the reason for each step:

    * **NFC** — so a Persian vowel written decomposed and a query's composed
      form are the same token.
    * **case folding** — Unicode-aware and locale-independent. It folds Latin
      case; Persian has none, so Persian text is unchanged by it. Using
      ``lower()`` instead would break the German and Turkish edge cases for no
      benefit here.
    * **split on non-word characters, preserving ZWNJ** — punctuation and symbols
      become separators. ZWNJ (U+200C) is explicitly kept inside a token,
      because Persian writes several words joined by it and splitting it would
      shred exactly the words an English tokenizer would have kept whole.
    * **drop tokens below two characters, unless the token is a letter** — this
      removes punctuation fragments and stray digits without discarding ``می`` or
      ``a``, which are real query terms.

    Deliberately absent: no stemming, no lemmatization, no stop-word list, no
    transliteration, no translation, and no language detection. Each would need a
    per-language policy and a measurement to justify; none exists in the
    architecture yet, and a silent English stemmer applied to Persian text is
    precisely the failure this stage must not introduce (§ 15.6).

    Matching is on whole tokens, never on raw substrings: a substring test would
    score ``AI`` as matching inside ``said``, and ``cat`` inside ``education``.
    """
    if not text:
        return frozenset()
    folded = unicodedata.normalize("NFC", text).casefold()
    tokens = _TOKEN_SPLIT_RE.sub(" ", folded).split()
    return frozenset(token for token in tokens if len(token) >= MIN_TOKEN_LENGTH or _is_letter(token))


def _is_letter(token: str) -> bool:
    """Whether a single-character token is a letter worth keeping."""
    return len(token) == 1 and token.isalpha()


def _extract_features(
    candidate: SourceCandidate,
    query_tokens: frozenset[str],
    reference_time: datetime | None,
    config: RankingConfig,
) -> RankingFeatures:
    """Compute the five normalized ranking signals for one candidate."""
    return RankingFeatures(
        provider=_provider_signal(candidate),
        title_match=_token_overlap(tokenize(candidate.title or ""), query_tokens),
        content_match=_token_overlap(tokenize(candidate.text), query_tokens),
        freshness=_freshness_signal(candidate.published_at, reference_time),
        quality=_quality_signal(candidate),
    )


def _token_overlap(
    document_tokens: frozenset[str], query_tokens: frozenset[str]
) -> float:
    """Jaccard overlap of two token sets, in ``[0.0, 1.0]``.

    Jaccard rather than containment because it is symmetric and bounded: it
    cannot be inflated by a document that happens to contain every query term
    among ten thousand others, and it cannot exceed 1.0. An empty query yields
    ``0.0`` rather than raising or dividing by zero — a source with no query to
    match is unmatched, not perfectly matched.
    """
    if not document_tokens or not query_tokens:
        return 0.0
    union = len(document_tokens | query_tokens)
    return len(document_tokens & query_tokens) / union


def _provider_signal(candidate: SourceCandidate) -> float:
    """The provider's judgement of this result, in ``[0.0, 1.0]``.

    Two cases, and the choice between them is the upstream contract's, not this
    stage's: when the provider exposes a relevance number
    (``SearchResult.relevance``) that number is used; otherwise the provider's own
    ordering is used as ``1 / rank``, which preserves the provider's ranking while
    compressing it into the signal range — position 1 is 1.0, position 2 is 0.5.

    When neither is available the signal is ``0.0``. It is never replaced by a
    default that would make an unranked result look like a good one: an absent
    signal must cost the source nothing and gain it nothing.
    """
    if candidate.relevance is not None and candidate.relevance > 0:
        return min(float(candidate.relevance), 1.0)
    if candidate.rank > 0:
        return 1.0 / candidate.rank
    return 0.0


def _freshness_signal(published_at: datetime | None, reference_time: datetime | None) -> float:
    """Recency in ``[0.0, 1.0]``, measured against a caller-supplied anchor.

    Exponential decay with a documented half-life: a source published at the
    reference time scores 1.0, one half-life old scores 0.5, and the value
    approaches but never reaches 0. Both inputs being required is the point —
    without ``reference_time`` there is no "now" to measure against, and using the
    wall clock would make the ranking of a stored candidate set change on its own.

    A publication date in the future (a provider clock skew, or a page dated ahead)
    scores the same as one at the reference time rather than exceeding 1.0: a
    future date is not evidence of quality, and it must not let a mistimed source
    outrank everything.
    """
    if published_at is None or reference_time is None:
        return 0.0
    published = published_at if published_at.tzinfo else published_at.replace(tzinfo=UTC)
    anchor = reference_time if reference_time.tzinfo else reference_time.replace(tzinfo=UTC)
    age_days = (anchor - published).total_seconds() / 86_400
    if age_days <= 0:
        return 1.0
    return float(2.0 ** (-age_days / FRESHNESS_HALFLIFE_DAYS))


def _quality_signal(candidate: SourceCandidate) -> float:
    """Extraction quality and metadata completeness, in ``[0.0, 1.0]``.

    Half the signal is content depth — extracted characters up to
    :data:`QUALITY_DEPTH_CHARS` — and half is metadata completeness: a title and
    a provider snippet each count, so a well-described page outranks an equally
    long bare one. A source with no usable content scores 0.0, which is what makes
    the FR-30 threshold meaningful.
    """
    depth = min(candidate.char_count / QUALITY_DEPTH_CHARS, 1.0)
    metadata = 0.0
    if candidate.title:
        metadata += 0.5
    if candidate.description:
        metadata += 0.5
    if not candidate.is_usable:
        return 0.0
    return QUALITY_DEPTH_SHARE * depth + (1.0 - QUALITY_DEPTH_SHARE) * metadata


def _exclusion_reason(
    candidate: SourceCandidate,
    features: RankingFeatures,
    config: RankingConfig,
) -> str | None:
    """Why this source is excluded, or ``None`` if it is kept (FR-30).

    The reason is recorded on the result rather than acted on by removing the
    entry: the stage must be able to report what it excluded and why (FR-42),
    and a caller may legitimately include an excluded source anyway. Excluded
    results stay in rank order so that "excluded" is a filter over a complete
    ordering, not a different ordering.
    """
    if not candidate.is_usable:
        return "excluded: source content unusable (empty or too short to cite)"
    if features.quality < config.min_quality_score:
        return f"excluded: quality {features.quality:.3f} below threshold {config.min_quality_score:.3f}"
    return None


def _sort_key(
    item: tuple[str, SourceCandidate, RankingFeatures, str | None],
) -> tuple[float, float, int, int, str]:
    """The full ordering key: score, then documented tie-breakers, then identity.

    Every component is derived from stable field values, never from position in
    the input or from a set or dict iteration order, so the same candidates in any
    input order produce the same ordering (T-14).

    The tie-breakers, in order, each resolving a case the previous one leaves
    open:

    1. **score, descending** — the ranking itself.
    2. **provider signal, descending** — sources that score identically are
       separated by the provider's own judgement, which is external evidence
       rather than an arbitrary choice.
    3. **extracted length, descending** — longer content breaks a remaining tie on
       substance.
    4. **provider position, ascending** — the exact position, which can still differ
       where two positions produced the same signal (a provider relevance number
       overrides position in step 2).
    5. **group key, ascending** — a total order over a value unique within a run,
       so the ordering is complete and reproducible. Two candidates sharing a
       canonical URL would already have been grouped.

    Excluded and non-excluded sources are **not** separated: the ordering is the
    same single ordering, and exclusion is a recorded property of an entry rather
    than a separate list. Sorting excluded sources to the bottom would make the
    positions of the kept sources depend on which weak sources happened to be in
    the batch.
    """
    key, candidate, features, _excluded = item
    return (
        -features.total,
        -features.provider,
        -candidate.char_count,
        candidate.rank,
        key,
    )
