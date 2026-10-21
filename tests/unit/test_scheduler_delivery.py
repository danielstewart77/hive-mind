"""A reminder reaches one person, and the audio reaches the same one.

This process holds one bot token and addresses whichever chat claimed the
raising mind's own bot. Telegram refuses both `sendMessage` and `sendVoice`
from a bot the target has never started, so every send here is driven
against a stand-in bot API that can refuse — the status read back is the
whole behaviour, and it is the one thing the real endpoint cannot be made to
show a test.
"""
from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestServer

from bots import scheduler


async def _bot_api(state: dict) -> TestServer:
    async def send_message(request: web.Request) -> web.Response:
        body = await request.json()
        state.setdefault("texts", []).append(body["chat_id"])
        if body["chat_id"] in state.get("refuses", ()):
            return web.json_response({"ok": False}, status=403)
        return web.json_response({"ok": True})

    async def send_voice(request: web.Request) -> web.Response:
        state.setdefault("voices", []).append(request.query["chat_id"])
        if request.query["chat_id"] in state.get("refuses", ()):
            return web.json_response({"ok": False}, status=403)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/bot{token}/sendMessage", send_message)
    app.router.add_post("/bot{token}/sendVoice", send_voice)
    server = TestServer(app)
    await server.start_server()
    return server


class TestWhetherTelegramTookIt:
    async def test_a_refused_voice_note_answers_false(self, monkeypatch) -> None:
        state: dict = {"refuses": ("5550001",)}
        server = await _bot_api(state)
        try:
            monkeypatch.setattr(
                scheduler, "TELEGRAM_API_BASE", str(server.make_url("")).rstrip("/")
            )

            took_it = await scheduler._send_voice("token", 5550001, b"ogg")
        finally:
            await server.close()

        assert took_it is False
        assert state["voices"] == ["5550001"]

    async def test_a_delivered_voice_note_answers_true(self, monkeypatch) -> None:
        state: dict = {}
        server = await _bot_api(state)
        try:
            monkeypatch.setattr(
                scheduler, "TELEGRAM_API_BASE", str(server.make_url("")).rstrip("/")
            )

            took_it = await scheduler._send_voice("token", 5550001, b"ogg")
        finally:
            await server.close()

        assert took_it is True


class TestWhereTheWordsAndTheAudioLand:
    async def test_the_chat_the_words_reached_is_reported_back(
        self, monkeypatch
    ) -> None:
        """So whatever follows them lands in the same place rather than
        resolving the claim a second time across a cache."""
        state: dict = {}
        server = await _bot_api(state)
        try:
            monkeypatch.setattr(
                scheduler, "TELEGRAM_API_BASE", str(server.make_url("")).rstrip("/")
            )
            monkeypatch.setattr(
                scheduler, "owner_chat_for", _answering(5550001)
            )

            landed = await scheduler._deliver_text("mind-uuid", "token", "hello")
        finally:
            await server.close()

        assert landed == 5550001
        assert state["texts"] == ["5550001"]

    async def test_a_refused_delivery_reports_the_chat_it_fell_back_to(
        self, monkeypatch
    ) -> None:
        """Their mind's bot is a different bot, so a refusal here must not end
        as a reminder that simply never arrives — and the audio has to follow
        the words to the chat that actually took them."""
        state: dict = {"refuses": ("5550001",)}
        server = await _bot_api(state)
        try:
            monkeypatch.setattr(
                scheduler, "TELEGRAM_API_BASE", str(server.make_url("")).rstrip("/")
            )
            monkeypatch.setattr(scheduler, "owner_chat_for", _answering(5550001))
            monkeypatch.setattr(
                scheduler.config, "telegram_owner_chat_id", 9999999
            )

            landed = await scheduler._deliver_text("mind-uuid", "token", "hello")
        finally:
            await server.close()

        assert landed == 9999999
        assert state["texts"] == ["5550001", "9999999"]

    async def test_a_delivery_that_reaches_nobody_reports_nobody(
        self, monkeypatch
    ) -> None:
        """Zero, so the voice note is not sent after words that never
        arrived."""
        state: dict = {"refuses": ("5550001", "9999999")}
        server = await _bot_api(state)
        try:
            monkeypatch.setattr(
                scheduler, "TELEGRAM_API_BASE", str(server.make_url("")).rstrip("/")
            )
            monkeypatch.setattr(scheduler, "owner_chat_for", _answering(5550001))
            monkeypatch.setattr(
                scheduler.config, "telegram_owner_chat_id", 9999999
            )

            landed = await scheduler._deliver_text("mind-uuid", "token", "hello")
        finally:
            await server.close()

        assert landed == 0


def _answering(chat_id: int):
    async def _owner_chat_for(mind_id: str, surface: str = "telegram") -> int:
        return chat_id

    return _owner_chat_for


class TestOneResolutionPerFire:
    async def test_a_named_chat_is_used_rather_than_resolved_again(
        self, monkeypatch
    ) -> None:
        """The claim is read across a cache that no longer holds an empty
        answer, so two lookups in one fire genuinely can disagree — which put
        one person's reminder in another person's chat."""
        state: dict = {}
        server = await _bot_api(state)
        resolutions: list[str] = []

        async def _owner_chat_for(mind_id: str, surface: str = "telegram") -> int:
            resolutions.append(mind_id)
            return 7777777

        try:
            monkeypatch.setattr(
                scheduler, "TELEGRAM_API_BASE", str(server.make_url("")).rstrip("/")
            )
            monkeypatch.setattr(scheduler, "owner_chat_for", _owner_chat_for)

            landed = await scheduler._deliver_text(
                "mind-uuid", "token", "hello", chat_id=5550001
            )
        finally:
            await server.close()

        assert landed == 5550001
        assert state["texts"] == ["5550001"]
        assert resolutions == []
