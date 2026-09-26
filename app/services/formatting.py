"""Message formatting.

Pure functions: model output in, one or more safe Telegram messages out. No
I/O, no network, no client objects, so it is testable in isolation (TR-5).

Audit references: C-4 (HTML parse mode receiving unescaped Markdown, replies
silently dropped), P0-8 (parse-mode mismatch), P0-9 (length limits), T-3, T-4.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

# Telegram rejects messages longer than this (P0-9).
TELEGRAM_HARD_LIMIT = 4096
# Headroom so a chunk plus any suffix always fits the hard limit.
_SAFE_CHUNK = TELEGRAM_HARD_LIMIT - 16


def escape(text: str) -> str:
    """Escape a string for Telegram's HTML parse mode.

    The bot's default parse mode is HTML, so any ``<``, ``>`` or ``&`` arriving
    from the model or from a user must be escaped or Telegram rejects the whole
    message with ``400 can't parse entities`` (C-4).
    """
    return html.escape(text or "", quote=False)


def _chunk(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most ``limit`` characters.

    Splits on newlines first so paragraphs stay intact, then hard-splits any
    single over-long line. Content is never dropped (P0-9: a long answer must
    not fail the whole task, and must not be silently truncated).
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text] if text else [""]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def split_for_telegram(text: str, limit: int = _SAFE_CHUNK) -> list[str]:
    """Split already-escaped text into sendable chunks.

    Public entry point for the length requirement.
    """
    return _chunk(text, limit)


def render_analysis(
    summary: str,
    sub_questions: Iterable[str],
    *,
    result_label: str = "Analysis result",
    questions_label: str = "Sub-questions",
    empty_message: str = "No response received.",
) -> str:
    """Render a *query analysis* as one safe HTML message.

    Every dynamic value is escaped, so markup characters in model output can
    never break message parsing (FR-3.6, T-4).

    **Not called by the Phase 2 request path.** The chat flow returns the model's
    own text and does not reshape it. This renderer belongs to the
    query-decomposition path, which Phase 3 wires up; it is retained, with its
    Phase 1 test, for the same reason
    :meth:`app.agents.supervisor.Supervisor.generate_final_answer` is. It is
    markup-safety-tested, so it cannot silently rot.
    """
    safe_summary = escape(summary)
    questions = [escape(q) for q in sub_questions if q and str(q).strip()]

    if not safe_summary and not questions:
        return escape(empty_message)

    parts = [f"<b>{escape(result_label)}</b>"]
    if safe_summary:
        parts.append(safe_summary)
    if questions:
        parts.append(f"<b>{escape(questions_label)}</b>")
        parts.extend(f"• {q}" for q in questions)
    return "\n\n".join(parts)
