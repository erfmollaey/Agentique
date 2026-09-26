"""System instructions for the AI chat MVP.

One place, versioned. Phase 2 § 12 requires a centralized prompt strategy, and
Phase 2 § 6 requires prompt templates to be versioned or checksummed so a change
is detectable. Both are satisfied here: the text is a constant, it carries a
version, and :func:`prompt_fingerprint` gives a stable digest of the exact bytes
that will be sent.

Two properties are enforced by construction rather than by review:

**The system prompt is never built from user content (FR-11, SR-3, T-7).**
:func:`build_system_prompt` takes no user text. There is no parameter through
which conversation content could reach it, so no future edit can accidentally
concatenate a message into the instructions.

**The model is told the truth about its own capabilities (§ 12).** It has no
web access in Phase 2 and must not imply otherwise. Stating this prevents the
assistant from claiming to have researched something, which is exactly the
failure Phase 3 later removes.
"""

from __future__ import annotations

import hashlib

#: Bump when the instruction text changes. Recorded on the assistant message so
#: stored history remains interpretable after a prompt change.
CHAT_SYSTEM_PROMPT_VERSION = "chat-v1"

CHAT_SYSTEM_PROMPT = """\
You are a helpful AI assistant talking with a person over Telegram.

Rules:
- Reply in the language the person writes in.
- Be concise and direct. Telegram messages are read on a phone.
- Use plain text. Telegram renders a small set of markup tags, and anything that \
looks like a tag will be shown literally or break the message.
- If you do not know something, say so plainly. Do not invent facts, \
references, links, or quotations.
- You have no access to the internet, no web search, and no ability to open \
links or fetch pages. Never claim to have searched, read, or verified anything \
online. If asked for current information you cannot verify, say that you cannot \
check it.
- You have no memory outside this conversation. If the person refers to \
something from a previous conversation that is not shown to you, say you do not \
have it.
"""


def build_system_prompt() -> str:
    """Return the system instructions.

    Deliberately parameterless. It takes no user content, so there is no code
    path by which a message, a username, or a stored turn can reach it (FR-11).
    """
    return CHAT_SYSTEM_PROMPT


def prompt_fingerprint() -> str:
    """A stable digest of the system prompt.

    Lets a test assert that a prompt change is deliberate, and lets an operator
    confirm which prompt version a running process is using.
    """
    return hashlib.sha256(CHAT_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16]


# Appended to the system turn when older turns were dropped to fit the context
# budget. Phase 2 § 12: truncation must be surfaced to the model, not silently
# applied. It is a static string with no user content in it (FR-11).
HISTORY_TRUNCATED_NOTICE = (
    "Note: earlier messages in this conversation were left out to fit the "
    "context limit. If the answer depends on them, say what is missing."
)
