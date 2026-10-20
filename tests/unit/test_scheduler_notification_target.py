"""A mind's own unsolicited turn goes to its own person.

Every reminder, gate failure and scheduled briefing went to one stack-wide
operator chat, so a second user's mind would have delivered their reminders
to the operator — a disclosure to him and a reminder the person never gets.
With one bot per person, the right address is the claim on that mind's bot.

These drive `scheduler.owner_chat_for` and `claimed_chat_id` against a
stand-in gateway, because the behaviour under test is the lookup, its
fallback and its cache.
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bots import scheduler


@pytest.fixture(autouse=True)
def _empty_cache():
    scheduler._claimed_chats.clear()
    yield
    scheduler._claimed_chats.clear()


async def _gateway(state: dict) -> TestServer:
    async def claim(request: web.Request) -> web.Response:
        state["reads"] = state.get("reads", 0) + 1
        state["surfaces"] = state.get("surfaces", []) + [
            request.query.get("surface", "")
        ]
        if "status" in state:
            return web.Response(status=state["status"], text="no")
        return web.json_response({"claimed_ref": state.get("claimed_ref", "")})

    app = web.Application()
    app.router.add_get("/broker/minds/{mind_id}/surface-claim", claim)
    server = TestServer(app)
    await server.start_server()
    return server


def _point_at(monkeypatch, server: TestServer) -> None:
    monkeypatch.setattr(scheduler, "SERVER_URL", str(server.make_url("")).rstrip("/"))


def test_a_reminder_goes_to_the_chat_that_claimed_this_minds_bot(monkeypatch):
    async def go():
        server = await _gateway({"claimed_ref": "5550001"})
        try:
            _point_at(monkeypatch, server)
            monkeypatch.setattr(scheduler.config, "telegram_owner_chat_id", 9999999)
            return await scheduler.owner_chat_for("mind-uuid")
        finally:
            await server.close()

    assert asyncio.run(go()) == 5550001


def test_a_mind_whose_bot_nobody_has_claimed_falls_back(monkeypatch):
    async def go():
        server = await _gateway({"claimed_ref": ""})
        try:
            _point_at(monkeypatch, server)
            monkeypatch.setattr(scheduler.config, "telegram_owner_chat_id", 9999999)
            return await scheduler.owner_chat_for("mind-uuid")
        finally:
            await server.close()

    assert asyncio.run(go()) == 9999999


def test_a_gateway_with_no_claim_routes_falls_back(monkeypatch):
    async def go():
        server = await _gateway({"status": 404})
        try:
            _point_at(monkeypatch, server)
            monkeypatch.setattr(scheduler.config, "telegram_owner_chat_id", 9999999)
            return await scheduler.owner_chat_for("mind-uuid")
        finally:
            await server.close()

    assert asyncio.run(go()) == 9999999


def test_a_gateway_that_cannot_be_reached_falls_back(monkeypatch):
    monkeypatch.setattr(scheduler, "SERVER_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(scheduler.config, "telegram_owner_chat_id", 9999999)
    assert asyncio.run(scheduler.owner_chat_for("mind-uuid")) == 9999999


def test_the_discord_claim_is_asked_for_as_discord(monkeypatch):
    async def go():
        state = {"claimed_ref": "4440002"}
        server = await _gateway(state)
        try:
            _point_at(monkeypatch, server)
            ref = await scheduler.claimed_chat_id("mind-uuid", "discord")
            return ref, state["surfaces"]
        finally:
            await server.close()

    ref, surfaces = asyncio.run(go())
    assert ref == "4440002"
    assert surfaces == ["discord"]


def test_one_fire_asks_the_gateway_once_per_mind(monkeypatch):
    async def go():
        state = {"claimed_ref": "5550001"}
        server = await _gateway(state)
        try:
            _point_at(monkeypatch, server)
            await scheduler.owner_chat_for("mind-uuid")
            await scheduler.owner_chat_for("mind-uuid")
            return state["reads"]
        finally:
            await server.close()

    assert asyncio.run(go()) == 1


def test_two_minds_resolve_to_their_own_people(monkeypatch):
    async def go():
        calls: dict[str, str] = {"m-one": "111", "m-two": "222"}

        async def claim(request: web.Request) -> web.Response:
            return web.json_response(
                {"claimed_ref": calls.get(request.match_info["mind_id"], "")}
            )

        app = web.Application()
        app.router.add_get("/broker/minds/{mind_id}/surface-claim", claim)
        server = TestServer(app)
        await server.start_server()
        try:
            _point_at(monkeypatch, server)
            monkeypatch.setattr(scheduler.config, "telegram_owner_chat_id", 9999999)
            return (
                await scheduler.owner_chat_for("m-one"),
                await scheduler.owner_chat_for("m-two"),
            )
        finally:
            await server.close()

    assert asyncio.run(go()) == (111, 222)
