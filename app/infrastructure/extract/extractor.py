"""Content extraction infrastructure (Phase 3, P3.4 — AD-031, AD-034).

Uses ``trafilatura`` 2.3.1 (pure-Python, no network on string input) with
explicit ZWNJ-preservation: ``Document.text`` strips U+200C; we extract
from the XML body via ``itertext()`` so Persian word-boundary is kept.

No parser type, no HTTP type leaks through: the adapter speaks only
``FetchedSource`` → ``ExtractedContent``, matching ``ContentExtraction``.
"""

from __future__ import annotations

import logging
import unicodedata

import trafilatura

from app.core.config import Settings
from app.domain.errors import (
    ExtractionParseError,
    ExtractionUnsupportedContentError,
)
from app.domain.research.extraction import (
    ExtractedContent,
)
from app.domain.research.fetch import FetchedSource
from app.domain.research.ports import ContentExtraction

log = logging.getLogger(__name__)

#: Content types this extractor handles (from ``FETCH_ALLOWED_CONTENT_TYPES``).
SUPPORTED_MEDIA_TYPES = frozenset({"text/html", "text/plain"})


class TrafilaturaExtractor:
    """Implements ``ContentExtraction`` using trafilatura.

    Key design choices, all observable from the library (not theoretical):
    - ``with_metadata=True`` yields ``title`` / ``description`` / ``text``.
    - ``Document.text`` strips ZWNJ; we use ``body.itertext()`` instead.
    - ``include_comments=False`` (default) drops comment content.
    - ``MAX_TREE_SIZE`` (library default) bounds parsing of pathological HTML.
    - Empty / low-value results are ``is_empty=True``, not exceptions, so
      the orchestrator distinguishes "unusable" from "failed" (FR-43).
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    # --- protocol ------------------------------------------------------------

    async def extract(self, source: FetchedSource) -> ExtractedContent:
        """Extract from bytes; never touches the network."""
        # Content-type dispatch
        media_type = (source.content_type or "").lower().split(";")[0].strip()
        if media_type and media_type not in SUPPORTED_MEDIA_TYPES:
            log.info("extract: unsupported content type=%s url=%s", media_type, source.final_url)
            raise ExtractionUnsupportedContentError(
                f"content type {media_type!r} not supported for extraction"
            )
        # Protocol requires async; the work is CPU-bound (parse + normalize),
        # so we run synchronously inside this async method.
        return self.extract_sync(source)

    def extract_sync(self, source: FetchedSource) -> ExtractedContent:
        # Content-type dispatch (FR-17, SR-12). Unsupported types fail closed.
        media_type = (source.content_type or "").lower().split(";")[0].strip()
        if media_type and media_type not in SUPPORTED_MEDIA_TYPES:
            log.info("extract: unsupported content type=%s url=%s", media_type, source.final_url)
            raise ExtractionUnsupportedContentError(
                f"content type {media_type!r} not supported for extraction"
            )

        body = source.body
        if not body:
            log.info("extract: empty body url=%s", source.final_url)
            return ExtractedContent.from_fetched(source, text="")

        # Decode from declared charset, falling back to UTF-8 (the only
        # encoding the pipeline requires; mis-declared charsets are the
        # extractor's problem, not the fetcher's — FR-21).
        charset = (source.charset or "utf-8").lower().strip() or "utf-8"
        try:
            html_text = body.decode(charset, errors="replace")
        except LookupError:
            # Unknown charset declaration — fail closed rather than guess.
            log.warning("extract: unknown charset=%s url=%s", charset, source.final_url)
            html_text = body.decode("utf-8", errors="replace")

        # Plain-text path: no parsing, direct normalization.
        media_type = (source.content_type or "").lower().split(";")[0].strip()
        if media_type == "text/plain":
            normalized = self._normalize_text(html_text)
            return ExtractedContent.from_fetched(
                source, text=normalized, language=self._guess_language(normalized)
            )

        # HTML path: parse with trafilatura.
        try:
            doc = trafilatura.bare_extraction(
                html_text,
                with_metadata=True,
                include_comments=False,
                include_tables=True,
                deduplicate=True,
            )
        except Exception as exc:
            log.warning("extract: parse failure url=%s error=%s", source.final_url, exc)
            raise ExtractionParseError(f"failed to parse HTML content: {exc}") from exc

        if doc is None or isinstance(doc, dict):
            log.info("extract: no document produced url=%s", source.final_url)
            return ExtractedContent.from_fetched(source, text="")

        # Title and description from metadata (document guaranteed after None/dict check).
        title = (doc.title or None) if hasattr(doc, "title") else None
        description = (doc.description or None) if hasattr(doc, "description") else None

        # Critical: preserve ZWNJ by extracting from XML body, never using doc.text.
        text_raw = self._extract_text_preserve_zwnj(doc) if hasattr(doc, "body") else ""
        normalized = self._normalize_text(text_raw)

        # If normalization yields nothing usable, still return structured result.
        result = ExtractedContent.from_fetched(
            source,
            title=title,
            text=normalized,
            description=description,
            language=self._guess_language(normalized),
        )

        # Empty result is a typed outcome, not an exception — FR-23 / FR-43.
        if result.is_empty:
            log.info(
                "extract: empty result url=%s chars=%d",
                source.final_url,
                result.char_count,
            )

        return result

    # --- internal -----------------------------------------------------------

    @staticmethod
    def _extract_text_preserve_zwnj(doc) -> str:
        """Extract readable text from trafilatura Document, preserving ZWNJ.

        ``doc.text`` strips U+200C (confirmed by observation); ``doc.body``
        is the parsed XML tree where ``itertext()`` retains separators.
        We concatenate all text nodes with single spaces, matching the
        library's own spacing logic but without losing Persian joins.
        """
        if doc.body is None:
            return ""
        # lxml itertext yields all text nodes in document order.
        parts = []
        for text_node in doc.body.itertext():
            if isinstance(text_node, str):
                stripped = text_node.strip()
                if stripped:
                    parts.append(stripped)
        # Join with space to match standard readability spacing.
        return " ".join(parts)

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Deterministic normalization: NFC, whitespace collapse, ZWNJ preserved.

        NFC for composed characters; whitespace collapsed to single spaces;
        ZWNJ (U+200C) never removed — it is not a combining character and
        NFC does not alter it.
        """
        text = unicodedata.normalize("NFC", text)
        # Preserve ZWNJ by replacing only non-ZWNJ whitespace sequences.
        # We use split() which keeps non-whitespace chars intact, then join.
        return " ".join(text.split())

    @staticmethod
    def _guess_language(text: str) -> str | None:
        """Best-effort language hint from content.

        Simple heuristics only; no external language model (no network,
        no LLM dependency). Persian is detected by Arabic-script + Persian
        specific chars (e.g. گ چ ژ پ); English by Latin + whitespace.
        """
        if not text:
            return None
        # Persian indicators: Arabic script plus distinguishing chars.
        persian_chars = set("گژچپ")
        # Count Persian-specific glyphs.
        persian_count = sum(1 for ch in text if ch in persian_chars)
        # If any Persian-specific chars present, treat as Persian.
        if persian_count > 0:
            return "fa"
        # If predominantly Arabic script without Persian chars, still hint fa
        # because Persian uses Arabic script; English gets en.
        has_arabic = any("\u0600" <= ch <= "\u06ff" for ch in text)
        has_latin = any(ch.isascii() and ch.isalpha() for ch in text)
        if has_arabic and not has_latin:
            return "fa"
        if has_latin and not has_arabic:
            # Could be English or mixed; prefer en for Latin-only, leave None for mixed.
            return "en" if not has_arabic else None
        # Mixed — leave as None rather than guess incorrectly.
        return None


# --- factory ---------------------------------------------------------------

SUPPORTED_EXTRACTORS = frozenset({"trafilatura"})


def create_extractor(
    settings: Settings | None = None, extractor: str | None = None
) -> ContentExtraction:
    """Build the configured extractor.

    Only one extractor exists by design (trafilatura). A future parser
    (readability-lxml, html5lib, etc.) would add a branch here, with
    the domain unchanged (T-3, AD-034).
    """
    name = (extractor or (settings.EXTRACT_EXTRACTOR if settings else None) or "trafilatura").strip().lower()
    if name not in SUPPORTED_EXTRACTORS:
        raise ValueError(
            f"Unknown EXTRACT_EXTRACTOR {name!r}. Supported: {', '.join(sorted(SUPPORTED_EXTRACTORS))}."
        )
    return TrafilaturaExtractor(settings)
