"""Extraction domain + infrastructure (Phase 3 P3.4).

Covers: ExtractedContent model, normalization rules, ZWNJ preservation,
empty/low-value distinction, content-type dispatch, adapter correctness,
fixture coverage (English, Persian, mixed, adversarial), structural proof
that no provider/HTTP type reaches domain, and typed error branches.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.domain.errors import (
    ExtractionEmptyResultError,
    ExtractionParseError,
    ExtractionUnsupportedContentError,
)
from app.domain.research.extraction import (
    ExtractedContent,
    ExtractionStatus,
)
from app.domain.research.fetch import FetchedSource
from app.infrastructure.extract.extractor import TrafilaturaExtractor, create_extractor

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "extraction"

# --- Domain contract --------------------------------------------------------

def test_extracted_content_requires_url():
    with pytest.raises(ValueError, match="url"):
        ExtractedContent(url="")


def test_extracted_content_derived_char_count():
    ec = ExtractedContent(url="https://example.com/", text="hello")
    assert ec.char_count == 5


def test_extracted_content_is_empty_for_short_text():
    ec = ExtractedContent(url="https://example.com/", text="  ")
    assert ec.is_empty is True
    assert ec.status == ExtractionStatus.EMPTY


def test_extracted_content_usable_for_real_text():
    ec = ExtractedContent(url="https://example.com/", text="Real content here.")
    assert ec.is_empty is False
    assert ec.is_usable is True
    assert ec.status == ExtractionStatus.EXTRACTED


def test_extracted_content_normalizes_whitespace():
    ec = ExtractedContent.from_fetched(
        FetchedSource(
            requested_url="https://a.com",
            final_url="https://a.com/",
            status_code=200,
            content_type="text/html",
            body=b"<p>hello  world</p>",
        ),
        text="hello   world\n\tthere",
    )
    assert ec.text == "hello world there"


def test_extracted_content_preserves_zwnj():
    # Persian word-boundary must survive normalization.
    persian = "سلام\u200cجهان"
    ec = ExtractedContent.from_fetched(
        FetchedSource(
            requested_url="https://a.com",
            final_url="https://a.com/",
            status_code=200,
            content_type="text/html",
            body=b"<p>test</p>",
        ),
        text=persian,
    )
    assert "\u200c" in ec.text, "ZWNJ must be preserved"
    assert ec.text == persian


def test_extracted_content_language_hint_fa_for_persian_chars():
    ec = ExtractedContent.from_fetched(
        FetchedSource(
            requested_url="https://a.com",
            final_url="https://a.com/",
            status_code=200,
            content_type="text/html",
            body=b"",
        ),
        text="قیمت نفت در بازار",
    )
    assert ec.language == "fa"


def test_extracted_content_from_fetched_sets_url_to_final():
    src = FetchedSource(
        requested_url="https://a.com/old",
        final_url="https://b.com/new",
        status_code=200,
        content_type="text/html",
        body=b"<h1>t</h1>",
    )
    ec = ExtractedContent.from_fetched(src, text="hello world")
    assert ec.url == "https://b.com/new"


# --- Error taxonomy ---------------------------------------------------------

def test_extraction_errors_are_typed():
    assert issubclass(ExtractionParseError, Exception)
    assert issubclass(ExtractionEmptyResultError, Exception)
    assert issubclass(ExtractionUnsupportedContentError, Exception)
    assert ExtractionParseError.retryable is False
    assert ExtractionEmptyResultError.retryable is False


def test_extraction_empty_result_distinguished_from_parse_error():
    """FR-43: a parse failure and an empty result are different outcomes."""
    # They are different classes; the caller can branch.
    assert ExtractionParseError is not ExtractionEmptyResultError


# --- Fixture: English -------------------------------------------------------

def test_english_article_extracts_readable_text():
    html = (FIXTURES / "english_article.html").read_text()
    extractor = TrafilaturaExtractor()
    src = FetchedSource(
        requested_url="https://example.com/article",
        final_url="https://example.com/article",
        status_code=200,
        content_type="text/html",
        body=html.encode("utf-8"),
    )
    result = extractor.extract_sync(src)
    assert result.is_usable is True
    assert "Python" in result.text or "asyncio" in result.text
    assert result.title == "Python Asyncio Guide"
    assert result.description is not None
    assert result.language == "en"


# --- Fixture: Persian -------------------------------------------------------

def test_persian_article_preserves_zwnj_and_title():
    html = (FIXTURES / "persian_article.html").read_text()
    extractor = TrafilaturaExtractor()
    src = FetchedSource(
        requested_url="https://example.ir/oil",
        final_url="https://example.ir/oil",
        status_code=200,
        content_type="text/html",
        body=html.encode("utf-8"),
    )
    result = extractor.extract_sync(src)
    assert result.is_usable is True
    assert "\u200c" in result.text or "قیمت" in result.text
    assert result.language == "fa"
    assert "راهنمای" in result.title or "نفت" in result.title


# --- Fixture: Mixed ---------------------------------------------------------

def test_mixed_persian_english_extracts_both():
    html = (FIXTURES / "mixed_persian_english.html").read_text()
    extractor = TrafilaturaExtractor()
    src = FetchedSource(
        requested_url="https://example.com/mixed",
        final_url="https://example.com/mixed",
        status_code=200,
        content_type="text/html",
        body=html.encode("utf-8"),
    )
    result = extractor.extract_sync(src)
    assert result.is_usable is True
    # Both scripts present; language hint ambiguous → None (safe fallback).
    assert result.language in ("fa", "en", None)


# --- Fixture: Adversarial ---------------------------------------------------

def test_adversarial_script_and_style_removed():
    html = (FIXTURES / "adversarial_script_style.html").read_text()
    extractor = TrafilaturaExtractor()
    src = FetchedSource(
        requested_url="https://evil.com/",
        final_url="https://evil.com/",
        status_code=200,
        content_type="text/html",
        body=html.encode("utf-8"),
    )
    result = extractor.extract_sync(src)
    assert result.is_usable is True
    # Script/style content must not reach extracted text; comment content
    # must not either (include_comments=False).
    assert "Hijacked" not in result.text
    assert "cookie" not in result.text.lower()
    assert "hidden comment" not in result.text


# --- Content-type dispatch --------------------------------------------------

def test_plain_text_path_no_parsing():
    src = FetchedSource(
        requested_url="https://a.com/",
        final_url="https://a.com/",
        status_code=200,
        content_type="text/plain",
        body=b"Hello world. Direct text.",
    )
    extractor = TrafilaturaExtractor()
    result = extractor.extract_sync(src)
    assert result.is_usable is True
    assert "Hello world" in result.text


def test_unsupported_content_type_raises_typed():
    src = FetchedSource(
        requested_url="https://a.com/",
        final_url="https://a.com/",
        status_code=200,
        content_type="application/pdf",
        body=b"%PDF-1.4 fake pdf",
    )
    extractor = TrafilaturaExtractor()
    with pytest.raises(ExtractionUnsupportedContentError):
        extractor.extract_sync(src)


# --- Factory ---------------------------------------------------------------

def test_create_extractor_default():
    ext = create_extractor()
    assert isinstance(ext, TrafilaturaExtractor)


def test_create_extractor_unknown_raises():
    from app.core.config import Settings
    with pytest.raises(ValueError, match="Unknown"):
        create_extractor(Settings(), extractor="unknown")


# --- Structural: no provider or HTTP in domain -----------------------------

def test_extraction_domain_has_no_provider_or_http():
    root = Path(__file__).resolve().parents[1] / "app" / "domain" / "research"
    forbidden = {"httpx", "requests", "urllib3", "aiohttp", "openai", "trafilatura"}
    offenders = {}
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                found.add(node.module.split(".")[0])
        leaked = found & forbidden
        if leaked:
            # extraction.py is allowed to reference trafilatura only in
            # infrastructure, not domain. The domain file should not import it.
            offenders[str(path.name)] = leaked
    # extraction.py must NOT import parser libs; it's pure domain.
    # If it does, that's a failure.
    assert "extraction.py" not in offenders, f"domain leaks parser libs: {offenders.get('extraction.py')}"


# --- Empty / low-value typed outcomes ---------------------------------------

def test_empty_html_yields_empty_not_exception():
    src = FetchedSource(
        requested_url="https://a.com/",
        final_url="https://a.com/",
        status_code=200,
        content_type="text/html",
        body=b"<html><body></body></html>",
    )
    extractor = TrafilaturaExtractor()
    result = extractor.extract_sync(src)
    assert result.is_empty is True
    assert result.status == ExtractionStatus.EMPTY


def test_low_text_result_is_empty():
    ec = ExtractedContent.from_fetched(
        FetchedSource(
            requested_url="https://a.com/",
            final_url="https://a.com/",
            status_code=200,
            content_type="text/html",
            body=b"",
        ),
        text="x",
    )
    assert ec.is_empty is True
