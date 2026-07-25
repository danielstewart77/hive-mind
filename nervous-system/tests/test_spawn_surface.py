"""The spawn payload names the surface the process will serve.

Minds shape behavior per surface (TTS prose for Telegram, full terminal
rendering for a tty), but ``owner_type`` is a routing key — it carries a
mind-uuid suffix and a web/terminal split that are the gateway's business.
``_spawn`` therefore ships the clean human label under ``surface``, derived
by the same ``_surface_label`` the session picker uses, so every consumer
of the word "terminal" agrees on what it means.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import patch

import pytest

from comms.models import ModelRegistry, Provider
from comms.sessions import SessionManager


def _registry() -> ModelRegistry:
    return ModelRegistry({"anthropic": Provider(name="anthropic")}, {"opus": "anthropic"})


async def _make_manager(tmp: str) -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager(_registry())
    await mgr.start()
    return mgr


def _capturing_session_class(payloads: list[dict]):
    class _Resp:
        status = 200

        async def text(self) -> str:
            return ""

    class _FakeClientSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, timeout=None):
            payloads.append(json)
            return _Resp()

    return _FakeClientSession


async def _fake_mind(db, mind_id):
    return {"name": "ada", "model": "opus", "gateway_url": "http://mind.test:8420"}


def _spawned_payload(owner_type: str | None, harness_sid: str | None = None) -> dict:
    async def scenario() -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _make_manager(tmp)
            mgr.broker_db = object()  # get_mind_by_id is patched; never touched
            payloads: list[dict] = []
            try:
                with patch("aiohttp.ClientSession", _capturing_session_class(payloads)), \
                     patch("comms.broker.get_mind_by_id", _fake_mind):
                    await mgr._spawn(
                        "sess-1", "opus", resume_sid="conv-1", mind_id="ada",
                        owner_type=owner_type, owner_ref="123", harness_sid=harness_sid,
                    )
            finally:
                await mgr.shutdown()
            assert len(payloads) == 1
            return payloads[0]

    return asyncio.run(scenario())


def test_telegram_owner_type_ships_as_telegram() -> None:
    payload = _spawned_payload("telegram:14cb820b-4a42")
    assert payload["surface"] == "telegram"


def test_web_owner_type_ships_as_terminal() -> None:
    """The browser tile registers as ``web`` but everyone calls it the
    terminal — same collapse the session picker's surface label does."""
    payload = _spawned_payload("web:14cb820b-4a42")
    assert payload["surface"] == "terminal"


def test_missing_owner_type_ships_an_empty_surface() -> None:
    """No invented default: a mind that sees "" knows the gateway didn't
    know, which is different from being told "local" and believing it."""
    payload = _spawned_payload(None)
    assert payload["surface"] == ""


def test_surface_rides_beside_owner_type_not_instead_of_it() -> None:
    """owner_type keeps flowing for env routing (rotation hook reads it)."""
    payload = _spawned_payload("discord:abc")
    assert payload["owner_type"] == "discord:abc"
    assert payload["surface"] == "discord"


def test_provider_thread_id_is_shipped_to_the_mind() -> None:
    payload = _spawned_payload("web", harness_sid="codex-thread-7")
    assert payload["harness_sid"] == "codex-thread-7"
