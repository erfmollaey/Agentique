"""T-3 and T-4 — Telegram output safety: escaping and length chunking.

Audit C-4: the bot declared ``ParseMode.HTML`` but sent ``**markdown**`` with
no escaping and no length check. Telegram then rejected or mis-rendered the
message, and because nothing caught the failure the reply was lost silently.
"""

from __future__ import annotations

import pytest

from app.services.delivery import DeliveryService
from app.services.formatting import (
    TELEGRAM_HARD_LIMIT,
    escape,
    render_analysis,
    split_for_telegram,
)

# --- T-4: escaping ---------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("<script>alert(1)</script>", "<script>"),
        ("a & b", "& "),
        ("5 < 10", "< 10"),
        ("<b>not bold</b>", "<b>not"),
    ],
)
def test_t4_markup_characters_are_escaped(raw, must_not_contain):
    escaped = escape(raw)
    assert must_not_contain not in escaped
    assert "&lt;" in escaped or "&amp;" in escaped


def test_t4_render_analysis_escapes_model_output():
    out = render_analysis(
        summary="<img src=x onerror=alert(1)>",
        sub_questions=["<b>injected</b>", "normal"],
    )
    assert "<img" not in out
    assert "<b>injected</b>" not in out
    assert "&lt;img" in out
    # The only tags present are the ones we emit ourselves.
    assert out.count("<b>") == 2  # result label + questions label


def test_t4_empty_analysis_renders_a_safe_placeholder():
    out = render_analysis(summary="", sub_questions=[])
    assert out
    assert "<" not in out


# --- T-3: length limits ----------------------------------------------------

def test_t3_short_text_is_a_single_chunk():
    assert split_for_telegram("hello") == ["hello"]


def test_t3_text_at_the_limit_is_not_split():
    text = "a" * 100
    assert split_for_telegram(text, 100) == [text]


def test_t3_text_over_the_limit_is_split():
    text = "a" * 250
    chunks = split_for_telegram(text, 100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text, "content was lost while chunking"


def test_t3_paragraphs_are_preserved_where_possible():
    text = "\n".join(["line " * 10 for _ in range(10)])
    chunks = split_for_telegram(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_t3_single_overlong_line_is_hard_split():
    text = "b" * 350
    chunks = split_for_telegram(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_t3_chunks_never_exceed_telegram_hard_limit():
    text = "c" * (TELEGRAM_HARD_LIMIT * 3)
    chunks = split_for_telegram(text, TELEGRAM_HARD_LIMIT - 16)
    assert all(len(c) <= TELEGRAM_HARD_LIMIT for c in chunks)
    assert "".join(chunks) == text


def test_t3_invalid_limit_is_rejected():
    with pytest.raises(ValueError):
        split_for_telegram("x", 0)


# --- T-3 at the delivery boundary ------------------------------------------

def test_t3_long_reply_is_delivered_as_multiple_messages(fake_bot, settings):
    """A long AI reply must not fail the task; it is split and all sent."""
    delivery = DeliveryService(fake_bot, settings)
    huge = "x" * 9000

    sent = delivery.send(chat_id=7, text=huge)

    assert sent > 1
    assert len(fake_bot.sent) == sent
    for _, text in fake_bot.sent:
        assert len(text) <= 4096, "a chunk exceeded Telegram's limit"
    assert "".join(t for _, t in fake_bot.sent) == huge, "content was truncated"


def test_t3_short_reply_is_delivered_as_one_message(fake_bot, settings):
    delivery = DeliveryService(fake_bot, settings)
    assert delivery.send(chat_id=7, text="short") == 1
    assert len(fake_bot.sent) == 1


def test_t3_empty_text_still_produces_one_message(fake_bot, settings):
    """Exactly one terminal response, even for empty content (FR-3.4)."""
    delivery = DeliveryService(fake_bot, settings)
    assert delivery.send(chat_id=7, text="") == 1
