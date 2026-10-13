"""Provider-independent fetch contract (Phase 3, P3.3).

The types here are what the Research Engine reasons about *after* the network
call: *a request to retrieve one untrusted URL*, and *the bounded response that
came back*. No HTTP client type, no DNS object, no ``ipaddress`` predicate, and
no SQLAlchemy model appears in this module, so the fetching boundary can be
replaced without reaching domain code (§ 15.3, AD-031).

Two distinctions are load-bearing and are stated here rather than implied:

``FetchedSource`` ≠ ``Source`` ≠ ``SourceChunk``
    A :class:`FetchedSource` is *raw bytes plus the metadata needed to interpret
    them*, and nothing else. A ``Source``
    (:mod:`app.domain.research.types`) is the persisted record of a candidate and
    carries the pipeline's status and failure reason. A ``SourceChunk`` is
    extracted text at P3.4. **P3.3 produces only the first.** It performs no
    HTML parsing, no readability extraction, no cleaning, and no chunking; a
    body that is complete garbage is still a successful fetch, because deciding
    that it is garbage is P3.4's job and folding the two together would make
    "could not reach" and "reached but unusable" indistinguishable — the exact
    conflation FR-43 forbids.

**A fetch is not a retrieval.** Reaching a URL proves nothing about its content.
Nothing in this module claims relevance, authority, freshness, or quality; the
selection policy at P3.5 owns those.

**The body is bytes, not text.** Decoding a charset, stripping markup, and
normalizing whitespace are P3.4's work. What is preserved here is what the
server actually sent (:attr:`FetchedSource.content_encoding`,
:attr:`FetchedSource.charset`), so that stage can make an informed choice
instead of guessing. Decoding here would also mean a mis-declared charset
silently corrupts the bytes before anything has inspected them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Upper bound on one URL's length. A URL longer than this is either a
#: programming error or an attempt to push an unbounded string through the
#: parser, the resolver, and the logs.
MAX_URL_CHARS = 2048

#: Default ports per scheme, used to decide whether a ``Host`` header needs an
#: explicit port. Kept here so the domain and the transport agree; the transport
#: reads it rather than hard-coding 80/443 twice.
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _optional_text(value: object) -> str | None:
    """Absent, null and empty text all normalize to ``None``."""
    if value is None:
        return None
    return " ".join(str(value).split()) or None


def _safe_url_for_display(url: str) -> str:
    """Reduce a URL to what is safe to store and log: scheme, host, port, path.

    Two things are removed, for the same reason — they carry secrets:

    * the **query string**, which is where signed-URL tokens, session
      identifiers, and search parameters live. A research run records and logs the
      URLs it fetched, so the query is dropped at the boundary rather than at each
      call site;
    * any **userinfo**, because ``urlsplit`` keeps it in ``netloc``. The fetcher
      refuses URLs carrying credentials outright, so this cannot normally fire —
      but the type that *records* a URL is the last place a password could be
      persisted, and it should not depend on the fetcher having been careful.

    The path is retained: it distinguishes one page of a site from another during
    an incident, and carries no credential in any scheme this fetcher accepts.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url[:MAX_URL_CHARS]

    host = parts.hostname or ""
    if ":" in host:  # an IPv6 literal needs its brackets back
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host

    return parts._replace(netloc=netloc, query="", fragment="").geturl()[:MAX_URL_CHARS]


class FetchRequest(BaseModel):
    """One retrieval of one untrusted URL.

    Deliberately thin: a URL and nothing else. Headers, credentials, method,
    and body are all absent by construction, because every one of them is a way
    to smuggle state into a request the boundary must keep honest. If a future
    stage needs one, adding it here is a deliberate, reviewed change — not
    something a caller can already reach for.

    The URL is *not* normalized here. ``urlsplit`` would happily accept forms
    this fetcher must reject (alternate IP encodings, embedded credentials), so
    parsing and rejection belong to the SSRF boundary, which is the single place
    allowed to decide what a URL means.
    """

    model_config = ConfigDict(frozen=True)

    url: str

    @field_validator("url")
    @classmethod
    def _present_and_bounded(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("must be a string")
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("must be a non-empty url")
        if len(trimmed) > MAX_URL_CHARS:
            raise ValueError(f"must be at most {MAX_URL_CHARS} characters")
        return trimmed


class FetchedSource(BaseModel):
    """A retrieved page: bounded bytes plus the metadata needed to read them.

    Fields, and why each is here:

    ``requested_url`` / ``final_url``
        What was asked for, and what answered after redirects. Both are recorded
        without query strings. They differ whenever a redirect was followed, and
        a source whose final URL is not its advertised URL is a fact the later
        stages need for attribution (a syndicated copy, a paywall interstitial,
        a canonical redirect).
    ``status_code``
        Kept so a 4xx/5xx page is never presented as retrieved content. Only a
        2xx reaches this object, so this is provenance, not policy.
    ``content_type``
        The media type the server declared. It was validated against the
        allowlist before the body was processed (FR-17); the value is preserved
        because P3.4 selects a parser from it.
    ``content_encoding`` / ``charset``
        The transfer encoding that was decoded and the character set declared.
        Preserved so P3.4 can decode deliberately rather than guess.
    ``body``
        The decompressed bytes, already inside the size bound. Bytes, not text:
        see the module docstring.
    ``redirect_chain``
        Every URL actually visited, in order, each already validated. It is what
        makes "the fetcher checked every hop" auditable after the fact.
    ``fetched_at`` / ``elapsed_ms``
        When the bytes arrived and how long it took. Both are needed for run
        reproducibility (§ 6) and for attributing a slow source.

    Deliberately **absent**: extracted text, chunks, claims, citations, rank,
    quality, and any report structure. Those are P3.4 and later, and their
    absence here is what keeps this a transport contract.
    """

    model_config = ConfigDict(frozen=True)

    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    content_encoding: str | None = None
    charset: str | None = None
    body: bytes = b""
    redirect_chain: tuple[str, ...] = ()
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    elapsed_ms: int = 0

    @field_validator("requested_url", "final_url", "content_type")
    @classmethod
    def _present(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return _safe_url_for_display(value.strip())

    @field_validator("content_encoding", "charset", mode="before")
    @classmethod
    def _normalize_optional(cls, value: object) -> object:
        return _optional_text(value)

    @field_validator("redirect_chain")
    @classmethod
    def _normalize_chain(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = []
        for entry in value:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("each redirect hop must be a non-empty url")
            cleaned.append(_safe_url_for_display(entry.strip()))
        return tuple(cleaned)

    @field_validator("fetched_at", mode="before")
    @classmethod
    def _default_fetched_at(cls, value: object) -> object:
        if value is None:
            return datetime.now(UTC)
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @field_validator("status_code")
    @classmethod
    def _success_only(cls, value: int) -> int:
        if not 200 <= value <= 299:
            raise ValueError("a fetched source must be a 2xx response")
        return value

    @field_validator("elapsed_ms")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @property
    def byte_count(self) -> int:
        """Size of the decoded body. The bound the SSRF boundary enforced."""
        return len(self.body)

    @property
    def was_redirected(self) -> bool:
        """Whether any hop was followed to reach this response."""
        return len(self.redirect_chain) > 1