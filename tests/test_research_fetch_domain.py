"""The fetch domain contract (Phase 3, P3.3).

``FetchRequest`` and ``FetchedSource`` are what the Research Engine reasons
about after a retrieval. These tests hold two things:

* the contract's own invariants — a fetched source is a 2xx, a URL is bounded,
  query strings are stripped before anything is stored or logged;
* the boundary properties § 15.3 demands of every provider-independent type:
  the module imports no HTTP library, no DNS object, and no ORM, and it
  introduces no extraction, chunk, claim, or citation concept that belongs to
  P3.4 and later.

The structural tests are the load-bearing ones. A domain type that imports
httpx cannot be reasoned about independently of the transport, and a future
stage that adds ``chunks`` to ``FetchedSource`` has moved a boundary without
deciding to.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.research.fetch import (
    MAX_URL_CHARS,
    FetchedSource,
    FetchRequest,
)
from app.domain.research.ports import SafeFetcher

DOMAIN = Path(__file__).resolve().parents[1] / "app" / "domain"


# --- request ---------------------------------------------------------------


def test_a_url_becomes_a_request():
    assert FetchRequest(url="https://example.com/a").url == "https://example.com/a"


def test_surrounding_whitespace_is_trimmed():
    assert FetchRequest(url="  https://example.com/a \n").url == "https://example.com/a"


def test_a_url_is_not_normalized_here():
    """Parsing belongs to the SSRF boundary, which is the one place allowed to
    decide what a URL means. Normalizing here would create a second, weaker
    interpretation that the fetcher never sees."""
    request = FetchRequest(url="HTTP://Example.COM/a")

    assert request.url == "HTTP://Example.COM/a"


@pytest.mark.parametrize("url", ["", "   "])
def test_an_empty_url_is_rejected(url):
    with pytest.raises(ValidationError):
        FetchRequest(url=url)


def test_a_url_longer_than_the_bound_is_rejected():
    """A URL longer than this is a programming error or an attempt to push an
    unbounded string through the parser, the resolver, and the logs."""
    with pytest.raises(ValidationError):
        FetchRequest(url="https://example.com/" + "a" * MAX_URL_CHARS)


def test_a_request_is_frozen():
    request = FetchRequest(url="https://example.com/a")

    with pytest.raises(ValidationError):
        request.url = "https://elsewhere.test/"  # type: ignore[misc]


# --- fetched source --------------------------------------------------------


def build(**overrides: object) -> FetchedSource:
    defaults: dict[str, object] = {
        "requested_url": "https://example.com/a",
        "final_url": "https://example.com/a",
        "status_code": 200,
        "content_type": "text/html",
        "charset": "utf-8",
        "body": b"<html><body>ok</body></html>",
    }
    return FetchedSource(**{**defaults, **overrides})


def test_a_fetched_source_carries_the_metadata_extraction_will_need():
    """What P3.4 selects a parser and a decoder from, and what a run needs to
    explain a source afterwards."""
    source = build()

    assert source.final_url == "https://example.com/a"
    assert source.status_code == 200
    assert source.content_type == "text/html"
    assert source.charset == "utf-8"
    assert source.byte_count == len(source.body)


def test_a_non_2xx_status_cannot_be_a_fetched_source():
    """The status is validated by the type, so no code path can construct a
    "retrieved source" from an error page.

    A 404 body is not source content, and a constructor that would accept one
    would put that decision on every caller instead of in one place.
    """
    for status in (199, 300, 404, 500):
        with pytest.raises(ValidationError):
            build(status_code=status)


def test_a_query_string_is_stripped_before_the_url_is_stored():
    """The query is where signed-URL tokens and session identifiers live. A URL
    recorded on a research run or written to a log must not carry one."""
    source = build(
        requested_url="https://example.com/a?token=SECRET123",
        final_url="https://example.com/a?token=SECRET123",
    )

    assert source.requested_url == "https://example.com/a"
    assert source.final_url == "https://example.com/a"
    assert "SECRET123" not in source.requested_url
    assert "SECRET123" not in source.final_url


def test_a_fragment_is_stripped_too():
    assert build(final_url="https://example.com/a#section").final_url == (
        "https://example.com/a"
    )


def test_the_path_is_kept():
    """The path distinguishes one page of a site from another and carries no
    credential in any scheme this fetcher accepts."""
    assert build(final_url="https://example.com/news/2026/story").final_url == (
        "https://example.com/news/2026/story"
    )


def test_a_port_in_the_url_survives():
    assert build(final_url="https://example.com:8443/a").final_url == (
        "https://example.com:8443/a"
    )


def test_a_credential_in_a_url_is_stripped_rather_than_stored():
    """Defence in depth. The fetcher already refuses such URLs outright, so this
    cannot normally happen — but a recorded URL must never be the thing that
    persists a password."""
    source = build(final_url="https://admin:hunter2@example.com/a")

    assert "hunter2" not in source.final_url
    assert "admin" not in source.final_url


def test_the_redirect_chain_records_every_validated_hop():
    """What makes "the fetcher checked every hop" auditable after the fact."""
    source = build(
        redirect_chain=(
            "https://a.test/one",
            "https://b.test/two?ref=x",
            "https://c.test/three",
        )
    )

    assert source.redirect_chain == (
        "https://a.test/one",
        "https://b.test/two",
        "https://c.test/three",
    )
    assert source.was_redirected is True


def test_a_single_hop_is_not_a_redirect():
    source = build(redirect_chain=("https://example.com/a",))

    assert source.was_redirected is False


def test_a_credential_in_a_redirect_chain_is_stripped():
    source = build(
        redirect_chain=(
            "https://a.test/one",
            "https://user:secret@b.test/two",
        )
    )

    assert "secret" not in " ".join(source.redirect_chain)


def test_an_empty_redirect_chain_is_allowed():
    """Recorded hops are metadata, not identity; an empty chain is a fetcher that
    recorded nothing, which must not be a construction error."""
    assert build(redirect_chain=()).redirect_chain == ()


def test_the_body_is_bytes_and_is_not_decoded_here():
    """Charset decoding belongs to P3.4. Decoding here would mean a mis-declared
    charset silently corrupts the bytes before anything has inspected them."""
    source = build(body="متن فارسی".encode())

    assert isinstance(source.body, bytes)
    assert source.body.decode("utf-8") == "متن فارسی"


def test_an_empty_body_is_valid():
    """A 204 has no body. Whether that makes a *usable* source is P3.4's
    judgement; whether it is a valid fetch is not in question."""
    assert build(status_code=204, body=b"").byte_count == 0


def test_a_fetched_source_is_frozen():
    source = build()

    with pytest.raises(ValidationError):
        source.status_code = 500  # type: ignore[misc]


def test_the_timestamp_defaults_to_now_and_is_aware():
    source = build()

    assert source.fetched_at.tzinfo is not None
    assert source.fetched_at <= datetime.now(UTC)


def test_a_naive_timestamp_is_read_as_utc():
    """Reproducibility (§ 6) needs comparable timestamps; a naive value would
    sort wrongly against an aware one rather than being rejected."""
    naive = datetime(2026, 10, 7, 12, 0, 0)

    assert build(fetched_at=naive).fetched_at.tzinfo is UTC


def test_elapsed_time_cannot_be_negative():
    with pytest.raises(ValidationError):
        build(elapsed_ms=-1)


# --- structural boundaries -------------------------------------------------


def domain_imports() -> set[str]:
    """Every top-level module imported anywhere under ``app/domain``."""
    import sys

    stdlib = set(sys.stdlib_module_names)
    found: set[str] = set()
    for path in DOMAIN.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in stdlib and top != "app":
                        found.add(top)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                top = node.module.split(".")[0]
                if top not in stdlib and top != "app":
                    found.add(top)
    return found


def test_the_domain_imports_no_transport_or_provider_library():
    """The property that makes swapping the fetcher possible (FR-12, § 15.3).

    A domain type that imports httpx, or reaches for ``ipaddress`` or ``socket``,
    has taken a decision about the transport that it should not be making.
    """
    imported = domain_imports()

    forbidden = {"httpx", "httpcore", "socket", "idna", "brotli", "zstandard"}
    assert not (imported & forbidden), f"domain imports transport modules: {imported}"


def test_the_domain_imports_no_orm():
    imported = domain_imports()

    assert "sqlalchemy" not in imported
    assert "asyncpg" not in imported


def test_the_domain_imports_no_provider_or_library_framework():
    """Consistent with the search domain's own test: no provider SDK, no FastAPI,
    no aiogram, no Celery."""
    imported = domain_imports()

    for forbidden in ("openai", "aiogram", "celery", "redis", "fastapi", "httpx"):
        assert forbidden not in imported, f"domain imports {forbidden}"


def test_the_fetch_module_imports_nothing_from_infrastructure():
    """The dependency arrow points one way: infrastructure knows the domain."""
    source = (DOMAIN / "research" / "fetch.py").read_text()

    assert "app.infrastructure" not in source
    assert "app.core" not in source
    assert "app.db" not in source


def test_the_fetch_module_introduces_no_later_stage_concept():
    """§ 15.8: extraction, chunking, claims, and citations are P3.4 and later.

    Asserted over the identifiers the module actually defines, so a future
    ``FetchedSource.chunks`` would fail here rather than quietly pulling
    extraction into the transport contract.
    """
    source = (DOMAIN / "research" / "fetch.py").read_text()
    tree = ast.parse(source)

    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            defined.add(node.name)
            defined.update(
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
        elif isinstance(node, ast.FunctionDef):
            defined.add(node.name)

    for later_stage in (
        "SourceChunk",
        "Chunk",
        "Claim",
        "Citation",
        "extract",
        "readability",
        "clean",
        "normalize_text",
        "rank",
        "score",
    ):
        assert later_stage not in defined, (
            f"{later_stage} belongs to P3.4 or later, not the fetch contract"
        )


def test_the_fetched_source_has_no_extraction_fields():
    """The fields that would mean extraction, chunking, or ranking leaked into
    the transport contract. Named explicitly because the class docstring is where
    a reviewer would look to confirm they are absent."""
    fields = set(FetchedSource.model_fields)

    for forbidden in (
        "text",
        "chunks",
        "claims",
        "citations",
        "rank",
        "score",
        "quality",
        "summary",
        "relevance",
    ):
        assert forbidden not in fields, f"FetchedSource.{forbidden} belongs to a later stage"

    assert set(fields) == {
        "requested_url",
        "final_url",
        "status_code",
        "content_type",
        "content_encoding",
        "charset",
        "body",
        "redirect_chain",
        "fetched_at",
        "elapsed_ms",
    }


def test_the_port_is_runtime_checkable_and_speaks_only_domain_types():
    """A fetcher implementation must be substitutable, and the port must not
    mention a transport type (§ 12)."""
    assert getattr(SafeFetcher, "_is_runtime_protocol", False)

    methods = {
        name for name in vars(SafeFetcher) if not name.startswith("_")
    }
    assert methods == {"fetch"}, f"the fetch port grew a method: {methods}"

    import inspect

    signature = inspect.signature(SafeFetcher.fetch)
    assert list(signature.parameters) == ["self", "request"]
    assert signature.return_annotation in (FetchedSource, "FetchedSource")


def test_a_plain_object_with_a_fetch_method_satisfies_the_port():
    """The substitution guarantee, as a test rather than a promise.

    A fetcher that satisfies this protocol with no infrastructure at all must be
    accepted, which is what lets P3.9 orchestrate against a fake (T-1).
    """

    class StubFetcher:
        async def fetch(self, request: FetchRequest) -> FetchedSource:
            return build()

    assert isinstance(StubFetcher(), SafeFetcher)


def test_an_object_without_a_fetch_method_does_not_satisfy_the_port():
    class NotAFetcher:
        pass

    assert not isinstance(NotAFetcher(), SafeFetcher)


def test_the_fetcher_port_takes_an_explicit_request_type():
    """The untrusted-URL boundary is explicit in the type, not in a comment.

    A method taking a bare ``str`` would let any string — including one built from
    model output — reach the fetcher without passing through the type that says it
    is untrusted.
    """
    import inspect

    annotation = inspect.signature(SafeFetcher.fetch).parameters["request"].annotation
    assert annotation in (FetchRequest, "FetchRequest")