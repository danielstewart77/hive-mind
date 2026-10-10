"""A conversation rendered for the harness that takes it over.

Every fixture is a real log captured off a running harness — a Claude Code
transcript, a Codex rollout from a container mind, and a dsh session log in
both its compressed and plain forms — with thinking signatures and encrypted
reasoning redacted. A reader written against an imagined schema passes its
own tests and reads nothing in production.
"""

from __future__ import annotations

from pathlib import Path

from minds import transcript

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "transcripts"
CLAUDE = FIXTURES / "claude.jsonl"
CODEX = FIXTURES / "rollout-2026-06-01T16-11-51-019e83f4-b0b3-7a61-b649-392094c6ef10.jsonl"
DSH_ZSTD = FIXTURES / "dsh" / "session.jsonl.zstd"
DSH_PLAIN = FIXTURES / "dsh" / "session.jsonl"


def _results(text: str) -> list[str]:
    return [part for part in text.split("\n\n") if part.startswith("Tool result: ")]


class TestClaudeTranscript:
    def test_a_claude_transcript_renders_prose_and_calls_whole_and_results_trimmed(self):
        text = transcript.render(transcript.read_claude(CLAUDE))

        assert "User: hey, i have two versions of discord installed on this machine." in text
        assert "Assistant: Let me look at what's actually installed before opining." in text
        # The call is whole: its full command, not a head of it.
        assert ("Tool call (Bash): {\"command\": \"dpkg -l | grep -i discord; echo ---; "
                "flatpak list 2>/dev/null | grep -i discord;") in text
        results = _results(text)
        assert len(results) == 1
        assert results[0].endswith(transcript.TRIMMED)
        assert len(results[0].splitlines()) <= transcript.RESULT_LINES + 1
        assert "discord" in results[0]

    def test_thinking_and_harness_bookkeeping_never_reach_the_handover(self):
        text = transcript.render(transcript.read_claude(CLAUDE))

        assert "REDACTED" not in text
        assert "stop_hook_summary" not in text


class TestCodexRollout:
    def test_a_codex_rollout_renders_prose_and_calls_whole_and_results_trimmed(self):
        text = transcript.render(transcript.read_codex(CODEX))

        assert "Tool call (exec_command): {\"cmd\":\"ls -R\",\"max_output_tokens\":200}" in text
        results = _results(text)
        assert results, "no tool result was rendered"
        assert results[0].startswith("Tool result: Chunk ID: c76445")
        assert results[0].endswith(transcript.TRIMMED)

    def test_the_composed_system_prompt_and_injected_context_are_dropped(self):
        """Codex folds the system prompt into the first turn; it rides in
        again as the new harness's own, and is not part of the history."""
        blocks = transcript.read_codex(CODEX)
        text = transcript.render(blocks)

        assert blocks[0] == {"role": "user", "kind": "text",
                             "text": "Run the netsage-alert skill.", "name": ""}
        assert "<environment_context>" not in text
        assert "<permissions instructions>" not in text


class TestDshLog:
    def test_a_compressed_dsh_log_renders_prose_and_calls_whole_and_results_trimmed(self):
        text = transcript.render(transcript.read_dsh(DSH_ZSTD))

        assert text.count("Tool call (") >= 2
        assert ('Tool call (read): {"file_path":"/mnt/dev/model-testing/hive-health-rebuild/'
                'runs/cypher-ea6748e3/DSH_BUILD_MODE"}') in text
        assert "Tool result: Updated todo list: 1 pending, 1 in progress, 0 completed." in text
        assert "User: " in text

    def test_the_plain_log_reads_the_same_as_the_compressed_one(self):
        assert transcript.read_dsh(DSH_PLAIN) == transcript.read_dsh(DSH_ZSTD)

    def test_plugin_context_and_the_composed_soul_are_not_history(self):
        text = transcript.render(transcript.read_dsh(DSH_ZSTD))

        assert "<soul>" not in text
        assert "<available_skills>" not in text
        assert "Current DSH file policy" not in text
        assert "Read DSH_BUILD_MODE to determine if this is human or autonomous mode." in text

    def test_a_log_still_being_written_reads_up_to_its_open_frame(self, tmp_path):
        """The last frame stays open while the conversation runs."""
        live = tmp_path / "session.jsonl.zstd"
        live.write_bytes(DSH_ZSTD.read_bytes()[:15_000])

        blocks = transcript.read_dsh(live)

        assert blocks
        assert blocks == transcript.read_dsh(DSH_ZSTD)[: len(blocks)]


class TestBudget:
    def _blocks(self, count: int, size: int) -> list[dict]:
        return [{"role": "user" if i % 2 == 0 else "assistant", "kind": "text",
                 "text": f"turn {i:04d} " + "x" * size, "name": ""} for i in range(count)]

    def test_an_oversized_rendering_keeps_the_summary_and_drops_the_oldest_first(self):
        summary = "We were fixing the boiler. " * 200
        window = 40_000
        budget = transcript.budget_for_window(window)

        text = transcript.render(self._blocks(400, 500), summary=summary, budget_bytes=budget)

        assert budget == window * 4 // 2
        assert len(text.encode("utf-8")) <= budget
        assert text.startswith(f"Summary of the conversation so far:\n{summary.strip()}")
        assert "turn 0399 " in text
        assert "turn 0000 " not in text
        assert "earlier entries omitted]" in text

    def test_a_large_window_is_still_capped_at_the_byte_ceiling(self):
        budget = transcript.budget_for_window(1_000_000)

        text = transcript.render(self._blocks(1_000, 500), budget_bytes=budget)

        assert budget == transcript.MAX_HANDOVER_BYTES
        assert transcript.MAX_HANDOVER_BYTES - 1_000 < len(text.encode("utf-8")) <= 120_000

    def test_an_unknown_window_caps_at_the_byte_ceiling(self):
        text = transcript.render(self._blocks(1_000, 500),
                                 budget_bytes=transcript.budget_for_window(None))

        assert len(text.encode("utf-8")) <= 120_000
        assert "turn 0999 " in text

    def test_a_summary_with_no_transcript_is_the_summary_alone(self):
        assert transcript.render([], summary="  the summary  ") == (
            "Summary of the conversation so far:\nthe summary"
        )
