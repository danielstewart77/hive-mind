"""A conversation always fits the model it is on.

Switching to a model that cannot hold the conversation — by its last measured
size against that model's threshold, or by images on a model that takes none
— hands it over to a fresh conversation rather than resuming it. A turn that
overflows the window anyway rotates at once and sends the same message again,
exactly once. A goal run that stops at its threshold is rotated and handed
its remaining rounds. And the dashboard shows every open conversation's
figures, idle or not.

The mind is reached over HTTP and only over HTTP, so it is the fake from
`test_harness_switch` at that boundary; what is asserted is the row the
gateway wrote and the requests it sent.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from unittest.mock import AsyncMock, patch

import pytest

from fastapi.testclient import TestClient

from comms import bootstrap_loader
from comms.sessions import DEFAULT_HARNESS_OVERHEAD_TOKENS as OVERHEAD
from comms.sessions import SessionManager, SwitchWontFit
from tests.test_harness_switch import (
    MIND,
    FakeMind,
    _Resp,
    _close,
    _manager,
    _run,
    _seed,
    _soul,
)

PERCENT = 50
MODELS = {
    "claude": [
        {"name": "claude-opus-5", "context_window": 1_000_000, "accepts_images": True,
         "effort_levels": ["high"]},
        {"name": "claude-sonnet-5", "context_window": 200_000, "accepts_images": True,
         "effort_levels": []},
        {"name": "text-only", "context_window": 200_000, "accepts_images": False,
         "effort_levels": []},
        {"name": "unknown-eyes", "context_window": 200_000, "accepts_images": None,
         "effort_levels": []},
        {"name": "bilby-32k", "context_window": 32_000, "accepts_images": False,
         "effort_levels": []},
    ],
}
OVERFLOW = {"type": "result", "is_error": True, "stop_reason": "error",
            "error_code": "CONTEXT_WINDOW_EXCEEDED", "error": "prompt is too long"}
DONE = {"type": "result", "is_error": False, "result": "ok"}


#: These tests are about their own subject, not about authorization, so they
#: present the admin credential. They used to clear every token instead and
#: rely on the gateway serving open when unconfigured — a bypass that is gone,
#: because a gateway that cannot establish who is calling must refuse rather
#: than answer the LAN.
GRILL_ADMIN = "admin-credential-for-tests"  # secret-guard: allow


class Mind(FakeMind):
    """The fake mind, answering each turn from a queue. Its listing gives every
    model ``percent`` as its rotation threshold, unless ``percents`` names one
    per model; None leaves the field off the rows."""

    def __init__(self, turns: list[list[dict]] | None = None, percent: int | None = PERCENT,
                 percents: dict[str, int] | None = None):
        super().__init__()
        self.models = {
            harness: [
                {**row, "rotation_threshold_percent": (percents or {}).get(row["name"], percent)}
                if (percents or {}).get(row["name"], percent) is not None else dict(row)
                for row in rows
            ]
            for harness, rows in MODELS.items()
        }
        self.turns = list(turns or [])

    def _answer(self, method, url, params=None, json_body=None):
        if method == "POST" and url.endswith("/message") and self.turns:
            self.turn_events = self.turns.pop(0)
        return super()._answer(method, url, params, json_body)

    def messages(self) -> list[dict]:
        return [r for r in self.requests
                if r["method"] == "POST" and r["path"].endswith("/message")]


async def _measured(mgr: SessionManager, sid: str = "sess-1", *, tokens=None,
                    has_images=None, window=200_000, threshold=100_000) -> None:
    await mgr._db.execute(
        "UPDATE sessions SET context_tokens = ?, context_window = ?, "
        "context_threshold = ?, context_has_images = ? WHERE id = ?",
        (tokens, window, threshold, has_images, sid),
    )
    await mgr._db.execute(
        "INSERT INTO session_turns (session_id, role, content, created_at) "
        "VALUES (?, 'user', 'an earlier turn', ?)", (sid, time.time()),
    )
    await mgr._db.commit()


async def _switch(mgr, mind, model, sid="sess-1"):
    with mind.wired(), _soul():
        return await mgr.switch_model(sid, model)


async def _turn(mgr, mind, content, sid="sess-1", **kwargs):
    events = []
    with mind.wired(), _soul():
        async for event in mgr.send_message(sid, content, **kwargs):
            events.append(event)
    return events


def _text(events) -> str:
    return " ".join(
        block.get("text", "")
        for e in events if e.get("type") == "assistant"
        for block in (e.get("message") or {}).get("content") or []
    )


# ---------------------------------------------------------------------------
# T8, receiving side: a report carries everything and names its conversation
# ---------------------------------------------------------------------------

def test_a_context_report_persists_every_figure_it_carries(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", GRILL_ADMIN)
    from comms import server as server_module
    import importlib
    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as client:
        mgr = server_module.session_mgr
        _run(_seed(mgr))
        answer = client.post("/sessions/context", json={
            "client_type": "telegram", "client_ref": "123", "tokens": 61_500,
            "threshold": 100_000, "window": 200_000, "kind": "estimated",
            "has_images": True, "model": "claude-sonnet-5", "conversation_id": "conv-1",
        }).json()
        row = _run(mgr._get_row("sess-1"))
    assert answer["ok"] is True
    assert (row["context_tokens"], row["context_threshold"], row["context_window"]) == (
        61_500, 100_000, 200_000)
    assert row["context_kind"] == "estimated"
    assert row["context_has_images"] == 1
    assert row["context_model"] == "claude-sonnet-5"


def test_a_report_for_a_replaced_conversation_is_dropped():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, claude_sid="conv-2")
            await _measured(mgr, tokens=5_000)
            answer = await mgr.report_context_for_client(
                "telegram", "123", tokens=190_000, conversation_id="conv-1",
            )
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return answer, row

    answer, row = _run(scenario())
    assert answer["ok"] is False
    assert row["context_tokens"] == 5_000


# ---------------------------------------------------------------------------
# T10 / T11: a switch resumes only a conversation the new model can hold
# ---------------------------------------------------------------------------

def test_a_conversation_just_under_the_new_threshold_resumes_under_its_own_id():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=99_999, window=1_000_000, threshold=500_000)
            mind = Mind()
            result = await _switch(mgr, mind, "claude-sonnet-5")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return result, row, mind

    result, row, mind = _run(scenario())
    assert result["id"] == "sess-1"
    assert "warning" not in result
    assert mind.sent("POST", "/handover") == []
    assert mind.spawns()[-1]["resume_sid"] == "conv-1"
    assert row["claude_sid"] == "conv-1"
    assert (row["context_window"], row["context_threshold"]) == (200_000, 200_000 * PERCENT // 100)


def test_a_switch_takes_its_threshold_from_the_new_models_own_percent():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=10_000, window=1_000_000, threshold=500_000)
            mind = Mind(percents={"claude-opus-5": 50, "claude-sonnet-5": 40})
            await _switch(mgr, mind, "claude-sonnet-5")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row

    row = _run(scenario())
    assert (row["context_window"], row["context_threshold"]) == (200_000, 200_000 * 40 // 100)


def test_a_model_row_with_no_percent_falls_back_to_the_conversations_own_ratio():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=10_000, window=1_000_000, threshold=300_000)
            mind = Mind(percent=None)
            await _switch(mgr, mind, "claude-sonnet-5")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row

    row = _run(scenario())
    assert (row["context_window"], row["context_threshold"]) == (200_000, 200_000 * 30 // 100)


def test_a_chat_over_the_new_threshold_is_handed_over_under_the_same_row():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=100_001, window=1_000_000, threshold=500_000)
            await mgr._db.execute("UPDATE sessions SET rotation_armed = 1 WHERE id = 'sess-1'")
            await mgr._db.commit()
            mind = Mind()
            result = await _switch(mgr, mind, "claude-sonnet-5")
            row = await mgr._get_row("sess-1")
            cur = await mgr._db.execute("SELECT COUNT(*) FROM sessions")
            rows = (await cur.fetchone())[0]
            await _close(mgr)
            return result, row, rows, mind

    result, row, rows, mind = _run(scenario())
    assert result["id"] == "sess-1"
    assert rows == 1
    assert row["status"] != "closed"
    assert row["claude_sid"] != "conv-1"
    assert all(s["resume_sid"] != "conv-1" for s in mind.spawns())
    assert mind.spawns()[-1]["resume_sid"] == row["claude_sid"]
    assert row["carry_forward"] and row["carry_forward_sid"] == row["claude_sid"]
    assert mind.spawns()[-1]["opening_turn"] == row["carry_forward"]
    assert row["model"] == "claude-sonnet-5"
    assert (row["context_window"], row["context_threshold"]) == (200_000, 200_000 * PERCENT // 100)
    assert row["context_kind"] == "estimated"
    assert row["rotation_armed"] == 0
    assert "100,001" in result["warning"]


def test_a_handover_asks_the_mind_to_drop_the_replaced_conversations_rotation_marker():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=100_001, window=1_000_000, threshold=500_000)
            mind = Mind()
            await _switch(mgr, mind, "claude-sonnet-5")
            await _close(mgr)
            return mind

    cleared = _run(scenario()).sent("DELETE", "/sessions/sess-1/rotation-marker")
    assert [c["params"].get("claude_sid") for c in cleared] == ["conv-1"]


def test_an_unmeasured_conversation_that_has_had_turns_is_never_assumed_to_fit():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=None)
            mind = Mind()
            await _switch(mgr, mind, "claude-sonnet-5")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row, mind

    row, mind = _run(scenario())
    assert row["claude_sid"] != "conv-1"
    assert len(mind.sent("POST", "/handover")) == 1


def test_images_on_a_text_only_model_hand_over_even_when_the_size_fits():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=1_000)
            await mgr.report_context("sess-1", has_images=True, conversation_id="conv-1")
            await _switch(mgr, Mind(), "text-only")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row

    assert _run(scenario())["claude_sid"] != "conv-1"


def test_images_on_a_model_whose_image_support_is_unknown_hand_over():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=1_000, has_images=1)
            await _switch(mgr, Mind(), "unknown-eyes")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row

    assert _run(scenario())["claude_sid"] != "conv-1"


def test_a_conversation_with_no_images_resumes_on_a_text_only_model():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=1_000, has_images=0)
            await _switch(mgr, Mind(), "text-only")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row

    row = _run(scenario())
    assert row["claude_sid"] == "conv-1"
    assert row["model"] == "text-only"


def test_a_terminal_handed_over_keeps_its_row_and_respawns_its_pane_on_the_handover():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=150_000, window=1_000_000, threshold=500_000)
            await mgr._db.execute(
                "UPDATE sessions SET terminal_held = 1, rotation_armed = 2, "
                "carry_forward = 'stale seed', carry_forward_sid = 'conv-9' WHERE id = 'sess-1'"
            )
            await mgr._db.commit()
            mind = Mind()
            result = await _switch(mgr, mind, "claude-sonnet-5")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return result, row, mind

    result, row, mind = _run(scenario())
    assert result["id"] == "sess-1"
    assert row["claude_sid"] != "conv-1"
    rotated = mind.sent("POST", "/sessions/sess-1/rotate-pty")[0]["json"]
    assert rotated["new_claude_sid"] == row["claude_sid"]
    assert rotated["model"] == "claude-sonnet-5"
    assert rotated["user_prompt"] == row["carry_forward"]
    assert row["carry_forward_sid"] == row["claude_sid"]
    assert row["rotation_armed"] == 0
    assert (row["context_window"], row["context_threshold"]) == (200_000, 100_000)
    # The pane was handed the handover alone, and is sized by it.
    assert row["context_tokens"] == len(rotated["user_prompt"]) // 4 + OVERHEAD["claude"]
    assert row["context_kind"] == "estimated"


class NoPane(Mind):
    def _answer(self, method, url, params=None, json_body=None):
        if method == "POST" and url.endswith("/rotate-pty"):
            self.requests.append({"method": method, "path": "/rotate-pty",
                                  "params": {}, "json": json_body})
            return _Resp(200, {"rotated": False})
        return super()._answer(method, url, params, json_body)


def test_a_terminal_whose_pane_is_gone_gets_a_process_on_the_new_conversation():
    """A new conversation id is written only with something running it."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=150_000, window=1_000_000, threshold=500_000)
            await mgr._db.execute("UPDATE sessions SET terminal_held = 1 WHERE id = 'sess-1'")
            await mgr._db.commit()
            mind = NoPane()
            await _switch(mgr, mind, "claude-sonnet-5")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row, mind

    row, mind = _run(scenario())
    assert row["claude_sid"] != "conv-1"
    assert [s["resume_sid"] for s in mind.spawns()] == [row["claude_sid"]]


def test_a_model_handover_leaves_room_for_the_soul_and_the_harness_under_the_threshold():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=80_000, window=1_000_000, threshold=300_000)
            mind = Mind(percent=30)
            await _switch(mgr, mind, "claude-sonnet-5")
            await _close(mgr)
            return mind

    mind = _run(scenario())
    budget = mind.sent("POST", "/handover")[0]["json"]["budget_bytes"]
    threshold = 200_000 * 30 // 100
    assert budget == (threshold - OVERHEAD["claude"]) * 4 - len("SOUL-BLOCKS".encode())


def test_a_model_change_onto_a_model_with_no_room_for_the_harness_is_refused():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=50_000, window=1_000_000, threshold=300_000)
            before = await mgr._get_row("sess-1")
            mind = Mind(percent=30)
            with pytest.raises(SwitchWontFit) as refused:
                await _switch(mgr, mind, "bilby-32k")
            after = await mgr._get_row("sess-1")
            await _close(mgr)
            return refused.value, before, after, mind

    refused, before, after, mind = _run(scenario())
    assert refused.status_code == 409
    assert "bilby-32k" in refused.detail and f"{32_000 * 30 // 100:,}" in refused.detail
    assert after == before
    assert mind.sent("POST", "/handover") == []
    assert mind.kills("sess-1") == []


def test_a_chat_rotation_trims_its_carry_forward_to_the_models_threshold():
    """The composed successor prompt — soul, recent memory and the rotation
    summary — has to fit where the conversation will rotate next."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=1_000, window=200_000, threshold=60_000)
            await mgr._db.execute(
                "INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at) "
                "VALUES (?, 'ada', '123', 'sess-1', ?, ?)",
                (MIND, json.dumps({"carry_forward": "x" * 200_000 + " LAST WORDS"}), time.time()),
            )
            await mgr._db.execute("UPDATE sessions SET rotation_armed = 1 WHERE id = 'sess-1'")
            await mgr._db.commit()
            mind = Mind()
            with mind.wired(), \
                    patch.object(bootstrap_loader, "_fetch_soul", return_value="<soul>me</soul>"), \
                    patch.object(bootstrap_loader, "_fetch_recent_memory", return_value=""):
                async for _ in mgr.send_message("sess-1", "hello"):
                    pass
            await _close(mgr)
            return mind

    mind = _run(scenario())
    blocks = mind.spawns()[-1]["system_prompt_blocks"]
    # The harness's own overhead sits under the threshold beside it.
    room = (60_000 - OVERHEAD["claude"]) * 4
    assert room - 1_000 <= len(blocks.encode()) <= room
    assert blocks.rstrip().endswith("LAST WORDS\n</session-memory>".rstrip())


# ---------------------------------------------------------------------------
# T12: an overflow rotates and re-sends exactly once
# ---------------------------------------------------------------------------

TOOL_TURN = [
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "make deploy"}},
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/never"}},
    ]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "deployed"},
    ]}},
    OVERFLOW,
]


def _db_path(mgr) -> str:
    import os
    return os.environ["SESSIONS_DB_PATH"]


class Watching(Mind):
    """Reads the row, through its own connection, the moment a turn arrives."""

    def __init__(self, *a, db_path: str = "", **k):
        super().__init__(*a, **k)
        self.db_path = db_path
        self.rows_at_message: list[dict] = []

    def _answer(self, method, url, params=None, json_body=None):
        if method == "POST" and url.endswith("/message"):
            import sqlite3
            with sqlite3.connect(self.db_path) as db:
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT * FROM sessions WHERE id = 'sess-1'").fetchone()
                self.rows_at_message.append(dict(row))
        return super()._answer(method, url, params, json_body)


def test_an_overflowed_turn_rotates_and_sends_the_same_message_once_on_the_same_row():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=180_000)
            mind = Watching(turns=[TOOL_TURN, [DONE]], db_path=_db_path(mgr))
            events = await _turn(mgr, mind, "ship it")
            row = await mgr._get_row("sess-1")
            ledger = await mgr.get_session_turns("sess-1")
            await _close(mgr)
            return events, row, ledger, mind

    events, row, ledger, mind = _run(scenario())
    sent = mind.messages()
    assert [r["path"] for r in sent] == ["/sessions/sess-1/message"] * 2
    assert sent[1]["json"]["content"].endswith("\nship it")
    assert row["claude_sid"] != "conv-1"
    assert [t["content"] for t in ledger].count("ship it") == 1
    opening = [s for s in mind.spawns() if s["resume_sid"] == row["claude_sid"]][0]["opening_turn"]
    assert 'Bash {"command": "make deploy"} (done)' in opening
    assert "/never" not in opening
    # The re-send was on record before it went out, and settled once answered.
    assert mind.rows_at_message[1]["resend_content"] == "ship it"
    assert row["resend_content"] is None
    assert "ran out of room" in _text(events)
    assert events[-1] == DONE


def test_a_message_typed_during_an_overflow_waits_behind_the_resend():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=180_000)
            mind = Mind(turns=[[OVERFLOW], [DONE], [DONE]])
            with mind.wired(), _soul():
                async def drain(text):
                    return [e async for e in mgr.send_message("sess-1", text)]
                first = asyncio.create_task(drain("ship it"))
                await asyncio.sleep(0)
                second = asyncio.create_task(drain("and then?"))
                results = await asyncio.gather(first, second)
            await _close(mgr)
            return results, mind

    results, mind = _run(scenario())
    contents = [m["json"]["content"].rsplit("\n", 1)[-1] for m in mind.messages()]
    assert contents == ["ship it", "ship it", "and then?"]
    assert results[1][-1] == DONE


def test_an_overflowed_turn_is_resent_with_its_images_and_goal_rounds():
    picture = [{"media_type": "image/png", "data": "AAAA"}]

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=180_000)
            mind = Mind(turns=[[OVERFLOW], [DONE]])
            await _turn(mgr, mind, "look", images=picture, goal_rounds=4)
            await _close(mgr)
            return mind

    resent = _run(scenario()).messages()[1]["json"]
    assert resent["images"] == picture
    assert resent["goal_rounds"] == 4


def test_a_harness_that_only_tallies_its_calls_is_not_said_to_have_run_none():
    tallied = {**OVERFLOW, "traffic": {"emitted": 3, "succeeded": 2, "failed": 1}}

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, harness="dsh")
            await _measured(mgr, tokens=180_000)
            mind = Mind(turns=[[tallied], [DONE]])
            await _turn(mgr, mind, "ship it")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row, mind

    row, mind = _run(scenario())
    opening = [s for s in mind.spawns() if s["resume_sid"] == row["claude_sid"]][0]["opening_turn"]
    assert "completed 2 tool call" in opening
    assert "no tool calls" not in opening


def test_an_overflow_on_a_row_that_never_reported_is_sized_from_the_models_listing():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, model="claude-sonnet-5")
            await _measured(mgr, tokens=None, window=None, threshold=None)
            mind = Mind(turns=[[OVERFLOW], [DONE]])
            await _turn(mgr, mind, "ship it")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row, mind

    row, mind = _run(scenario())
    threshold = 200_000 * PERCENT // 100
    budget = mind.sent("POST", "/handover")[0]["json"]["budget_bytes"]
    assert budget <= (threshold - OVERHEAD["claude"]) * 4
    assert (row["context_window"], row["context_threshold"]) == (200_000, threshold)


def test_a_resent_message_that_overflows_again_is_reported_and_not_rotated_again():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=180_000)
            mind = Mind(turns=[[OVERFLOW], [OVERFLOW], [DONE]])
            events = await _turn(mgr, mind, "ship it")
            await _close(mgr)
            return events, mind

    events, mind = _run(scenario())
    assert len(mind.sent("POST", "/handover")) == 1
    assert len(mind.messages()) == 2
    assert events[-1]["is_error"] is True
    assert "again" in _text(events)


def test_a_resend_interrupted_by_a_restart_goes_out_once_with_what_it_carried():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await mgr._db.execute(
                "UPDATE sessions SET resend_content = 'ship it', resend_images = ?, "
                "resend_goal_rounds = 2 WHERE id = 'sess-1'",
                (json.dumps([{"media_type": "image/png", "data": "AAAA"}]),),
            )
            await mgr._db.commit()
            mind = Mind()
            with mind.wired(), _soul():
                first = await mgr.resume_pending_resends()
                second = await mgr.resume_pending_resends()
            await _close(mgr)
            return first, second, mind

    first, second, mind = _run(scenario())
    assert (first, second) == (1, 0)
    [sent] = mind.messages()
    assert sent["json"]["content"].endswith("\nship it")
    assert sent["json"]["images"] == [{"media_type": "image/png", "data": "AAAA"}]
    assert sent["json"]["goal_rounds"] == 2


def test_a_resend_whose_answer_never_arrives_is_stamped_and_not_sent_again():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=180_000)
            mind = Mind(turns=[[OVERFLOW], []])
            await _turn(mgr, mind, "ship it")
            row = await mgr._get_row("sess-1")
            owed = await mgr.pending_resends()
            await _close(mgr)
            return row, owed

    row, owed = _run(scenario())
    assert row["resend_content"] == "ship it"
    assert row["resend_dispatched_at"] is not None
    assert owed == []


class Refusing(Mind):
    def _answer(self, method, url, params=None, json_body=None):
        if method == "POST" and url.endswith("/message"):
            self.requests.append({"method": method, "path": "/x/message",
                                  "params": {}, "json": json_body})
            return _Resp(500, {"error": "harness fell over"})
        return super()._answer(method, url, params, json_body)


def test_a_resend_the_mind_answers_with_an_error_is_no_longer_owed():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await mgr._db.execute(
                "UPDATE sessions SET resend_content = 'ship it' WHERE id = 'sess-1'"
            )
            await mgr._db.commit()
            with Refusing().wired(), _soul():
                await mgr.resume_pending_resends()
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row

    row = _run(scenario())
    assert row["resend_content"] is None
    assert row["resend_dispatched_at"] is None


def test_the_gateway_sends_an_owed_resend_when_it_starts(tmp_path, monkeypatch):
    import os
    from comms import broker as broker_mod
    from comms import server as server_module
    import importlib

    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))

    async def before_the_restart():
        mgr = SessionManager()
        os.environ["SESSIONS_DB_PATH"] = str(tmp_path / "sessions.db")
        await mgr.start()
        mgr.broker_db = await broker_mod.init_db(str(tmp_path / "broker.db"))
        await broker_mod.register_mind(
            mgr.broker_db, mind_id=MIND, name="ada", gateway_url="http://mind.test:8420",
            model="claude-opus-5", harness="claude_cli", owner="daniel",
        )
        await _seed(mgr)
        await mgr._db.execute(
            "UPDATE sessions SET resend_content = 'ship it' WHERE id = 'sess-1'"
        )
        await mgr._db.commit()
        await mgr.broker_db.close()
        await mgr.shutdown()

    _run(before_the_restart())
    mind = Mind()
    importlib.reload(server_module)
    with mind.wired(), _soul(), TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}):
        deadline = time.time() + 5
        while not mind.messages() and time.time() < deadline:
            time.sleep(0.05)
    assert [m["json"]["content"].endswith("\nship it") for m in mind.messages()] == [True]


def test_goal_rounds_reach_the_mind_through_the_message_route(tmp_path, monkeypatch):
    import importlib
    from comms import server as server_module

    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", GRILL_ADMIN)
    importlib.reload(server_module)
    mind = Mind()
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as client:
        mgr = server_module.session_mgr
        from comms import broker as broker_mod
        _run_in(client, broker_mod.register_mind(
            mgr.broker_db, mind_id=MIND, name="ada", gateway_url="http://mind.test:8420",
            model="claude-opus-5", harness="claude_cli", owner="daniel",
        ))
        _run_in(client, _seed(mgr))
        with mind.wired(), _soul():
            client.post("/sessions/sess-1/message",
                        json={"content": "build it", "goal_rounds": 4}).read()
    assert mind.messages()[0]["json"]["goal_rounds"] == 4


def _run_in(client, coro):
    """Run on the app's own loop, where its database connection lives."""
    return client.portal.call(lambda: coro)


# ---------------------------------------------------------------------------
# TG2: a goal stopped at its threshold is rotated and re-dispatched once
# ---------------------------------------------------------------------------

def test_a_goal_stopped_for_rotation_is_redispatched_once_with_its_rounds_left():
    stopped = {"type": "result", "is_error": False, "stop_reason": "rotation_needed",
               "goal": {"objective": "build the widget", "roundsLeft": 3}}

    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=100_500)
            mind = Mind(turns=[[stopped], [DONE]])
            events = await _turn(mgr, mind, "build the widget", goal_rounds=5)
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return events, row, mind

    events, row, mind = _run(scenario())
    assert len(mind.sent("POST", "/handover")) == 1
    assert row["claude_sid"] != "conv-1"
    sent = mind.messages()
    assert [r["path"] for r in sent] == ["/sessions/sess-1/message"] * 2
    assert sent[0]["json"]["goal_rounds"] == 5
    assert sent[1]["json"]["goal_rounds"] == 3
    assert sent[1]["json"]["content"].endswith("\nbuild the widget")
    assert events[-1] == DONE


# ---------------------------------------------------------------------------
# A rotation arm, and a report, that would write over the wrong thing
# ---------------------------------------------------------------------------

async def _just_rotated(mgr) -> float:
    began = time.time()
    await mgr._db.execute(
        "UPDATE sessions SET conversation_started_at = ? WHERE id = 'sess-1'", (began,)
    )
    await mgr._db.commit()
    return began


def test_an_arm_naming_no_conversation_from_a_hook_older_than_the_conversation_is_refused():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            began = await _just_rotated(mgr)
            answer = await mgr.arm_rotation("telegram", "123", started_at=began - 60)
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return answer, row

    answer, row = _run(scenario())
    assert answer["ok"] is False
    assert row["rotation_armed"] == 0


def test_an_arm_naming_neither_conversation_nor_start_is_refused_on_a_fresh_conversation():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _just_rotated(mgr)
            answer = await mgr.arm_rotation("telegram", "123")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return answer, row

    answer, row = _run(scenario())
    assert answer["ok"] is False
    assert row["rotation_armed"] == 0


def test_a_handover_dates_the_new_conversation_so_a_late_arm_is_refused():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=100_001, window=1_000_000, threshold=500_000)
            hook_started = time.time() - 1
            await _switch(mgr, Mind(), "claude-sonnet-5")
            answer = await mgr.arm_rotation("telegram", "123", started_at=hook_started)
            await _close(mgr)
            return answer

    assert _run(scenario())["ok"] is False


def test_a_report_of_a_zero_window_leaves_the_known_window_alone():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=1_000)
            await mgr.report_context("sess-1", tokens=2_000, window=0, threshold=0)
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row

    row = _run(scenario())
    assert (row["context_tokens"], row["context_window"], row["context_threshold"]) == (
        2_000, 200_000, 100_000)


# ---------------------------------------------------------------------------
# T13: the dashboard shows an idle conversation's figures
# ---------------------------------------------------------------------------

def test_an_idle_conversation_shows_its_size_threshold_and_window():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await mgr.report_context("sess-1", tokens=42_000, threshold=100_000,
                                     window=200_000, kind="usage", now=10.0)
            board = await mgr.live_dashboard(now=40.0)
            await _close(mgr)
            return board

    board = _run(scenario())
    assert board["sessions"] == []
    idle = {s["session_id"]: s for s in board["idle"]}
    context = idle["sess-1"]["context"]
    assert (context["tokens"], context["threshold"], context["window"]) == (
        42_000, 100_000, 200_000)
    assert context["kind"] == "usage"
    assert context["age_seconds"] == 30.0


# ---------------------------------------------------------------------------
# A conversation always knows its size: whatever starts a conversation over
# under its row records the size of what it was opened with, as an estimate
# ---------------------------------------------------------------------------

SOUL = "<soul>" + "who I am. " * 400 + "</soul>"
CODEX = [
    {"name": "codex-200k", "context_window": 200_000, "accepts_images": True,
     "effort_levels": [], "rotation_threshold_percent": 50},
    {"name": "codex-other", "context_window": 200_000, "accepts_images": True,
     "effort_levels": [], "rotation_threshold_percent": 50},
    {"name": "codex-8k", "context_window": 8_000, "accepts_images": True,
     "effort_levels": [], "rotation_threshold_percent": 50},
    {"name": "codex-24k", "context_window": 24_000, "accepts_images": True,
     "effort_levels": [], "rotation_threshold_percent": 50},
    {"name": "codex-40k", "context_window": 40_000, "accepts_images": True,
     "effort_levels": [], "rotation_threshold_percent": 30},
]


def _codex_mind() -> Mind:
    mind = Mind()
    mind.models["codex"] = [dict(row) for row in CODEX]
    mind.transcripts["conv-1"] = "the old conversation. " * 1_000
    return mind


def _big_soul():
    return patch("comms.bootstrap_loader.compose_prompt_blocks",
                 new=AsyncMock(return_value=SOUL))


async def _with_history(mgr) -> None:
    """A conversation that has had turns and was measured, with a summary."""
    await _measured(mgr, tokens=6_000, window=1_000_000, threshold=500_000)
    await mgr._db.execute(
        "INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at) "
        "VALUES (?, 'ada', '123', 'sess-1', ?, ?)",
        (MIND, json.dumps({"carry_forward": "We were fixing the boiler."}), time.time()),
    )
    await mgr._db.commit()


@pytest.mark.parametrize("in_pane", [False, True], ids=["chat", "pane"])
def test_a_harness_switch_leaves_the_conversation_holding_an_estimate_of_what_it_opened_on(in_pane):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                if in_pane:
                    await mgr.adopt_into_terminal("sess-1")
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
            row = await mgr._get_row("sess-1")
            board = await mgr.live_dashboard()
            await _close(mgr)
            return row, board, mind

    row, board, mind = _run(scenario())
    if in_pane:
        # The pane is handed the handover alone, as its opening user turn.
        opened_on = mind.sent("POST", "/sessions/sess-1/rotate-pty")[0]["json"]["user_prompt"]
    else:
        spawned = mind.spawns()[-1]
        opened_on = spawned["system_prompt_blocks"] + spawned["opening_turn"]
    # What it opened on, plus what the harness puts in front of it.
    assert row["context_tokens"] == len(opened_on) // 4 + OVERHEAD["codex"]
    assert row["context_kind"] == "estimated"
    assert row["context_model"] == "codex-200k"
    assert (row["context_window"], row["context_threshold"]) == (200_000, 200_000 * 50 // 100)
    # The dashboard draws that estimate, against the new model's room,
    # rather than a blank.
    shown = {s["session_id"]: s for s in board["sessions"] + board["idle"]}["sess-1"]["context"]
    assert (shown["tokens"], shown["kind"], shown["window"]) == (
        row["context_tokens"], "estimated", 200_000)


def test_a_model_change_right_after_a_harness_switch_that_fits_stays_in_place():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                first = await mgr._get_row("sess-1")
                result = await mgr.switch_model("sess-1", "codex-other")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return first, result, row, mind

    first, result, row, mind = _run(scenario())
    assert "warning" not in result
    assert row["model"] == "codex-other"
    assert row["claude_sid"] == first["claude_sid"]
    assert len(mind.sent("POST", "/handover")) == 1
    assert row["carry_forward"] == first["carry_forward"]
    assert mind.spawns()[-1]["resume_sid"] == first["claude_sid"]
    assert mind.spawns()[-1]["opening_turn"] == first["carry_forward"]


def test_a_model_change_right_after_a_harness_switch_that_does_not_fit_hands_over_by_size():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                first = await mgr._get_row("sess-1")
                mind.transcripts[first["claude_sid"]] = "a newer exchange"
                result = await mgr.switch_model("sess-1", "codex-24k")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return first, result, row, mind

    first, result, row, mind = _run(scenario())
    assert first["context_tokens"] > 24_000 * 50 // 100
    assert result["handed_over"] is True
    assert f"about {first['context_tokens']:,} tokens" in result["warning"]
    assert "hasn't been measured" not in result["warning"]
    assert row["claude_sid"] not in (first["claude_sid"], "conv-1")
    spawned = mind.spawns()[-1]
    assert spawned["opening_turn"] == row["carry_forward"]
    assert row["context_tokens"] == (
        len(spawned["system_prompt_blocks"]) + len(spawned["opening_turn"])) // 4 + OVERHEAD["codex"]
    assert row["context_tokens"] != first["context_tokens"]
    assert (row["context_kind"], row["context_model"]) == ("estimated", "codex-24k")
    assert row["context_threshold"] == 24_000 * 50 // 100


def test_a_chat_rotations_successor_holds_an_estimate_of_its_opening_prompt():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=90_000)
            await mgr._db.execute(
                "INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at) "
                "VALUES (?, 'ada', '123', 'sess-1', ?, ?)",
                (MIND, json.dumps({"carry_forward": "We were fixing the boiler. " * 300}),
                 time.time()),
            )
            await mgr._db.execute("UPDATE sessions SET rotation_armed = 1 WHERE id = 'sess-1'")
            await mgr._db.commit()
            mind = Mind()
            with mind.wired(), \
                    patch.object(bootstrap_loader, "_fetch_soul", return_value=SOUL), \
                    patch.object(bootstrap_loader, "_fetch_recent_memory", return_value=""):
                async for _ in mgr.send_message("sess-1", "hello"):
                    pass
            cur = await mgr._db.execute("SELECT * FROM sessions WHERE rotated_from = 'sess-1'")
            successor = dict(await cur.fetchone())
            await _close(mgr)
            return successor, mind

    successor, mind = _run(scenario())
    spawned = [s for s in mind.spawns() if s["session_id"] == successor["id"]][0]
    assert successor["context_tokens"] == (
        len(spawned["system_prompt_blocks"]) // 4 + OVERHEAD["claude"])
    assert successor["context_kind"] == "estimated"
    assert successor["context_model"] == successor["model"]
    # The same model, so the same room to draw the estimate against.
    assert (successor["context_window"], successor["context_threshold"]) == (200_000, 100_000)


def test_a_report_that_measured_nothing_leaves_the_estimate_in_place():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
            first = await mgr._get_row("sess-1")
            await mgr.report_context("sess-1", tokens=0, kind="none",
                                     conversation_id=first["claude_sid"])
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return first, row

    first, row = _run(scenario())
    assert (row["context_tokens"], row["context_kind"]) == (first["context_tokens"], "estimated")


@pytest.mark.parametrize("in_pane", [False, True], ids=["chat", "pane"])
def test_a_harness_switch_onto_a_small_model_sizes_the_handover_under_its_threshold(in_pane):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                if in_pane:
                    await mgr.adopt_into_terminal("sess-1")
                await mgr.switch_harness("sess-1", "codex", "codex-40k")
            await _close(mgr)
            return mind

    budget = _run(scenario()).sent("POST", "/handover")[0]["json"]["budget_bytes"]
    # Sized as if the soul rides beside it, a pane's too: a pane may be gone,
    # or adopted by a chat surface, before the handover lands.
    assert budget == (40_000 * 30 // 100 - OVERHEAD["codex"]) * 4 - len(SOUL.encode())


def test_the_first_real_report_replaces_the_opening_estimate():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
            first = await mgr._get_row("sess-1")
            await mgr.report_context("sess-1", tokens=first["context_tokens"] + 7_321,
                                     kind="usage", has_images=True, model="codex-200k",
                                     conversation_id=first["claude_sid"])
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return first, row

    first, row = _run(scenario())
    assert first["context_kind"] == "estimated"
    assert (row["context_tokens"], row["context_kind"], row["context_has_images"]) == (
        first["context_tokens"] + 7_321, "usage", 1)


def test_a_report_that_measured_nothing_on_a_row_with_no_size_still_records_it_as_none():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await mgr.report_context("sess-1", tokens=0, kind="none", conversation_id="conv-1")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row

    row = _run(scenario())
    assert (row["context_tokens"], row["context_kind"]) == (0, "none")
    assert row["context_observed_at"] is not None


def test_a_harness_switch_never_borrows_the_old_models_rotation_ratio():
    """A model whose own row names no percentage has no known threshold; the
    ratio the old model's reports implied belongs to the old model."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=6_000, window=1_000_000, threshold=140_000)
            mind = FakeMind()
            with mind.wired(), _soul():
                await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row, mind

    row, mind = _run(scenario())
    # Half the window, less the harness's own overhead and the soul.
    assert mind.sent("POST", "/handover")[0]["json"]["budget_bytes"] == (
        (40_000 // 2 - OVERHEAD["codex"]) * 4 - len("SOUL-BLOCKS".encode()))
    assert (row["context_window"], row["context_threshold"]) == (40_000, None)


def test_an_empty_conversation_switched_onto_a_model_of_unknown_room_changes_model_in_place():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            mind = FakeMind()
            mind.transcripts = {}
            with mind.wired(), _soul():
                await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
                first = await mgr._get_row("sess-1")
                result = await mgr.switch_model("sess-1", "gpt-5.6-mini")
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return first, result, row

    first, result, row = _run(scenario())
    assert "warning" not in result
    assert row["claude_sid"] == first["claude_sid"]
    assert row["model"] == "gpt-5.6-mini"


# ---------------------------------------------------------------------------
# The harness's own overhead is learned and counted
# ---------------------------------------------------------------------------

def test_the_first_real_report_teaches_the_overhead_the_next_opening_counts():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                first = await mgr._get_row("sess-1")
                spawned = mind.spawns()[-1]
                opening = len(spawned["system_prompt_blocks"] + spawned["opening_turn"]) // 4
                await mgr.report_context("sess-1", tokens=opening + 12_345, kind="usage",
                                         model="codex-200k", surface="telegram",
                                         conversation_id=first["claude_sid"])
                # Back and over again: the next opening counts what was learned.
                await mgr.switch_harness("sess-1", "claude", "claude-opus-5")
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
            row = await mgr._get_row("sess-1")
            spawned = mind.spawns()[-1]
            await _close(mgr)
            return row, spawned

    row, spawned = _run(scenario())
    assert row["context_tokens"] == (
        len(spawned["system_prompt_blocks"] + spawned["opening_turn"]) // 4 + 12_345)


def test_a_harness_estimate_teaches_no_overhead():
    """A count read off the transcript leaves out what the harness sent, so it
    says nothing about the overhead."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                first = await mgr._get_row("sess-1")
                await mgr.report_context("sess-1", tokens=50, kind="estimated",
                                         model="codex-200k",
                                         conversation_id=first["claude_sid"])
                await mgr.switch_harness("sess-1", "claude", "claude-opus-5")
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
            row = await mgr._get_row("sess-1")
            spawned = mind.spawns()[-1]
            await _close(mgr)
            return row, spawned

    row, spawned = _run(scenario())
    assert row["context_tokens"] == (
        len(spawned["system_prompt_blocks"] + spawned["opening_turn"]) // 4 + OVERHEAD["codex"])


def test_a_composed_prompt_too_big_for_the_switch_is_trimmed_under_the_threshold():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            await mgr._db.execute(
                "INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at) "
                "VALUES (?, 'ada', '123', 'sess-1', ?, ?)",
                (MIND, json.dumps({"carry_forward": "x" * 200_000 + " LAST WORDS"}),
                 time.time() + 1),
            )
            await mgr._db.commit()
            mind = _codex_mind()
            with mind.wired(), \
                    patch.object(bootstrap_loader, "_fetch_soul", return_value="<soul>me</soul>"), \
                    patch.object(bootstrap_loader, "_fetch_recent_memory", return_value=""):
                await mgr.switch_harness("sess-1", "codex", "codex-40k")
            await _close(mgr)
            return mind

    blocks = _run(scenario()).spawns()[-1]["system_prompt_blocks"]
    room = (40_000 * 30 // 100 - OVERHEAD["codex"]) * 4
    assert len(blocks.encode()) <= room
    assert blocks.rstrip().endswith("LAST WORDS\n</session-memory>".rstrip())


async def _teach(mgr, extra: int, *, kind: str = "usage", named: bool = True,
                 surface: str | None = "telegram", model: str | None = None) -> dict:
    """The conversation's first report: its opening plus ``extra``."""
    row = await mgr._get_row("sess-1")
    await mgr.report_context(
        "sess-1", tokens=row["context_opening_tokens"] + extra, kind=kind,
        model=model or row["model"], surface=surface,
        conversation_id=row["claude_sid"] if named else None,
    )
    return row


async def _over_and_back(mgr, model="codex-200k"):
    """Switch onto codex ``model`` again, by way of claude."""
    await mgr.switch_harness("sess-1", "claude", "claude-opus-5")
    await mgr.switch_harness("sess-1", "codex", model)


def test_the_least_overhead_seen_stands_and_nothing_or_less_teaches_nothing():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            seen = []
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                for extra in (12_000, 20_000, 7_000, -500):
                    await _teach(mgr, extra)
                    seen.append(await mgr.harness_overhead(MIND, "codex", "codex-200k"))
                    await _over_and_back(mgr)
            await _close(mgr)
            return seen

    assert _run(scenario()) == [12_000, 12_000, 7_000, 7_000]


def test_another_models_overhead_stands_in_and_a_panes_is_kept_apart():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.adopt_into_terminal("sess-1")
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                await _teach(mgr, 15_000, surface="terminal")
            pane = await mgr.harness_overhead(MIND, "codex", "codex-other", "pane")
            process = await mgr.harness_overhead(MIND, "codex", "codex-other", "process")
            await _close(mgr)
            return pane, process

    assert _run(scenario()) == (15_000, OVERHEAD["codex"])


def test_a_harness_estimate_first_leaves_the_opening_for_the_provider_count_after():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                row = await _teach(mgr, 50, kind="estimated")
                await mgr.report_context(
                    "sess-1", tokens=row["context_opening_tokens"] + 11_111, kind="usage",
                    model="codex-200k", surface="telegram", conversation_id=row["claude_sid"],
                )
            learned = await mgr.harness_overhead(MIND, "codex", "codex-200k")
            await _close(mgr)
            return learned

    assert _run(scenario()) == 11_111


def test_a_count_from_after_the_first_turn_or_naming_no_conversation_teaches_nothing():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                await _teach(mgr, 3_000, named=False)
                unnamed = await mgr.harness_overhead(MIND, "codex", "codex-200k")
                await _over_and_back(mgr)
                for _ in range(2):
                    await mgr._db.execute(
                        "INSERT INTO session_turns (session_id, role, content, created_at) "
                        "VALUES ('sess-1', 'user', 'a turn', ?)", (time.time() + 1,),
                    )
                await mgr._db.commit()
                await _teach(mgr, 3_000)
                late = await mgr.harness_overhead(MIND, "codex", "codex-200k")
            await _close(mgr)
            return unnamed, late

    assert _run(scenario()) == (OVERHEAD["codex"], OVERHEAD["codex"])


def test_a_model_handover_trims_the_prompt_to_half_the_room_and_gives_the_rest_to_the_handover():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=80_000, window=1_000_000, threshold=300_000)
            await mgr._db.execute(
                "INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at) "
                "VALUES (?, 'ada', '123', 'sess-1', ?, ?)",
                (MIND, json.dumps({"carry_forward": "x" * 200_000 + " LAST WORDS"}), time.time()),
            )
            await mgr._db.commit()
            mind = Mind(percent=30)
            with mind.wired(), \
                    patch.object(bootstrap_loader, "_fetch_soul", return_value="<soul>me</soul>"), \
                    patch.object(bootstrap_loader, "_fetch_recent_memory", return_value=""):
                await mgr.switch_model("sess-1", "claude-sonnet-5")
            await _close(mgr)
            return mind

    mind = _run(scenario())
    room = (200_000 * 30 // 100 - OVERHEAD["claude"]) * 4
    blocks = mind.spawns()[-1]["system_prompt_blocks"]
    assert room // 2 - 1_000 <= len(blocks.encode()) <= room // 2
    assert blocks.rstrip().endswith("LAST WORDS\n</session-memory>".rstrip())
    budget = mind.sent("POST", "/handover")[0]["json"]["budget_bytes"]
    assert budget == room - len(blocks.encode())


def test_a_respawn_reopens_on_the_prompt_trimmed_as_the_switch_trimmed_it():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            await mgr._db.execute(
                "INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at) "
                "VALUES (?, 'ada', '123', 'sess-1', ?, ?)",
                (MIND, json.dumps({"carry_forward": "x" * 200_000 + " LAST WORDS"}),
                 time.time() + 1),
            )
            await mgr._db.commit()
            mind = _codex_mind()
            with mind.wired(), \
                    patch.object(bootstrap_loader, "_fetch_soul", return_value="<soul>me</soul>"), \
                    patch.object(bootstrap_loader, "_fetch_recent_memory", return_value=""):
                await mgr.switch_harness("sess-1", "codex", "codex-40k")
                switched = mind.spawns()[-1]["system_prompt_blocks"]
                mgr._procs.pop("sess-1")
                [e async for e in mgr.send_message("sess-1", "hello")]
            await _close(mgr)
            return switched, mind.spawns()[-1]

    switched, respawned = _run(scenario())
    assert respawned["opening_turn"]
    assert len(switched.encode()) < 200_000
    assert respawned["system_prompt_blocks"] == switched


def test_recent_memory_gives_way_once_session_memory_has_nothing_left():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            recent = "<recent-memory>\n" + "".join(f"- row {i}\n" for i in range(2_000)) + "</recent-memory>"
            with patch.object(bootstrap_loader, "_fetch_soul", return_value="<soul>me</soul>"), \
                    patch.object(bootstrap_loader, "_fetch_recent_memory", return_value=recent):
                blocks = await bootstrap_loader.compose_prompt_blocks(
                    mind_id=MIND, mind_name="ada", client_ref="123", db=mgr._db,
                    max_bytes=2_000,
                )
            await _close(mgr)
            return blocks

    blocks = _run(scenario())
    assert len(blocks.encode()) <= 2_000
    assert "- row 0\n" in blocks and "- row 1999" not in blocks
    assert blocks.startswith("<soul>me</soul>")


async def _learned(mgr, model: str, tokens: int, *, surface: str = "process",
                   harness: str = "codex", mind_id: str = MIND) -> None:
    """An overhead already on record, as earlier conversations taught it."""
    await mgr._db.execute(
        "INSERT INTO context_overhead (mind_id, harness, model, surface, tokens, observed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)", (mind_id, harness, model, surface, tokens, time.time()),
    )
    await mgr._db.commit()


def test_the_exact_model_answers_first_then_the_least_of_the_minds_other_models():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _learned(mgr, "codex-200k", 12_000)
            await _learned(mgr, "codex-other", 7_000)
            await _learned(mgr, "codex-40k", 30_000, mind_id="someone-else")
            answers = (
                await mgr.harness_overhead(MIND, "codex", "codex-200k"),
                await mgr.harness_overhead(MIND, "codex", "codex-unseen"),
                await mgr.harness_overhead("someone-else", "codex", "codex-200k"),
                await mgr.harness_overhead("nobody", "codex", "codex-200k"),
                await mgr.harness_overhead(MIND, "hermes", "gpt-6-astra"),
            )
            await _close(mgr)
            return answers

    assert _run(scenario()) == (
        12_000, 7_000, 30_000, OVERHEAD["codex"], max(OVERHEAD.values()))


def test_overhead_is_learned_under_the_rows_model_name_whatever_the_report_calls_it():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                await _teach(mgr, 12_345, model="codex-200k-20261001")
            learned = await mgr.harness_overhead(MIND, "codex", "codex-200k")
            await _close(mgr)
            return learned

    assert _run(scenario()) == 12_345


@pytest.mark.parametrize("surface,teaches", [
    ("telegram", True), ("terminal", False), (None, False), ("local", False),
])
def test_only_the_surface_that_opened_the_conversation_teaches(surface, teaches):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                await _teach(mgr, 12_345, surface=surface)
            learned = await mgr.harness_overhead(MIND, "codex", "codex-200k")
            await _close(mgr)
            return learned

    assert _run(scenario()) == (12_345 if teaches else OVERHEAD["codex"])


def test_a_count_naming_no_conversation_leaves_the_opening_for_the_one_that_does():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                await _teach(mgr, 3_000, named=False)
                await _teach(mgr, 12_345)
            learned = await mgr.harness_overhead(MIND, "codex", "codex-200k")
            await _close(mgr)
            return learned

    assert _run(scenario()) == 12_345


def test_the_first_turn_on_record_still_teaches_and_turns_before_the_switch_do_not_count():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            for _ in range(3):
                await mgr._db.execute(
                    "INSERT INTO session_turns (session_id, role, content, created_at) "
                    "VALUES ('sess-1', 'user', 'before the switch', ?)", (time.time() - 60,),
                )
            await mgr._db.commit()
            mind = _codex_mind()
            with mind.wired(), _big_soul():
                await mgr.switch_harness("sess-1", "codex", "codex-200k")
                await mgr._db.execute(
                    "INSERT INTO session_turns (session_id, role, content, created_at) "
                    "VALUES ('sess-1', 'user', 'the first turn', ?)", (time.time() + 1,),
                )
                await mgr._db.commit()
                await _teach(mgr, 12_345)
            learned = await mgr.harness_overhead(MIND, "codex", "codex-200k")
            await _close(mgr)
            return learned

    assert _run(scenario()) == 12_345


@pytest.mark.parametrize("threshold,refused", [(9_253, True), (9_300, False)])
def test_a_switch_is_refused_only_below_the_least_handover_worth_sending(threshold, refused):
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            mind = FakeMind()
            mind.models = {**mind.models, "codex": [
                {"name": "codex-edge", "context_window": threshold * 2,
                 "rotation_threshold_percent": 50, "effort_levels": []}]}
            with mind.wired(), _soul():
                try:
                    await mgr.switch_harness("sess-1", "codex", "codex-edge")
                    outcome = False
                except SwitchWontFit:
                    outcome = True
            await _close(mgr)
            return outcome, mind

    outcome, mind = _run(scenario())
    assert outcome is refused
    if not refused:
        assert mind.sent("POST", "/handover")[0]["json"]["budget_bytes"] == (
            (threshold - OVERHEAD["codex"]) * 4 - len("SOUL-BLOCKS"))


def test_a_model_of_unknown_threshold_trims_the_prompt_to_half_its_room():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            await mgr._db.execute(
                "INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at) "
                "VALUES (?, 'ada', '123', 'sess-1', ?, ?)",
                (MIND, json.dumps({"carry_forward": "x" * 200_000 + " LAST WORDS"}),
                 time.time() + 1),
            )
            await mgr._db.commit()
            mind = FakeMind()
            with mind.wired(), \
                    patch.object(bootstrap_loader, "_fetch_soul", return_value="<soul>me</soul>"), \
                    patch.object(bootstrap_loader, "_fetch_recent_memory", return_value=""):
                await mgr.switch_harness("sess-1", "codex", "gpt-5.6-terra")
            await _close(mgr)
            return mind

    mind = _run(scenario())
    room = (40_000 // 2 - OVERHEAD["codex"]) * 4
    blocks = mind.spawns()[-1]["system_prompt_blocks"]
    assert room // 2 - 1_000 <= len(blocks.encode()) <= room // 2
    assert mind.sent("POST", "/handover")[0]["json"]["budget_bytes"] == room - len(blocks.encode())


def test_a_resent_message_is_part_of_the_room_and_of_the_opening():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, model="claude-sonnet-5")
            await _measured(mgr, tokens=50_000, window=200_000, threshold=60_000)
            mind = Mind(turns=[[OVERFLOW], [DONE]])
            content = "ship it " * 2_000
            await _turn(mgr, mind, content)
            row = await mgr._get_row("sess-1")
            await _close(mgr)
            return row, mind, content

    row, mind, content = _run(scenario())
    spawned = [s for s in mind.spawns() if s["resume_sid"] == row["claude_sid"]][0]
    handover = spawned["opening_turn"]
    note = handover[len("HANDOVER: the old conversation\n\n"):] if handover.startswith(
        "HANDOVER: the old conversation") else handover
    message = f"{note}\n\n{content}" if note else content
    room = (60_000 - OVERHEAD["claude"]) * 4
    assert mind.sent("POST", "/handover")[0]["json"]["budget_bytes"] == (
        room - len(spawned["system_prompt_blocks"].encode()) - len(message.encode()))
    assert row["context_opening_tokens"] == (
        len(spawned["system_prompt_blocks"]) + len(handover) + len(content)) // 4


def test_a_rotation_seed_leaves_room_for_the_overhead_of_the_surface_it_opens_in():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, harness="codex", model="codex-200k")
            await _measured(mgr, tokens=1_000, window=40_000, threshold=20_000)
            await _learned(mgr, "codex-200k", 12_000, surface="pane")
            await _learned(mgr, "codex-200k", 5_000, surface="process")
            budgets = (await mgr._rotation_seed_budget("sess-1", "pane"),
                       await mgr._rotation_seed_budget("sess-1", "process"))
            await _close(mgr)
            return budgets

    assert _run(scenario()) == ((20_000 - 12_000) * 4, (20_000 - 5_000) * 4)


def test_session_memory_gives_way_before_recent_memory():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await mgr._db.execute(
                "INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at) "
                "VALUES (?, 'ada', '123', 'sess-1', ?, ?)",
                (MIND, json.dumps({"carry_forward": "y" * 5_000}), time.time()),
            )
            await mgr._db.commit()
            recent = "<recent-memory>\n" + "".join(f"- row {i}\n" for i in range(100)) + "</recent-memory>"
            with patch.object(bootstrap_loader, "_fetch_soul", return_value="<soul>me</soul>"), \
                    patch.object(bootstrap_loader, "_fetch_recent_memory", return_value=recent):
                blocks = await bootstrap_loader.compose_prompt_blocks(
                    mind_id=MIND, mind_name="ada", client_ref="123", db=mgr._db,
                    max_bytes=len(recent) + 500,
                )
            await _close(mgr)
            return blocks, recent

    blocks, recent = _run(scenario())
    assert recent in blocks
    assert "y" * 5_000 not in blocks


def test_a_refused_switch_reaches_a_chat_surface_as_its_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))
    monkeypatch.setenv("SESSIONS_DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", GRILL_ADMIN)
    import importlib
    from comms import broker
    from comms import server as server_module
    importlib.reload(server_module)
    with TestClient(server_module.app, headers={"Authorization": f"Bearer {GRILL_ADMIN}"}) as client:
        mgr = server_module.session_mgr
        _run(broker.register_mind(
            mgr.broker_db, mind_id=MIND, name="ada", gateway_url="http://mind.test:8420",
            model="claude-opus-5", harness="claude_cli", owner="daniel",
        ))
        _run(_seed(mgr))
        mind = FakeMind()
        with mind.wired(), _soul():
            answer = client.post("/command", json={
                "content": "/harness codex gpt-5.6-nano", "owner_type": "telegram",
                "client_ref": "123", "owner_ref": "123", "mind_id": MIND,
            })
    assert answer.status_code == 200
    assert "gpt-5.6-nano can't take this conversation" in answer.json()["error"]


def test_a_switch_that_fails_to_start_restores_the_old_harness_on_its_own_whole_prompt():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _with_history(mgr)
            await mgr._db.execute(
                "INSERT INTO session_memory (mind_id, mind_name, client_ref, session_id, body, created_at) "
                "VALUES (?, 'ada', '123', 'sess-1', ?, ?)",
                (MIND, json.dumps({"carry_forward": "x" * 200_000 + " LAST WORDS"}),
                 time.time() + 1),
            )
            await mgr._db.commit()
            mind = _codex_mind()
            mind.failing_spawns = {"codex"}
            with mind.wired(), \
                    patch.object(bootstrap_loader, "_fetch_soul", return_value="<soul>me</soul>"), \
                    patch.object(bootstrap_loader, "_fetch_recent_memory", return_value=""):
                with pytest.raises(ValueError):
                    await mgr.switch_harness("sess-1", "codex", "codex-40k")
            await _close(mgr)
            return mind

    restored = _run(scenario()).spawns()[-1]
    assert restored["harness"] == "claude"
    assert "x" * 200_000 + " LAST WORDS" in restored["system_prompt_blocks"]


def test_an_overflow_the_model_has_no_room_to_take_keeps_the_message_and_says_why():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr, model="bilby-32k")
            await _measured(mgr, tokens=20_000, window=32_000, threshold=16_000)
            mind = Mind(turns=[[OVERFLOW]])
            events = await _turn(mgr, mind, "ship it")
            ledger = await mgr.get_session_turns("sess-1")
            await _close(mgr)
            return events, ledger

    events, ledger = _run(scenario())
    assert "ship it" in [t["content"] for t in ledger]
    said = _text(events)
    assert "bilby-32k can't take this conversation" in said
    assert "It stays where it is" not in said



def test_a_pane_the_mind_refuses_to_rotate_is_left_running():
    """Only a mind saying it holds no live pane means there is none. A
    refusal is about a pane that may be running, and a model handover that
    read it as "no pane" would end that pane to start a process instead."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=150_000, window=1_000_000, threshold=500_000)
            await mgr._db.execute("UPDATE sessions SET terminal_held = 1 WHERE id = 'sess-1'")
            await mgr._db.commit()
            before = await mgr._get_row("sess-1")
            mind = Mind()
            mind.rotate_status = 500
            with pytest.raises(ValueError):
                await _switch(mgr, mind, "claude-sonnet-5")
            after = await mgr._get_row("sess-1")
            await _close(mgr)
            return before, after, mind

    before, after, mind = _run(scenario())
    assert mind.kills("sess-1") == []
    assert mind.spawns() == []
    assert (after["claude_sid"], after["model"]) == (before["claude_sid"], before["model"])


@pytest.mark.parametrize("answer", [(200, []), (200, {}), (200, {"rotated": "false"})])
def test_only_a_mind_saying_it_holds_no_pane_reads_as_no_pane(answer):
    """An answer that says nothing about rotating leaves the pane alone rather
    than ending it to start a process in its place."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=150_000, window=1_000_000, threshold=500_000)
            await mgr._db.execute("UPDATE sessions SET terminal_held = 1 WHERE id = 'sess-1'")
            await mgr._db.commit()
            mind = Mind()
            mind.rotate_status, mind.rotate_body = answer
            with pytest.raises(ValueError):
                await _switch(mgr, mind, "claude-sonnet-5")
            after = await mgr._get_row("sess-1")
            await _close(mgr)
            return after, mind

    after, mind = _run(scenario())
    assert mind.kills("sess-1") == []
    assert mind.spawns() == []
    assert after["claude_sid"] == "conv-1"
