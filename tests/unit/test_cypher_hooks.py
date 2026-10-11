"""Cypher's own hooks: the dsh log her Claude-dialect hooks have to read.

Her hooks are copies of the operator mind's, which read a Claude Code
transcript. Her harness writes a different schema into a compressed,
multi-frame container, so one module translates at the boundary rather than
five hooks each learning a second schema. These tests drive that module and
the hook functions whose answers change because of it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "minds" / "cypher" / "hooks"

# Her hooks are per-install and untracked, like every deployment mind's folder:
# a fresh clone has none to test.
pytestmark = pytest.mark.skipif(
    not HOOKS_DIR.is_dir(), reason="Cypher's hooks live only on the host that runs her"
)


def _load(name: str):
    """Import one of Cypher's hook modules by path.

    They are not a package — the harness runs each as a script — so a test
    reaching them imports by location, the way the harness does.
    """
    sys.path.insert(0, str(HOOKS_DIR))
    try:
        spec = importlib.util.spec_from_file_location(f"cypher_{name}", HOOKS_DIR / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(HOOKS_DIR))


def _dsh_log(path: Path, events: list[dict], header: dict | None = None) -> Path:
    """Write an uncompressed dsh session log the translator can read."""
    lines = [json.dumps({"type": "session", "version": 0, "id": "s-1",
                         "cwd": "/mnt/dev", "delegationDepth": 0, **(header or {})})]
    lines += [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n")
    return path


def _assistant(seq: int, usage: dict, text: str = "ok", model: str = "claude-haiku-5.5") -> dict:
    return {
        "type": "assistant/message", "seq": seq,
        "data": {
            "turn": 1, "step": seq,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "source": {"kind": "model", "provider": "hive-proxy", "model": model},
            },
            "usage": usage,
        },
    }


class TestTranslatedUsage:
    def test_the_context_figure_sums_cached_input_alongside_uncached(self, tmp_path):
        """dsh reports uncached input, cache reads and cache writes separately.

        Summing only `inputTokens` and `outputTokens` reads a conversation
        holding a hundred thousand tokens as holding a few thousand.
        """
        dsh = _load("dsh_transcript")
        log = _dsh_log(tmp_path / "session.jsonl", [
            _assistant(1, {"inputTokens": 500, "outputTokens": 10, "cacheReadTokens": 40_000}),
            _assistant(2, {"inputTokens": 1_200, "outputTokens": 300,
                           "cacheReadTokens": 90_000, "cacheWriteTokens": 2_000}),
        ])

        entries = dsh.translate(dsh.read_log_lines(log))
        usage = [e["message"]["usage"] for e in entries if e["type"] == "assistant"][-1]

        assert sum(usage.values()) == 1_200 + 300 + 90_000 + 2_000

    def test_the_rotation_hook_reads_that_figure_off_her_log(self, tmp_path):
        """The hook's own measurement, over a translated log, not a fixture."""
        dsh = _load("dsh_transcript")
        check = _load("rotation_check")
        log = _dsh_log(tmp_path / "session.jsonl", [
            _assistant(1, {"inputTokens": 1_000, "outputTokens": 500,
                           "cacheReadTokens": 103_000, "cacheWriteTokens": 100}),
        ])

        measured = check._measure({}, dsh.materialize(log), chars_per_token=4)

        assert measured["tokens"] == 1_000 + 500 + 103_000 + 100

    def test_a_log_reporting_no_usage_refuses_rather_than_estimating_from_size(self, tmp_path):
        """A size-derived number is a confident wrong answer.

        On a compressed log, bytes-over-four understates by the compression
        ratio and the rotation never fires; on a long plaintext one it
        overstates and every turn rotates. Both read as working.
        """
        check = _load("rotation_check")
        plain = tmp_path / "session.jsonl"
        plain.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                                      "content": [{"type": "text", "text": "x" * 5_000}]}}) + "\n")

        assert check._measure({}, plain, chars_per_token=4)["tokens"] is None


class TestTurnText:
    def test_her_turn_text_survives_translation(self, tmp_path):
        """The carry-forward is composed from turn text, not from token counts.

        A rotation that measures correctly and carries nothing hands the
        successor a summary of an empty conversation.
        """
        dsh = _load("dsh_transcript")
        check = _load("rotation_check")
        log = _dsh_log(tmp_path / "session.jsonl", [
            {"type": "user/message", "seq": 1,
             "data": {"content": [{"type": "text", "text": "build the thing"}]}},
            _assistant(2, {"inputTokens": 10, "outputTokens": 5}, text="building it"),
        ])

        turns = check._read_turns(dsh.materialize(log))

        assert turns == [{"role": "user", "text": "build the thing"},
                         {"role": "assistant", "text": "building it"}]


class TestDelegateAttribution:
    def test_a_delegate_s_log_is_reported_as_a_delegate(self, tmp_path):
        dsh = _load("dsh_transcript")
        log = _dsh_log(tmp_path / "session.jsonl",
                       [_assistant(1, {"inputTokens": 1, "outputTokens": 1})],
                       header={"origin": "subagent", "parentSession": "s-parent", "delegationDepth": 1})

        assert dsh.is_delegate_log(log) is True

    def test_her_own_log_is_not(self, tmp_path):
        dsh = _load("dsh_transcript")
        log = _dsh_log(tmp_path / "session.jsonl",
                       [_assistant(1, {"inputTokens": 1, "outputTokens": 1})])

        assert dsh.is_delegate_log(log) is False


class TestBackgroundTasks:
    def test_a_job_winding_down_still_counts_as_live(self, tmp_path, monkeypatch):
        """`stopping` is a live process group, and the respawn kills it."""
        monkeypatch.setenv("AUTO_REMEMBER_LOG_DIR", str(tmp_path))
        state = _load("rotation_state")
        state.record_background_tasks("sid-1", [
            {"type": "bash", "description": "pytest -q", "status": "stopping"},
            {"type": "bash", "description": "done one", "status": "completed"},
        ])

        live = state.running_background_tasks("sid-1")

        assert [t["description"] for t in live] == ["pytest -q"]

    def test_state_lands_where_her_container_can_write(self, tmp_path, monkeypatch):
        """Her project root is mounted read-only; only her own tree is writable."""
        monkeypatch.setenv("AUTO_REMEMBER_LOG_DIR", str(tmp_path / "mine"))
        monkeypatch.setenv("HIVE_PROJECT_DIR", str(tmp_path / "readonly"))
        state = _load("rotation_state")

        assert state.state_dir() == tmp_path / "mine"

    def test_a_failed_state_write_is_reported_as_failed(self, tmp_path, monkeypatch):
        """Staging that cannot persist must not report success.

        The composition that follows takes minutes and has nothing to promote,
        and the fire hook finds no marker — with the log saying it staged.
        """
        monkeypatch.setenv("AUTO_REMEMBER_LOG_DIR", str(tmp_path / "nope"))
        state = _load("rotation_state")
        (tmp_path / "nope").write_text("not a directory")

        assert state.record_background_tasks("sid-1", []) is False


class TestTypedPrompts:
    @pytest.mark.parametrize("sources", [["goal"], ["plugin"], ["user", "plugin"]])
    def test_a_prompt_her_harness_injected_does_not_fire_a_rotation(self, sources):
        """Her untyped prompts carry no envelope tag, only a source.

        A goal round's re-entry, a hook's own injected context and a finished
        job's notice are all plain prose. Firing on one respawns her pane and
        kills the build it was reporting on.
        """
        fire = _load("rotation_fire")

        assert fire._is_typed("read BRIEF.md and build it", {"prompt_sources": sources}) is False

    def test_a_prompt_she_was_actually_sent_does(self):
        fire = _load("rotation_fire")

        assert fire._is_typed("carry on", {"prompt_sources": ["user"]}) is True



# ---------------------------------------------------------------------------
# Measuring a turn, sizing its threshold, and telling the gateway
# ---------------------------------------------------------------------------

def _turn_start(seq: int) -> dict:
    return {"type": "turn/start", "seq": seq, "data": {"turn": seq}}


def _measured(seq: int, model: str, tokens: int, kind: str, window: int = 0) -> dict:
    return {"type": "context/measured", "seq": seq,
            "data": {"model": model, "tokens": tokens, "kind": kind, "window": window}}


def _user(seq: int, text: str, extra: list | None = None) -> dict:
    return {"type": "user/message", "seq": seq,
            "data": {"content": [{"type": "text", "text": text}, *(extra or [])]}}


def _tool_result(seq: int, text: str) -> dict:
    return {"type": "tool/result", "seq": seq, "data": {"message": {
        "source": {"kind": "tool", "callId": "c1"},
        "content": [{"type": "tool-result", "toolCallId": "c1",
                     "content": [{"type": "text", "text": text}]}]}}}


class _Gateway:
    """The proxy's model listing and the gateway's context route, at urlopen."""

    def __init__(self, windows: dict[str, int]):
        self.windows = windows
        self.reports: list[dict] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        if "/v1/models" in url:
            body = {"data": [{"id": m, "context_window": w} for m, w in self.windows.items()]}
        elif url.endswith("/sessions/context"):
            self.reports.append(json.loads(req.data))
            body = {"ok": True}
        else:
            raise AssertionError(f"unexpected request {url}")
        payload = json.dumps(body).encode()

        class _Resp:
            status = 200

            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp()


@pytest.fixture
def mind(tmp_path, monkeypatch):
    """Her runtime.yaml, her notify tool and a gateway, all under tmp_path,
    and the threshold the mind server spawned her with.

    The notify tool is a script that writes down what it was asked to send:
    the process boundary is where the hook stops being ours.
    """
    check = _load("rotation_check")
    root = tmp_path / "app"
    (root / "minds" / "cypher").mkdir(parents=True)
    (root / "minds" / "cypher" / "runtime.yaml").write_text(
        "name: cypher\n")
    tool = root / "tools" / "stateless" / "notify"
    tool.mkdir(parents=True)
    sent = tmp_path / "sent.jsonl"
    (tool / "notify.py").write_text(
        "import json, sys\n"
        f"open({str(sent)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    monkeypatch.setenv("HIVE_PROJECT_DIR", str(root))
    monkeypatch.setenv("MIND_NAME", "cypher")
    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.test")
    monkeypatch.setenv("MIND_PROXY_KEY", "hmp-cypher")
    monkeypatch.setenv("OWNER_TYPE", "telegram")
    monkeypatch.setenv("CLIENT_REF", "chat-1")
    monkeypatch.setenv("HIVE_HARNESS", "dsh")
    monkeypatch.setenv("HIVE_ROTATION_THRESHOLD_TOKENS", "80000")
    for var in ("HIVE_SURFACE", "HIVE_GOAL_RUN", "HIVEMIND_GROUP_SESSION", "ROTATION_SPAWN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(check, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(check, "_SPAWN_MODEL_ARG", "")
    gateway = _Gateway({})
    monkeypatch.setattr(check.urllib.request, "urlopen", gateway)

    def notices() -> list[list[str]]:
        if not sent.is_file():
            return []
        return [json.loads(line) for line in sent.read_text().splitlines()]

    return check, gateway, notices


def _stop(log: Path, **extra) -> dict:
    return {"session_id": "conv-1", "transcript_path": str(log), **extra}


class TestMeasurement:
    def test_a_model_reporting_zeros_is_measured_by_the_harness_estimate_not_an_older_count(
        self, tmp_path,
    ):
        """[gpt-oss counted 50k, luna 0/0, luna 0/0]. The zeros are not a size,
        and neither is the 50k: it is the conversation as it was two turns and
        another model ago."""
        dsh = _load("dsh_transcript")
        check = _load("rotation_check")
        log = _dsh_log(tmp_path / "session.jsonl", [
            _turn_start(1),
            _assistant(2, {"inputTokens": 49_000, "outputTokens": 1_000}, model="gpt-oss"),
            _measured(3, "gpt-oss", 50_000, "usage"),
            _turn_start(4),
            _assistant(5, {"inputTokens": 0, "outputTokens": 0}, model="luna"),
            _measured(6, "luna", 61_000, "estimated"),
            _turn_start(7),
            _assistant(8, {"inputTokens": 0, "outputTokens": 0}, model="luna"),
            _measured(9, "luna", 64_000, "estimated"),
        ])

        measured = check._measure({}, dsh.materialize(log), chars_per_token=4)

        assert measured == {"tokens": 64_000, "kind": "estimated", "model": "luna"}

    def test_a_failed_turn_counts_what_it_was_refused_over(self, mind, tmp_path):
        """[counted 150k, a message, a 60k tool result, failure] on a 200k
        window. The failed call reported nothing, but what it read is in the
        conversation — rotating on the 150k alone would leave it overflowing."""
        check, gateway, _ = mind
        gateway.windows["qwen"] = 200_000
        dsh = _load("dsh_transcript")
        log = _dsh_log(tmp_path / "session.jsonl", [
            _turn_start(1),
            _assistant(2, {"inputTokens": 149_000, "outputTokens": 1_000}, model="qwen"),
            _measured(3, "qwen", 150_000, "usage"),
            _turn_start(4),
            _user(5, "read the big file"),
            _tool_result(6, "x" * 240_000),
        ])
        assessed = check._assess(_stop(dsh.materialize(log), stop_reason="error",
                                       error_code="LLM_ERROR",
                                       context={"model": "qwen", "kind": "none"}))

        assert assessed["measured"]["tokens"] >= 150_000 + 60_000
        assert assessed["rotate"] is True

    def test_a_turn_refused_for_size_is_left_to_the_overflow_path(self, mind, tmp_path):
        """The overflow path rotates it and re-sends the message; a rotation
        composed from here as well would race it."""
        check, gateway, _ = mind
        gateway.windows["qwen"] = 200_000
        log = _dsh_log(tmp_path / "session.jsonl", [_turn_start(1)])

        assessed = check._assess(_stop(
            log, stop_reason="error", error_code="CONTEXT_WINDOW_EXCEEDED",
            context={"model": "qwen", "tokens": 199_000, "kind": "estimated"}))

        assert assessed["rotate"] is False

    def test_a_turn_with_no_size_tells_daniel_once_per_model_until_a_count_arrives(
        self, mind, tmp_path,
    ):
        """A model that reports nothing reports nothing every turn; a notice per
        turn is noise. A real count re-arms it, and so does another model."""
        check, gateway, notices = mind
        gateway.windows.update(luna=128_000, qwen=128_000)
        dsh = _load("dsh_transcript")
        # An earlier turn was counted; a count from before this turn is not one.
        log = _dsh_log(tmp_path / "session.jsonl", [
            _turn_start(1),
            _assistant(2, {"inputTokens": 40_000, "outputTokens": 100}, model="luna"),
            _measured(3, "luna", 40_100, "usage"),
            _turn_start(4),
        ])
        silent = {"model": "luna", "kind": "none"}

        check._assess(_stop(dsh.materialize(log), context=silent))
        check._assess(_stop(dsh.materialize(log), context=silent))
        after_two = len(notices())
        check._assess(_stop(dsh.materialize(log), context={"model": "qwen", "kind": "none"}))
        after_switch = len(notices())
        check._assess(_stop(dsh.materialize(log),
                            context={"model": "luna", "tokens": 9_000, "kind": "usage"}))
        check._assess(_stop(dsh.materialize(log), context=silent))

        assert (after_two, after_switch, len(notices())) == (1, 2, 3)


class TestThreshold:
    def test_the_threshold_is_the_one_the_process_was_spawned_with(self, mind, monkeypatch):
        """The share belongs to the model, so the mind server reads it off the
        proxy's row and hands over the product. On any harness but claude it
        stands uncapped: no other harness pins a window, so nothing holds it
        to a 200k model's figure."""
        check, gateway, _ = mind
        gateway.windows["gpt-5.6-terra"] = 400_000
        monkeypatch.setenv("HIVE_ROTATION_THRESHOLD_TOKENS", str(400_000 * 75 // 100))

        threshold = check._threshold_for_model(check._cfg(), "gpt-5.6-terra", "")

        assert threshold == 400_000 * 75 // 100

    def test_a_conversation_switched_to_another_model_is_held_to_that_models_window(
        self, mind, tmp_path, monkeypatch,
    ):
        """The model is taken from the turn's own entry, not from the spawn: a
        switch inside the conversation is the model the next turn ran on."""
        check, gateway, _ = mind
        gateway.windows.update(big=1_000_000, small=128_000)
        # The process was spawned on the model it has since left.
        monkeypatch.setattr(check, "_SPAWN_MODEL_ARG", "big")
        monkeypatch.setenv("HIVE_MODEL_CONTEXT_WINDOW", "1000000")
        dsh = _load("dsh_transcript")
        log = _dsh_log(tmp_path / "session.jsonl", [
            _turn_start(1),
            _assistant(2, {"inputTokens": 90_000, "outputTokens": 10}, model="big"),
            _turn_start(3),
            _assistant(4, {"inputTokens": 100_000, "outputTokens": 10}, model="small"),
        ])

        assessed = check._assess(_stop(dsh.materialize(log)))

        assert assessed["window"] == 128_000


class TestReport:
    def test_each_turn_reports_its_size_against_the_threshold_and_window_it_was_held_to(
        self, mind, tmp_path,
    ):
        """What the dashboard draws is what this check decided on, for the
        conversation it measured — so a report that lands after a rotation can
        be told apart, and a picture in the history is known before a switch."""
        check, gateway, _ = mind
        gateway.windows["qwen"] = 131_072
        dsh = _load("dsh_transcript")
        log = _dsh_log(tmp_path / "session.jsonl", [
            _turn_start(1),
            _user(2, "what is this", extra=[{"type": "image", "attachment": {"id": "a1"}}]),
            _assistant(3, {"inputTokens": 20_000, "outputTokens": 500}, model="qwen"),
            _measured(4, "qwen", 20_500, "usage", 131_072),
        ])

        assessed = check._assess(_stop(dsh.materialize(log)))

        report = gateway.reports[-1]
        assert {k: report[k] for k in ("tokens", "kind", "model", "conversation_id",
                                       "has_images")} == {
            "tokens": 20_500, "kind": "usage", "model": "qwen",
            "conversation_id": "conv-1", "has_images": True}
        assert (report["threshold"], report["window"]) == (
            assessed["threshold"], assessed["window"])


class TestGoalRound:
    def test_a_goal_round_over_its_threshold_asks_dsh_to_end_the_run(
        self, mind, tmp_path, monkeypatch,
    ):
        check, gateway, _ = mind
        gateway.windows["qwen"] = 100_000
        log = _dsh_log(tmp_path / "session.jsonl", [_turn_start(1)])
        over = _stop(log, context={"model": "qwen", "tokens": 90_000, "kind": "usage"})

        outside = check._hook_output(over, check._assess(over))
        monkeypatch.setenv("HIVE_GOAL_RUN", "1")
        inside = check._hook_output(over, check._assess(over))

        assert (outside, inside) == ({}, {"rotate": True})


class TestUnknownWindow:
    def test_a_model_with_no_listed_window_is_reported_without_one_and_not_rotated(
        self, mind, tmp_path,
    ):
        """Zero would read as a window with no room in it; a guessed one rotates
        a conversation at room it may not have."""
        check, gateway, _ = mind
        log = _dsh_log(tmp_path / "session.jsonl", [_turn_start(1)])

        assessed = check._assess(_stop(
            log, context={"model": "unlisted", "tokens": 900_000, "kind": "usage"}))

        report = gateway.reports[-1]
        assert assessed is None
        assert report["tokens"] == 900_000
        assert "window" not in report and "threshold" not in report

    def test_a_model_spawned_with_no_threshold_is_reported_without_one_and_not_rotated(
        self, mind, tmp_path, monkeypatch,
    ):
        """The proxy listed a window but declared no share of it: the window is
        known and reported, and no percentage stands in for the missing one."""
        check, gateway, _ = mind
        gateway.windows["qwen"] = 131_072
        monkeypatch.delenv("HIVE_ROTATION_THRESHOLD_TOKENS")
        log = _dsh_log(tmp_path / "session.jsonl", [_turn_start(1)])

        assessed = check._assess(_stop(
            log, context={"model": "qwen", "tokens": 130_000, "kind": "usage"}))

        report = gateway.reports[-1]
        assert assessed is None
        assert report["window"] == 131_072
        assert "threshold" not in report

    def test_a_claude_model_on_the_claude_harness_falls_back_to_its_known_ceiling(
        self, mind, monkeypatch,
    ):
        """Its ceiling without the [1m] pin is known, so it alone is not left
        without a threshold."""
        check, _, _ = mind
        monkeypatch.setenv("HIVE_HARNESS", "claude")
        cfg = check._cfg()

        assert check._threshold_for_model(cfg, "claude-opus-5", "") == cfg["THRESHOLD_NO_1M"]


class TestFailedTurns:
    def test_a_failed_terminal_turn_is_not_written_to_the_turn_ledger(
        self, mind, tmp_path, monkeypatch,
    ):
        """It has no answer to ledger, and the overflow path re-sends its
        message — a ledgered copy would be read back as a turn that happened."""
        check, gateway, _ = mind
        gateway.windows["qwen"] = 200_000
        monkeypatch.setenv("HIVE_SURFACE", "terminal")
        recorded: list[str] = []
        original = gateway.__call__

        def route(req, timeout=None):
            if req.full_url.endswith("/sessions/record-turn"):
                recorded.append(json.loads(req.data)["role"])
                return original(_Req(req.full_url.replace("record-turn", "context"),
                                     b"{}"), timeout)
            return original(req, timeout)

        monkeypatch.setattr(check.urllib.request, "urlopen", route)
        dsh = _load("dsh_transcript")
        log = dsh.materialize(_dsh_log(tmp_path / "session.jsonl", [
            _turn_start(1), _user(2, "hello"),
            _assistant(3, {"inputTokens": 1_000, "outputTokens": 10}, text="hi", model="qwen"),
        ]))

        check._assess(_stop(log, stop_reason="error", error_code="LLM_ERROR"))
        failed = list(recorded)
        check._assess(_stop(log))

        assert (failed, recorded) == ([], ["user", "assistant"])


class _Req:
    def __init__(self, url, data):
        self.full_url, self.data = url, data


class TestGoalRoundAnswer:
    def test_a_goal_round_answered_with_rotate_composes_nothing(
        self, mind, tmp_path, monkeypatch, capsys,
    ):
        """dsh ends the run and the gateway rotates it; minutes of Ollama for a
        second, competing rotation from here would race it."""
        check, gateway, _ = mind
        gateway.windows["qwen"] = 100_000
        monkeypatch.setenv("HIVE_GOAL_RUN", "1")
        log = _dsh_log(tmp_path / "session.jsonl", [_turn_start(1)])
        event = _stop(log, context={"model": "qwen", "tokens": 90_000, "kind": "usage"})
        monkeypatch.setattr(check.sys, "stdin", io.StringIO(json.dumps(event)))
        forks: list[bool] = []

        def fork():
            forks.append(True)
            return 1  # the parent's side: the child would compose

        monkeypatch.setattr(check.os, "fork", fork)

        check.main()

        assert json.loads(capsys.readouterr().out) == {"rotate": True}
        assert forks == []


class TestArmStartedAt:
    def test_an_arm_says_when_the_conversation_it_measured_began(
        self, mind, tmp_path, monkeypatch,
    ):
        """The gateway refuses an arm for a conversation that began before the
        session's last rotation: the hook measured the one it replaced."""
        check, _, _ = mind
        dsh = _load("dsh_transcript")
        first = {"type": "turn/start", "seq": 1, "time": 1_791_042_190_658, "data": {}}
        log = dsh.materialize(_dsh_log(tmp_path / "session.jsonl", [first, _turn_start(2)]))
        armed: list[dict] = []

        def urlopen(req, timeout=None):
            armed.append(json.loads(req.data))

            class _Resp:
                def read(self):
                    return b'{"ok": true, "session_id": "row-1"}'

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return _Resp()

        monkeypatch.setattr(check.urllib.request, "urlopen", urlopen)

        check._comms_arm_rotation(check._cfg(), client_type="telegram", client_ref="chat-1",
                                  claude_sid="conv-1",
                                  started_at=check._transcript_started_at(log))

        assert armed[0]["started_at"] == 1_791_042_190_658 / 1000
