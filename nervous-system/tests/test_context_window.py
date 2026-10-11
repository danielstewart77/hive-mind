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

import json
import tempfile
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from comms import bootstrap_loader
from comms.sessions import SessionManager
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


class Mind(FakeMind):
    """The fake mind, answering each turn from a queue and saying its percent."""

    def __init__(self, turns: list[list[dict]] | None = None, percent: int = PERCENT):
        super().__init__()
        self.models = MODELS
        self.turns = list(turns or [])
        self.percent = percent

    def _answer(self, method, url, params=None, json_body=None):
        if method == "GET" and url.endswith("/runtime/settings"):
            self.requests.append({"method": method, "path": "/runtime/settings",
                                  "params": {}, "json": None})
            return _Resp(200, {"settings": [
                {"key": "rotation_threshold_percent", "value": self.percent},
            ]})
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
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    from comms import server as server_module
    import importlib
    importlib.reload(server_module)
    with TestClient(server_module.app) as client:
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


def test_a_conversation_over_the_new_threshold_is_handed_over_not_resumed():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=100_001, window=1_000_000, threshold=500_000)
            mind = Mind()
            result = await _switch(mgr, mind, "claude-sonnet-5")
            old = await mgr._get_row("sess-1")
            new = await mgr._get_row(result["id"])
            await _close(mgr)
            return result, old, new, mind

    result, old, new, mind = _run(scenario())
    assert result["id"] != "sess-1"
    assert new["rotated_from"] == "sess-1"
    assert old["status"] == "closed"
    assert all(s["resume_sid"] != "conv-1" for s in mind.spawns()[1:])
    assert new["claude_sid"] != "conv-1"
    assert new["carry_forward"] and new["carry_forward_sid"] == new["claude_sid"]
    assert mind.spawns()[-1]["opening_turn"] == new["carry_forward"]
    assert new["model"] == "claude-sonnet-5"
    assert (new["context_window"], new["context_threshold"]) == (200_000, 200_000 * PERCENT // 100)
    assert new["context_tokens"] is None
    assert new["rotation_armed"] == 0
    assert "100,001" in result["warning"]


def test_an_unmeasured_conversation_that_has_had_turns_is_never_assumed_to_fit():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=None)
            mind = Mind()
            result = await _switch(mgr, mind, "claude-sonnet-5")
            await _close(mgr)
            return result, mind

    result, mind = _run(scenario())
    assert result["id"] != "sess-1"
    assert len(mind.sent("POST", "/handover")) == 1


def test_images_on_a_text_only_model_hand_over_even_when_the_size_fits():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=1_000)
            await mgr.report_context("sess-1", has_images=True, conversation_id="conv-1")
            mind = Mind()
            text_only = await _switch(mgr, mind, "text-only")
            await _close(mgr)
            return text_only

    assert _run(scenario())["id"] != "sess-1"


def test_images_on_a_model_whose_image_support_is_unknown_hand_over():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=1_000, has_images=1)
            unknown = await _switch(mgr, Mind(), "unknown-eyes")
            await _close(mgr)
            return unknown

    assert _run(scenario())["id"] != "sess-1"


def test_a_conversation_with_no_images_resumes_on_a_text_only_model():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=1_000, has_images=0)
            result = await _switch(mgr, Mind(), "text-only")
            await _close(mgr)
            return result

    assert _run(scenario())["id"] == "sess-1"


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


def test_a_handover_seed_leaves_room_for_the_soul_under_a_small_models_threshold():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=50_000, window=1_000_000, threshold=300_000)
            mind = Mind(percent=30)
            await _switch(mgr, mind, "bilby-32k")
            await _close(mgr)
            return mind

    mind = _run(scenario())
    budget = mind.sent("POST", "/handover")[0]["json"]["budget_bytes"]
    threshold = 32_000 * 30 // 100
    assert budget + len("SOUL-BLOCKS".encode()) <= threshold * 4


def test_a_chat_rotation_trims_its_carry_forward_to_the_models_threshold():
    """The composed successor prompt — soul, recent memory and the rotation
    summary — has to fit where the conversation will rotate next."""
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=1_000, window=32_000, threshold=8_000)
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
    assert len(blocks.encode()) <= 8_000 * 4
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


def test_an_overflowed_turn_rotates_and_sends_the_same_message_once_on_the_new_conversation():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=180_000)
            mind = Mind(turns=[TOOL_TURN, [DONE]])
            events = await _turn(mgr, mind, "ship it")
            successor = (await mgr.get_active_session("telegram", "123"))
            new = await mgr._get_row(successor["id"])
            old_ledger = await mgr.get_session_turns("sess-1")
            await _close(mgr)
            return events, new, old_ledger, mind

    events, new, old_ledger, mind = _run(scenario())
    assert new["id"] != "sess-1" and new["rotated_from"] == "sess-1"
    sent = mind.messages()
    assert [r["path"] for r in sent] == ["/sessions/sess-1/message",
                                         f"/sessions/{new['id']}/message"]
    assert sent[1]["json"]["content"].endswith("\nship it")
    assert [t["content"] for t in old_ledger] == ["an earlier turn"]
    opening = [s for s in mind.spawns() if s["session_id"] == new["id"]][0]["opening_turn"]
    assert 'Bash {"command": "make deploy"} (done)' in opening
    assert "/never" not in opening
    assert "ran out of room" in _text(events)
    assert events[-1] == DONE
    assert new["resend_content"] is None


def test_a_resent_message_that_overflows_again_is_reported_and_not_rotated_again():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await _measured(mgr, tokens=180_000)
            mind = Mind(turns=[[OVERFLOW], [OVERFLOW], [DONE]])
            events = await _turn(mgr, mind, "ship it")
            cur = await mgr._db.execute("SELECT COUNT(*) FROM sessions")
            rows = (await cur.fetchone())[0]
            await _close(mgr)
            return events, rows, mind

    events, rows, mind = _run(scenario())
    assert rows == 2
    assert len(mind.messages()) == 2
    assert events[-1]["is_error"] is True
    assert "again" in _text(events)


def test_a_resend_interrupted_by_a_restart_goes_out_once():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            mgr = await _manager(tmp)
            await _seed(mgr)
            await mgr._db.execute(
                "UPDATE sessions SET resend_content = 'ship it' WHERE id = 'sess-1'"
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
    assert [m["json"]["content"].endswith("\nship it") for m in mind.messages()] == [True]


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
            cur = await mgr._db.execute("SELECT id FROM sessions WHERE rotated_from = 'sess-1'")
            successors = [r[0] for r in await cur.fetchall()]
            await _close(mgr)
            return events, successors, mind

    events, successors, mind = _run(scenario())
    assert len(successors) == 1
    sent = mind.messages()
    assert [r["path"] for r in sent] == ["/sessions/sess-1/message",
                                         f"/sessions/{successors[0]}/message"]
    assert sent[0]["json"]["goal_rounds"] == 5
    assert sent[1]["json"]["goal_rounds"] == 3
    assert sent[1]["json"]["content"].endswith("\nbuild the widget")
    assert events[-1] == DONE


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
