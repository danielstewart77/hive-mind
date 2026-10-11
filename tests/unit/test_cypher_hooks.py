"""Cypher's own hooks: the dsh log her Claude-dialect hooks have to read.

Her hooks are copies of the operator mind's, which read a Claude Code
transcript. Her harness writes a different schema into a compressed,
multi-frame container, so one module translates at the boundary rather than
five hooks each learning a second schema. These tests drive that module and
the hook functions whose answers change because of it.
"""

from __future__ import annotations

import importlib.util
import os
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
    """Her runtime.yaml, her notify tool and a gateway, all under tmp_path.

    The notify tool is a script that writes down what it was asked to send:
    the process boundary is where the hook stops being ours.
    """
    check = _load("rotation_check")
    root = tmp_path / "app"
    (root / "minds" / "cypher").mkdir(parents=True)
    (root / "minds" / "cypher" / "runtime.yaml").write_text(
        "name: cypher\nrotation_threshold_percent: 80\n")
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
        log = _dsh_log(tmp_path / "session.jsonl", [_turn_start(1)])
        silent = {"model": "luna", "kind": "none"}

        check._assess(_stop(log, context=silent))
        check._assess(_stop(log, context=silent))
        after_two = len(notices())
        check._assess(_stop(log, context={"model": "qwen", "kind": "none"}))
        after_switch = len(notices())
        check._assess(_stop(log, context={"model": "luna", "tokens": 9_000, "kind": "usage"}))
        check._assess(_stop(log, context=silent))

        assert (after_two, after_switch, len(notices())) == (1, 2, 3)


class TestThreshold:
    def test_the_threshold_is_the_minds_percentage_of_the_models_listed_window(self, mind):
        """50% of a 400k window is 200k on any harness but claude: no other
        harness pins a window, so nothing caps it at a 200k model's figure."""
        check, gateway, _ = mind
        gateway.windows["gpt-5.6-terra"] = 400_000
        (Path(os.environ["HIVE_PROJECT_DIR"]) / "minds" / "cypher" / "runtime.yaml").write_text(
            "name: cypher\nrotation_threshold_percent: 50\n")

        threshold = check._threshold_for_model(check._cfg(), "gpt-5.6-terra", "")

        assert threshold == 400_000 * 50 // 100

    def test_a_conversation_switched_to_another_model_is_held_to_that_models_window(
        self, mind, tmp_path,
    ):
        """The model is taken from the turn's own entry, not from the spawn: a
        switch inside the conversation is the model the next turn ran on."""
        check, gateway, _ = mind
        gateway.windows.update(big=1_000_000, small=128_000)
        dsh = _load("dsh_transcript")
        log = _dsh_log(tmp_path / "session.jsonl", [
            _turn_start(1),
            _assistant(2, {"inputTokens": 90_000, "outputTokens": 10}, model="big"),
            _turn_start(3),
            _assistant(4, {"inputTokens": 100_000, "outputTokens": 10}, model="small"),
        ])

        assessed = check._assess(_stop(dsh.materialize(log)))

        assert (assessed["window"], assessed["threshold"], assessed["rotate"]) == (
            128_000, 128_000 * 80 // 100, False)


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
