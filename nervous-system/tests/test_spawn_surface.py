"""The spawn payload names the surface the process will serve.

Minds shape behavior per surface (TTS prose for Telegram, full terminal
rendering for a tty), but ``surface`` is a routing key — it carries a
mind-uuid suffix and a web/terminal split that are the gateway's business.
``_spawn`` therefore ships the clean human label under ``surface_label``, derived
by the same ``_surface_label`` the session picker uses, so every consumer
of the word "terminal" agrees on what it means.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from comms_pg_fixture import (  # noqa: F401 - fixtures
    close_manager,
    comms_db,
    make_manager,
    register_mind,
    scratch_dsn,
)

pytestmark = pytest.mark.usefixtures("comms_db")


def _capturing_session_class(payloads: list[dict]):
    class _Resp:
        status = 200

        async def text(self) -> str:
            return ""

        async def json(self) -> dict:
            return {}

    class _RequestCtx:
        """What ``aiohttp.ClientSession.post`` actually returns.

        Not a coroutine: a ``_RequestContextManager``, which is both
        awaitable and an async context manager. The double stubbed only the
        awaitable half, so a caller holding the response open — the thing
        that releases the connection instead of leaking it — failed against
        a fake that the real client would have satisfied.
        """
        def __await__(self):
            async def _resp():
                return _Resp()
            return _resp().__await__()

        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *exc):
            return False

    class _FakeClientSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, json=None, timeout=None, headers=None):
            payloads.append(json)
            return _RequestCtx()

    return _FakeClientSession


def _spawned_payload(surface: str | None, harness_sid: str | None = None) -> dict:
    async def scenario() -> dict:
        mgr = await make_manager()
        # A registered mind: the spawn reads its address and its session
        # credential off the registration.
        await register_mind(
            "ada", gateway_url="http://mind.test:8420", model="opus",
            harness="claude_cli", session_token="ada-token",  # secret-guard: allow
        )
        payloads: list[dict] = []
        try:
            with patch("aiohttp.ClientSession", _capturing_session_class(payloads)):
                await mgr._spawn(
                    "sess-1", "opus", resume_sid="conv-1", mind_id="ada",
                    surface=surface, surface_id="123", harness_sid=harness_sid,
                )
        finally:
            await close_manager(mgr)
        assert len(payloads) == 1
        return payloads[0]

    return asyncio.run(scenario())


def test_a_telegram_surface_ships_as_telegram() -> None:
    payload = _spawned_payload("telegram:14cb820b-4a42")
    assert payload["surface_label"] == "telegram"


def test_a_web_surface_ships_as_terminal() -> None:
    """The browser tile registers as ``web`` but everyone calls it the
    terminal — same collapse the session picker's surface label does."""
    payload = _spawned_payload("web:14cb820b-4a42")
    assert payload["surface_label"] == "terminal"


def test_a_missing_surface_ships_an_empty_label() -> None:
    """No invented default: a mind that sees "" knows the gateway didn't
    know, which is different from being told "local" and believing it."""
    payload = _spawned_payload(None)
    assert payload["surface_label"] == ""


def test_the_label_rides_beside_the_raw_address_not_instead_of_it() -> None:
    """The raw address keeps flowing for env routing (rotation hook reads it)."""
    payload = _spawned_payload("discord:abc")
    assert payload["surface"] == "discord:abc"
    assert payload["surface_id"] == "123"
    assert payload["surface_label"] == "discord"


def test_provider_thread_id_is_shipped_to_the_mind() -> None:
    payload = _spawned_payload("web", harness_sid="codex-thread-7")
    assert payload["harness_sid"] == "codex-thread-7"
