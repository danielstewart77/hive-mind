"""Cypher's own hooks: the dsh log her Claude-dialect hooks have to read.

Her hooks are copies of the operator mind's, which read a Claude Code
transcript. Her harness writes a different schema into a compressed,
multi-frame container, so one module translates at the boundary rather than
five hooks each learning a second schema. These tests drive that module and
the hook functions whose answers change because of it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[2] / "minds" / "cypher" / "hooks"


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

        measured = check._live_context_tokens(dsh.materialize(log), chars_per_token=4)

        assert measured == 1_000 + 500 + 103_000 + 100

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

        assert check._live_context_tokens(plain, chars_per_token=4) is None


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


class TestThreshold:
    def test_the_threshold_comes_off_the_model_s_window_not_the_serving_ceiling(self, tmp_path, monkeypatch):
        """Her file carries both numbers, and they differ by a factor of eight.

        `context_window` is what dsh serves a request under; a threshold taken
        from it would rotate her at a tenth of the ceiling rather than a tenth
        of the model's window.
        """
        check = _load("rotation_check")
        mind = tmp_path / "minds" / "cypher"
        mind.mkdir(parents=True)
        (mind / "runtime.yaml").write_text(
            "name: cypher\ncontext_window: 131072\n"
            "model_context_window: 1000000\nrotation_threshold_percent: 10\n")
        monkeypatch.setenv("HIVE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setenv("MIND_NAME", "cypher")

        assert check._declared_threshold() == 1_000_000 * 10 // 100


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

