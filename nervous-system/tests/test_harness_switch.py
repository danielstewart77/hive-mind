"""A conversation runs on a harness, and can be moved to another one.

The harness is a property of the conversation like its model: recorded on the
row at birth, carried by a rotation, named on every spawn, attach and pane
rotation, and changed mid-conversation by `/harness`. A switch hands the old
conversation over as plain text — rendered by the mind *before* anything is
torn down — and opens the new harness on it as its first user turn.

The mind is reached over HTTP and only over HTTP, so it is stubbed there: a
fake that answers the routes the gateway calls and records every request,
which is what these tests read back.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import tempfile
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi.testclient import TestClient

from comms import broker
from comms.sessions import SessionManager

MIND = "11111111-1111-1111-1111-111111111111"
MIND_URL = "http://mind.test:8420"

HARNESSES = {
    "harnesses": [
        {"name": "claude", "available": True, "reason": ""},
        {"name": "codex", "available": True, "reason": ""},
        {"name": "dsh", "available": False, "reason": "dsh CLI not found"},
    ],
    "default": "claude",
}

MODELS = {
    "claude": [
        {"name": "claude-opus-5", "context_window": 1_000_000,
         "effort_levels": ["low", "medium", "high", "max"]},
    ],
    "codex": [
        {"name": "gpt-5.6-terra", "context_window": 40_000,
         "effort_levels": ["low", "medium", "high"]},
        {"name": "gpt-5.6-mini", "context_window": None,
         "effort_levels": ["low"]},
    ],
    "dsh": [{"name": "qwen35-131k", "context_window": 131_072, "effort_levels": []}],
}


_OPEN: list[SessionManager] = []


def _run(coro):
    async def guarded():
        # A scenario that fails part-way would otherwise leave its sqlite
        # threads running, and the interpreter waits on them forever.
        try:
            return await coro
        finally:
            while _OPEN:
                await _close(_OPEN[-1])
    return asyncio.run(guarded())


# ---------------------------------------------------------------------------
# The mind, at its HTTP boundary
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status: int, body=None, lines: list[bytes] | None = None):
        self.status = status
        self._body = body
        self.content = _Lines(lines or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body

    async def text(self):
        return json.dumps(self._body) if self._body is not None else ""

    async def read(self):
        return (await self.text()).encode()


class _Lines:
    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class FakeMind:
    """Answers the routes the gateway calls on a mind; records each request."""

    def __init__(self):
        self.requests: list[dict] = []
        self.harnesses = HARNESSES
        self.models = MODELS
        self.handover: tuple[int, dict] = (200, {"text": "HANDOVER: the old conversation"})
        self.failing_spawns: set[str] = set()
        self.turn_events: list[dict] = [{"type": "result", "is_error": False, "result": "ok"}]

    def _answer(self, method: str, url: str, params=None, json_body=None) -> _Resp:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query.update({k: str(v) for k, v in (params or {}).items()})
        path = parts.path
        self.requests.append(
            {"method": method, "path": path, "params": query, "json": json_body}
        )
        if method == "GET" and path == "/harnesses":
            return _Resp(200, self.harnesses)
        if method == "GET" and path == "/models":
            return _Resp(200, {"models": self.models.get(query.get("harness"), [])})
        if method == "POST" and path == "/handover":
            return _Resp(*self.handover)
        if method == "POST" and path == "/sessions":
            if (json_body or {}).get("harness") in self.failing_spawns:
                return _Resp(500, {"error": "harness failed to start"})
            return _Resp(200, {})
        if method == "POST" and path.endswith("/message"):
            lines = [f"data: {json.dumps(e)}\n".encode() for e in self.turn_events]
            return _Resp(200, None, lines)
        if method == "POST" and path.endswith("/rotate-pty"):
            return _Resp(200, {"rotated": True})
        if method == "POST" and path.endswith("/release"):
            return _Resp(200, {"released": True})
        if method == "DELETE":
            return _Resp(200, {})
        return _Resp(404, {"error": "no route"})

    def wired(self):
        mind = self

        class _Http:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get(self, url, params=None, **kwargs):
                return mind._answer("GET", url, params)

            def post(self, url, json=None, params=None, **kwargs):
                return mind._answer("POST", url, params, json)

            def delete(self, url, params=None, **kwargs):
                return mind._answer("DELETE", url, params)

        return patch("aiohttp.ClientSession", _Http)

    # Readers over what was sent.
    def sent(self, method: str, path: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["path"] == path]

    def spawns(self) -> list[dict]:
        return [r["json"] for r in self.sent("POST", "/sessions")]

    def kills(self, session_id: str) -> list[dict]:
        return self.sent("DELETE", f"/sessions/{session_id}")


# ---------------------------------------------------------------------------
# A real manager on a real broker, in a temp dir
# ---------------------------------------------------------------------------

async def _manager(tmp: str, harness: str = "claude_cli") -> SessionManager:
    os.environ["SESSIONS_DB_PATH"] = os.path.join(tmp, "sessions.db")
    mgr = SessionManager()
    await mgr.start()
    _OPEN.append(mgr)
    mgr.broker_db = await broker.init_db(os.path.join(tmp, "broker.db"))
    await broker.register_mind(
        mgr.broker_db, mind_id=MIND, name="ada", gateway_url=MIND_URL,
        model="claude-opus-5", harness=harness,
    )
    return mgr


async def _close(mgr: SessionManager) -> None:
    if mgr in _OPEN:
        _OPEN.remove(mgr)
    # Nothing outside the fake mind is reachable; shutdown must not try.
    mgr._procs.clear()
    await mgr.broker_db.close()
    await mgr.shutdown()


async def _seed(
    mgr: SessionManager, sid: str = "sess-1", *, harness: str = "claude",
    model: str = "claude-opus-5", claude_sid: str = "conv-1",
    harness_sid: str | None = None, effort: str | None = None,
    client_ref: str = "123",
) -> str:
    now = time.time()
    await mgr._db.execute(
        """INSERT INTO sessions (id, owner_type, owner_ref, model, claude_sid,
                                 harness_sid, harness, effort, created_at,
                                 last_active, status, mind_id, summary)
           VALUES (?, 'telegram', ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, 'chat')""",
        (sid, client_ref, model, claude_sid, harness_sid, harness, effort, now, now, MIND),
    )
    await mgr._db.execute(
        "INSERT INTO active_sessions (client_type, client_ref, session_id) VALUES ('telegram', ?, ?)",
        (client_ref, sid),
    )
    await mgr._db.commit()
    return sid


def _soul():
    """Lucent is another service; the composed prompt is all that matters here."""
    return patch(
        "comms.bootstrap_loader.compose_prompt_blocks",
        new=AsyncMock(return_value="SOUL-BLOCKS"),
    )


# ---------------------------------------------------------------------------
# 1. The harness is recorded at birth and carried by a rotation
# ---------------------------------------------------------------------------

def test_a_new_conversation_records_the_minds_default_and_a_successor_keeps_its_parents():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp, harness="claude_cli")
            mind = FakeMind()
            with mind.wired(), _soul():
                born = await mgr.create_session(
                    owner_type="telegram", owner_ref="123", client_ref="123", mind_id=MIND,
                )
                # Moved to codex mid-conversation; the mind's default is still claude.
                await mgr._db.execute(
                    "UPDATE sessions SET harness = 'codex', model = 'gpt-5.6-terra' WHERE id = ?",
                    (born["id"],),
                )
                await mgr._db.commit()
                successor = await mgr.create_session(
                    owner_type="telegram", owner_ref="123", client_ref="123",
                    model="gpt-5.6-terra", mind_id=MIND, rotated_from=born["id"],
                )
            assert born["harness"] == "claude"
            assert (await mgr._get_row(successor["id"]))["harness"] == "codex"
            assert successor["harness"] == "codex"
            assert [s["harness"] for s in mind.spawns()] == ["claude", "codex"]
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 2. Every spawn, attach and pane rotation names the conversation's harness
# ---------------------------------------------------------------------------

def test_spawn_and_pane_rotation_name_the_conversations_harness():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp, harness="claude_cli")
            await _seed(mgr, harness="codex", model="gpt-5.6-terra", effort="high")
            mind = FakeMind()
            with mind.wired():
                await mgr._spawn("sess-1", "gpt-5.6-terra", resume_sid="conv-1", mind_id=MIND)
                await mgr._rotate_pty_on_mind(
                    session_id="sess-1", new_claude_sid="conv-2",
                    model="gpt-5.6-terra", mind_id=MIND, user_prompt="seed",
                )
            assert mind.spawns()[0]["harness"] == "codex"
            rotated = mind.sent("POST", "/sessions/sess-1/rotate-pty")[0]["json"]
            assert rotated["harness"] == "codex"
            assert rotated["effort"] == "high"
            await _close(mgr)

    _run(scenario())


def test_a_row_from_before_the_column_is_spawned_on_the_minds_default():
    """Rows written before the column existed carry NULL; they are resolved
    from the mind's registration, never sent without a harness."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp, harness="codex_cli")
            await _seed(mgr, harness=None, model="gpt-5.6-terra")
            mind = FakeMind()
            with mind.wired():
                await mgr._spawn("sess-1", "gpt-5.6-terra", resume_sid="conv-1", mind_id=MIND)
            assert mind.spawns()[0]["harness"] == "codex"
            assert (await mgr._get_row("sess-1"))["harness"] == "codex"
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 3-5. The command surface asks the mind about the right harness
# ---------------------------------------------------------------------------

@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-bearer")

    from comms import server as server_module

    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
        mgr = server_module.session_mgr
        _run(broker.register_mind(
            mgr.broker_db, mind_id=MIND, name="ada", gateway_url=MIND_URL,
            model="claude-opus-5", harness="claude_cli",
        ))
        yield client, server_module


def _command(client, content: str):
    return client.post("/command", json={
        "content": content, "owner_type": "telegram",
        "client_ref": "123", "owner_ref": "123", "mind_id": MIND,
    }).json()


def test_bare_harness_lists_what_the_mind_offers_marks_the_current_and_says_why_not(app_client):
    client, server_module = app_client
    mgr = server_module.session_mgr
    _run(_seed(mgr, harness="codex", model="gpt-5.6-terra"))
    mind = FakeMind()
    with mind.wired():
        body = _command(client, "/harness")
    assert body["current"] == "codex"
    by_name = {h["name"]: h for h in body["harnesses"]}
    assert by_name["claude"]["available"] is True
    assert by_name["dsh"]["available"] is False
    assert by_name["dsh"]["reason"] == "dsh CLI not found"


def test_picking_a_harness_asks_the_mind_for_that_harnesss_models(app_client):
    client, server_module = app_client
    mgr = server_module.session_mgr
    _run(_seed(mgr, harness="claude"))
    mind = FakeMind()
    with mind.wired():
        body = _command(client, "/harness codex")
    asked = mind.sent("GET", "/models")
    assert [r["params"].get("harness") for r in asked] == ["codex"]
    assert body["harness"] == "codex"
    assert [m["name"] for m in body["models"]] == ["gpt-5.6-terra", "gpt-5.6-mini"]


def test_bare_model_lists_the_conversations_own_harnesss_models(app_client):
    client, server_module = app_client
    mgr = server_module.session_mgr
    _run(_seed(mgr, harness="codex", model="gpt-5.6-terra"))
    mind = FakeMind()
    with mind.wired():
        body = _command(client, "/model")
    assert [r["params"].get("harness") for r in mind.sent("GET", "/models")] == ["codex"]
    assert [m["name"] for m in body["models"]] == ["gpt-5.6-terra", "gpt-5.6-mini"]


def test_effort_and_a_model_switch_check_against_the_conversations_own_harness():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp, harness="claude_cli")
            await _seed(mgr, harness="codex", model="gpt-5.6-terra")
            mind = FakeMind()
            with mind.wired():
                options = await mgr.effort_options("sess-1")
                await mgr.set_effort("sess-1", "medium")
                await mgr.switch_model("sess-1", "gpt-5.6-mini")
            assert options["levels"] == ["low", "medium", "high"]
            assert [r["params"].get("harness") for r in mind.sent("GET", "/models")] == [
                "codex", "codex", "codex",
            ]
            assert (await mgr._get_row("sess-1"))["model"] == "gpt-5.6-mini"
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 6. Refused mid-answer
# ---------------------------------------------------------------------------

def test_a_harness_switch_mid_answer_is_refused_and_goes_through_once_released():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            mind = FakeMind()
            lock = mgr._locks.setdefault("sess-1", asyncio.Lock())
            with mind.wired(), _soul():
                async with lock:
                    with pytest.raises(ValueError):
                        await asyncio.wait_for(
                            mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra"), 5,
                        )
                assert mind.kills("sess-1") == []
                assert (await mgr._get_row("sess-1"))["harness"] == "claude"
                await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
            assert (await mgr._get_row("sess-1"))["harness"] == "codex"
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 7. Only an offered harness and model pair is accepted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("harness,model", [
    ("dsh", "qwen35-131k"),         # a harness the mind reports unavailable
    ("hermes", "gpt-6-astra"),      # a harness the mind does not list at all
    ("codex", "claude-opus-5"),     # a model the target harness does not offer
])
def test_a_switch_to_something_not_offered_is_refused_and_the_row_is_untouched(harness, model):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, harness_sid="thread-0", effort="high")
            before = await mgr._get_row("sess-1")
            mind = FakeMind()
            with mind.wired(), _soul():
                with pytest.raises(ValueError):
                    await mgr.switch_harness("sess-1", harness, model)
            assert await mgr._get_row("sess-1") == before
            assert mind.kills("sess-1") == []
            assert mind.spawns() == []
            await _close(mgr)

    _run(scenario())


def test_an_offered_pair_is_accepted():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            mind = FakeMind()
            with mind.wired(), _soul():
                result = await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
            assert result["harness"] == "codex"
            assert result["model"] == "gpt-5.6-terra"
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 8. The switch itself
# ---------------------------------------------------------------------------

def test_a_switch_keeps_the_row_and_opens_the_new_harness_on_the_handover():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, harness_sid="thread-0")
            await mgr._db.execute(
                "INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at) "
                "VALUES (?, 'ada', '123', 'sess-1', ?, ?)",
                (MIND, json.dumps({"carry_forward": "We were fixing the boiler."}), time.time()),
            )
            await mgr._db.commit()
            mind = FakeMind()
            with mind.wired(), _soul():
                result = await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
            row = await mgr._get_row("sess-1")
            assert result["id"] == "sess-1"
            assert row["harness"] == "codex"
            assert row["model"] == "gpt-5.6-terra"
            assert row["claude_sid"] and row["claude_sid"] != "conv-1"
            assert row["harness_sid"] is None

            # Rendered on the harness being left, before anything was killed.
            handover = mind.sent("POST", "/handover")[0]
            assert handover["json"]["harness"] == "claude"
            assert handover["json"]["claude_sid"] == "conv-1"
            assert handover["json"]["harness_sid"] == "thread-0"
            assert handover["json"]["summary"] == "We were fixing the boiler."
            assert handover["json"]["budget_bytes"] == 40_000 * 4 // 2
            order = [(r["method"], r["path"]) for r in mind.requests]
            assert order.index(("POST", "/handover")) < order.index(("DELETE", "/sessions/sess-1"))
            # The mind's codex thread map forgets the old thread too.
            assert mind.kills("sess-1")[0]["params"].get("forget_thread") == "1"

            spawned = mind.spawns()[-1]
            assert spawned["harness"] == "codex"
            assert spawned["model"] == "gpt-5.6-terra"
            assert spawned["resume_sid"] == row["claude_sid"]
            assert spawned["harness_sid"] is None
            assert spawned["opening_turn"] == "HANDOVER: the old conversation"
            assert spawned["system_prompt_blocks"] == "SOUL-BLOCKS"
            await _close(mgr)

    _run(scenario())


def test_an_unknown_window_budgets_the_handover_at_the_byte_ceiling():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            mind = FakeMind()
            with mind.wired(), _soul():
                await mgr.switch_harness("sess-1", "codex", "gpt-5.6-mini")
            assert mind.sent("POST", "/handover")[0]["json"]["budget_bytes"] == 120_000
            assert mind.sent("POST", "/handover")[0]["json"]["summary"] == ""
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 9. The handover outlives respawns until a clean turn lands
# ---------------------------------------------------------------------------

def test_the_handover_is_reapplied_until_a_turn_completes_without_error():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            mind = FakeMind()
            with mind.wired(), _soul():
                await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
                row = await mgr._get_row("sess-1")
                assert row["carry_forward"] == "HANDOVER: the old conversation"
                assert row["carry_forward_sid"] == row["claude_sid"]

                # The process dies before any turn: the chat respawn re-applies it.
                mgr._procs.pop("sess-1")
                mind.turn_events = [{"type": "result", "is_error": True, "result": "boom"}]
                [e async for e in mgr.send_message("sess-1", "hello")]
                assert mind.spawns()[-1]["opening_turn"] == "HANDOVER: the old conversation"
                assert (await mgr._get_row("sess-1"))["carry_forward"] == (
                    "HANDOVER: the old conversation"
                )

                mind.turn_events = [{"type": "result", "is_error": False, "result": "hi"}]
                [e async for e in mgr.send_message("sess-1", "hello again")]
                assert (await mgr._get_row("sess-1"))["carry_forward"] is None

                mgr._procs.pop("sess-1")
                await mgr._spawn(
                    "sess-1", "gpt-5.6-terra", resume_sid=row["claude_sid"], mind_id=MIND,
                )
            assert mind.spawns()[-1]["opening_turn"] == ""
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 10. An unrenderable conversation is never torn down
# ---------------------------------------------------------------------------

def test_an_unreadable_transcript_with_no_summary_refuses_and_the_old_harness_lives():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, harness_sid="thread-0")
            before = await mgr._get_row("sess-1")
            mind = FakeMind()
            mind.handover = (422, {"detail": "unreadable"})
            with mind.wired(), _soul():
                with pytest.raises(ValueError):
                    await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
            assert mind.kills("sess-1") == []
            assert mind.spawns() == []
            assert await mgr._get_row("sess-1") == before
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 11. A harness that will not start hands the conversation back
# ---------------------------------------------------------------------------

def test_a_new_harness_that_fails_to_start_restores_and_respawns_the_old_one():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, harness_sid="thread-0", effort="max")
            mind = FakeMind()
            mind.failing_spawns = {"codex"}
            with mind.wired(), _soul():
                with pytest.raises(Exception):
                    await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
            row = await mgr._get_row("sess-1")
            assert (row["harness"], row["model"], row["claude_sid"], row["harness_sid"], row["effort"]) == (
                "claude", "claude-opus-5", "conv-1", "thread-0", "max",
            )
            assert row["carry_forward"] is None
            assert [s["harness"] for s in mind.spawns()] == ["codex", "claude"]
            restored = mind.spawns()[-1]
            assert restored["resume_sid"] == "conv-1"
            assert restored["harness_sid"] == "thread-0"
            assert restored["model"] == "claude-opus-5"
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 12. A switch replaces any rotation in flight
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("armed", [1, 2])
def test_a_switch_clears_an_armed_or_staged_rotation(armed):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await mgr._db.execute(
                "UPDATE sessions SET rotation_armed = ?, carry_forward = 'staged seed', "
                "carry_forward_sid = 'conv-staged', carry_forward_at = ? WHERE id = 'sess-1'",
                (armed, time.time()),
            )
            await mgr._db.commit()
            mind = FakeMind()
            with mind.wired(), _soul():
                await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
            row = await mgr._get_row("sess-1")
            assert row["rotation_armed"] == 0
            assert row["carry_forward"] == "HANDOVER: the old conversation"
            await _close(mgr)

    _run(scenario())


def test_an_arm_from_the_conversation_the_switch_replaced_is_ignored():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            mind = FakeMind()
            with mind.wired(), _soul():
                await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
            new_sid = (await mgr._get_row("sess-1"))["claude_sid"]

            stale = await mgr.arm_rotation("telegram", "123", claude_sid="conv-1")
            assert stale["ok"] is False
            assert (await mgr._get_row("sess-1"))["rotation_armed"] == 0

            live = await mgr.arm_rotation("telegram", "123", claude_sid=new_sid)
            assert live["ok"] is True
            assert (await mgr._get_row("sess-1"))["rotation_armed"] == 1
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 13. Effort survives only where the new model takes it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model,kept", [
    ("gpt-5.6-terra", "high"),      # lists the level: kept
    ("gpt-5.6-mini", None),         # does not: back to the model's default
])
def test_a_switch_keeps_the_effort_only_where_the_new_model_lists_it(model, kept):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, effort="high")
            mind = FakeMind()
            with mind.wired(), _soul():
                await mgr.switch_harness("sess-1", "codex", model)
            assert (await mgr._get_row("sess-1"))["effort"] == kept
            assert mind.spawns()[-1]["effort"] == kept
            await _close(mgr)

    _run(scenario())


# ---------------------------------------------------------------------------
# 14. One conversation's switch is that conversation's alone
# ---------------------------------------------------------------------------

def test_a_switch_leaves_other_conversations_and_the_minds_default_alone():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp, harness="claude_cli")
            await _seed(mgr, "sess-1", client_ref="123")
            await _seed(mgr, "sess-2", claude_sid="conv-2", client_ref="456")
            other_before = await mgr._get_row("sess-2")
            mind = FakeMind()
            with mind.wired(), _soul():
                await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
                fresh = await mgr.create_session(
                    owner_type="telegram", owner_ref="789", client_ref="789", mind_id=MIND,
                )
            assert await mgr._get_row("sess-2") == other_before
            assert mind.kills("sess-2") == []
            assert (await broker.get_mind_by_id(mgr.broker_db, MIND))["harness"] == "claude_cli"
            assert fresh["harness"] == "claude"
            await _close(mgr)

    _run(scenario())
