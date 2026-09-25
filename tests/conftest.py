"""Shared fixtures.

Tests must never touch the network. Both external boundaries — the AI provider
and Telegram — are substitutable precisely because of the Phase 1 dependency
injection work (P1-6, FR-5.4).
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.domain.schemas import QueryAnalysis


@pytest.fixture
def settings() -> Settings:
    """Settings built from test values, never from the real ``.env``."""
    return Settings(
        BOT_TOKEN="1234567890:TEST_TOKEN_NOT_A_REAL_CREDENTIAL",
        GROQ_API_KEY="gsk-test-key-not-a-real-credential",
        REDIS_URL="redis://localhost:6379/15",
        LLM_BASE_URL="https://provider.invalid/v1",
        LLM_MODEL="test-model",
        LLM_TIMEOUT_SECONDS=1.0,
        LLM_MAX_TOKENS=256,
        RATE_LIMIT_MAX_REQUESTS=3,
        RATE_LIMIT_WINDOW_SECONDS=60,
        MAX_IN_FLIGHT_REQUESTS=2,
        LOG_LEVEL="WARNING",
    )


class FakeLLMClient:
    """Stands in for the AI provider. Records calls; returns canned results."""

    def __init__(self, result: QueryAnalysis | None = None, error: Exception | None = None) -> None:
        self.result = result or QueryAnalysis(summary="s", sub_questions=["a", "b"])
        self.error = error
        self.calls: list[str] = []

    def analyze_query(self, user_query: str) -> QueryAnalysis:
        self.calls.append(user_query)
        if self.error is not None:
            raise self.error
        return self.result

    def generate_final_answer(self, user_query: str, sub_answers: list[str]) -> str:
        return "final"


class LoopBoundSession:
    """Mimics ``aiogram``'s ``AiohttpSession`` caching an aiohttp session."""

    def __init__(self) -> None:
        self.bound_to: asyncio.AbstractEventLoop | None = None
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class LoopBoundBot:
    """A bot double that reproduces aiogram's loop-affine session caching.

    This is the shape of the real defect: the session is created on first use
    and bound to the loop that was running then. Reusing it from a different,
    closed loop is what failed (audit C-3). Modelling it faithfully is what
    makes the regression test meaningful rather than cosmetic.
    """

    def __init__(self) -> None:
        self.session = LoopBoundSession()
        self.sent: list[tuple[int, str]] = []
        self.loops: list[asyncio.AbstractEventLoop] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs) -> str:
        running = asyncio.get_running_loop()
        if self.session.bound_to is None:
            # Mirrors AiohttpSession.create_session: bind once, on first use.
            self.session.bound_to = running
        elif self.session.bound_to is not running:
            raise RuntimeError(
                "session is bound to a different event loop than the current one"
            )
        await asyncio.sleep(0)  # yield, exercising real loop machinery
        self.loops.append(running)
        self.sent.append((chat_id, text))
        return "ok"


@pytest.fixture
def fake_bot() -> LoopBoundBot:
    return LoopBoundBot()


@pytest.fixture(autouse=True)
def block_outbound_network(monkeypatch):
    """Fail any attempt to open a non-loopback socket during a test.

    Phase 1 rule: no test may perform a real network call to an LLM or Telegram
    endpoint. ``Message.answer`` reaches the network through
    ``AiohttpSession.__call__`` rather than ``Bot.send_message``, so patching a
    client method is not sufficient. This guard makes an accidental outbound
    connection a loud test failure instead of a silent dependency on the
    internet.
    """
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guard(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError(
                f"test attempted an outbound network connection to {host!r}; "
                "Phase 1 tests must not touch the network"
            )
        return real_connect(self, address, *args, **kwargs)

    def _guard_ex(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError(
                f"test attempted an outbound network connection to {host!r}"
            )
        return real_connect_ex(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guard)
    monkeypatch.setattr(socket.socket, "connect_ex", _guard_ex)


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    return FakeLLMClient()


@pytest.fixture(autouse=True)
def forbid_real_container():
    """Fail loudly if a test ever builds the real production container.

    Guards against a specific failure mode observed during Phase 1
    implementation: a test that appears to inject doubles but actually reaches
    the real container, and therefore the real ``.env`` credentials, and passes
    for the wrong reason. Tests construct an ``AppContainer`` explicitly; any
    call to ``build_container`` is a bug in the test.
    """
    import app.bot.container as container_module

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "a test attempted to build the real application container; "
            "inject an AppContainer or patch get_container instead"
        )

    original = container_module.build_container
    container_module.build_container = refuse
    try:
        yield
    finally:
        container_module.build_container = original


@pytest.fixture(autouse=True)
def reset_loop_state():
    """Keep the process loop from leaking between tests."""
    from app.infrastructure import asyncio_runtime

    asyncio_runtime.reset_event_loop()
    yield
    asyncio_runtime.reset_event_loop()
