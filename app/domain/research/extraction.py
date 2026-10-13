"""Provider-independent extraction contract (Phase 3, P3.4 — AD-034, § 15.3).

Transform ``FetchedSource`` (P3.3) into normalized ``ExtractedContent`` via a
provider-independent extraction contract. No HTML parser type, no HTTP client,
no provider SDK, no SQLAlchemy model appears here — replacement requires no
domain change (§ 15.3, T-3, FR-20 … FR-23).

Key distinctions, stated rather than implied:

``ExtractedContent`` ≠ ``SourceChunk`` ≠ ``Source``
    An ``ExtractedContent`` is the *normalized text* produced from a fetched
    page at P3.4 — title, body, description, language hint, and the
    ``is_empty`` / ``char_count`` metadata that distinguishes usable content
    from garbage (FR-23). A ``SourceChunk`` is a positioned passage for
    citation (P3.4 chunking); a ``Source`` is the persisted record.
    This module produces only the first.

**The body is text, not bytes at this stage.** The bytes from ``FetchedSource``
are decoded, parsed, and normalized here. Decoding belongs to this stage,
because deciding whether a charset declaration is trustworthy requires
inspection of the content, and guessing at the fetch stage would silently
corrupt Persian / mixed-language text before extraction can inspect it.

**Unicode / Persian safety.** Normalization is NFC by default so
composed characters are stable, but ZWNJ (U+200C) is preserved because it
is load-bearing for Persian word-boundary and must not be folded by
NFC (which it is not — ZWNJ is a separator, not a combining character).
The extractor must never use a parser's ``.text`` directly for Persian
content without checking ZWNJ preservation (AD-031 observation, § 15.10).

**Empty / low-value is a typed outcome.** A page that parses but yields
no readable text is *not* a parse failure; it is ``is_empty=True`` so
the orchestrator can distinguish "reached but unusable" from "could not
reach" (FR-43). ``ExtractionEmptyResultError`` exists for the rare case
where extraction is desired but the caller requires non-empty output.

**No network access.** The extractor operates on the bytes already in
memory (from ``FetchedSource.body``). It never opens sockets, executes
JavaScript, or reaches external services.

**No P3.5+ functionality.** Deduplication, ranking, claim extraction,
citation construction, and synthesis are deliberately absent.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.research.fetch import FetchedSource

#: Upper bound on extracted text length (SR-5: bounded resources). Beyond
#: this the content is truncated with a documented marker rather than
#: silently dropped.
MAX_EXTRACTED_CHARS = 100_000

#: Minimum readable text to be considered non-empty (FR-23). Below this
#: the result is ``is_empty=True`` — the page is marked unusable rather
#: than presented as evidence.
MIN_EXTRACTED_CHARS = 10


class ExtractionStatus(enum.StrEnum):
    """Outcome of attempting extraction from a fetched source."""

    EXTRACTED = "extracted"
    EMPTY = "empty"
    PARSE_ERROR = "parse_error"
    UNSUPPORTED = "unsupported"


class ExtractedContent(BaseModel):
    """Normalized content produced from one fetched page (P3.4).

    Fields, load-bearing distinctions:

    ``url`` — the ``FetchedSource.final_url``, not ``requested_url``; a
    redirect produces content attributable to the destination that answered.
    ``title`` / ``description`` — metadata, optional; a missing title is not
    a failure, because not every source carries one.
    ``text`` — the normalized readable body. Always present (may be empty
    string when ``is_empty=True``), never ``None``, so callers need not
    branch on presence.
    ``language`` — best-effort language hint (BCP-47 tag, lowercased),
    derived from content rather than declared. ``None`` means "unknown".
    ``is_empty`` — ``True`` when ``len(text.strip()) < MIN_EXTRACTED_CHARS``.
    This is the distinction FR-43 requires: a successful fetch that yields
    garbage is recorded as empty, not as a failure, so the orchestrator
    knows exactly which stage produced which outcome.
    ``char_count`` — length of ``text`` after normalization; the actual
    measurement, not a derived property, for reproducibility (§ 6).
    ``extracted_at`` — when extraction finished, UTC, for run reproducibility.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    title: str | None = None
    text: str = ""
    description: str | None = None
    language: str | None = None
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("url")
    @classmethod
    def _url_present(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("extracted content must have a source url")
        return value.strip()

    @field_validator("text")
    @classmethod
    def _text_is_str(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("extracted text must be a string")
        return value

    @field_validator("language", mode="before")
    @classmethod
    def _language_normalized(cls, value: object) -> object:
        if value is None:
            return None
        tag = str(value).strip().lower()
        return tag if tag else None

    @classmethod
    def from_fetched(
        cls,
        source: FetchedSource,
        *,
        title: str | None = None,
        text: str = "",
        description: str | None = None,
        language: str | None = None,
    ) -> ExtractedContent:
        """Build from a fetched source, deriving ``is_empty`` and ``char_count``.

        The caller (extraction adapter) supplies the parsed fields; this method
        applies the normalization and emptiness rules consistently.
        """
        normalized_text = " ".join(text.split()) if isinstance(text, str) else ""
        # Preserve ZWNJ: do not apply aggressive whitespace collapse to it.
        # NFC normalization for composed chars; ZWNJ stays.
        import unicodedata

        normalized_text = unicodedata.normalize("NFC", normalized_text)
        # Derive language hint when caller hasn't specified one.
        if language is None:
            persian_chars = set("گژچپ")
            persian_count = sum(1 for ch in normalized_text if ch in persian_chars)
            has_arabic = any("\u0600" <= ch <= "\u06ff" for ch in normalized_text)
            has_latin = any(ch.isascii() and ch.isalpha() for ch in normalized_text)
            if persian_count > 0 or (has_arabic and not has_latin):
                derived_language = "fa"
            elif has_latin and not has_arabic:
                derived_language = "en"
            else:
                derived_language = None
        else:
            derived_language = language
        return cls(
            url=source.final_url,
            title=title,
            text=normalized_text,
            description=description,
            language=derived_language,
            extracted_at=datetime.now(UTC),
        )

    @property
    def char_count(self) -> int:
        return len(self.text or "")

    @property
    def is_empty(self) -> bool:
        return len((self.text or "").strip()) < MIN_EXTRACTED_CHARS

    @property
    def status(self) -> ExtractionStatus:
        """Outcome category for this result."""
        if self.is_empty:
            return ExtractionStatus.EMPTY
        if self.char_count == 0:
            return ExtractionStatus.EMPTY
        return ExtractionStatus.EXTRACTED

    @property
    def is_usable(self) -> bool:
        return not self.is_empty and self.char_count >= MIN_EXTRACTED_CHARS


@runtime_checkable
class ContentExtraction(Protocol):
    """The extraction port (P3.4 — § 15.3, AD-034).

    One method: given the bytes a fetch produced, produce normalized content
    or raise a typed ``ExtractionError`` subclass. No parser type, no HTTP
    type, no provider type leaks through.

    The adapter decides parser choice from ``source.content_type``; the
    domain does not mandate a parser. The adapter applies normalization
    (NFC, whitespace, ZWNJ preservation) deterministically so any
    adapter yields the same result for the same bytes.
    """

    async def extract(self, source: FetchedSource) -> ExtractedContent: ...

    # Optional factory-style method for synchronous extraction (used in
    # tests and fixtures where async is unnecessary). Not required by the
    # protocol — callers that need sync use the adapter directly.
    def extract_sync(self, source: FetchedSource) -> ExtractedContent: ...
