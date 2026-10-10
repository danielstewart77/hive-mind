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


class TestOversizedBlocks:
    def _text(self, role: str, text: str) -> dict:
        return {"role": role, "kind": "text", "text": text, "name": ""}

    def test_an_oversized_newest_block_is_cut_to_its_tail_and_older_blocks_still_come(self):
        older = [self._text("user", f"older turn {i}") for i in range(3)]
        huge = self._text("user", "HEAD-" + "x" * 50_000 + "-TAIL")

        text = transcript.render(older + [huge], budget_bytes=10_000)

        assert len(text.encode("utf-8")) <= 10_000
        assert "-TAIL" in text and "HEAD-" not in text
        assert transcript.TRIMMED in text

    def test_a_cut_block_leaves_room_the_walk_spends_on_older_blocks(self):
        older = [self._text("assistant", "the older answer")]
        big = self._text("user", "y" * 5_000)
        newest = self._text("user", "z" * 50_000)

        text = transcript.render(older + [big, newest], budget_bytes=4_000)

        assert len(text.encode("utf-8")) <= 4_000
        assert "zzzz" in text and "yyyy" in text
        assert "Assistant: the older answer" in text

    def test_a_tool_calls_input_keeps_its_head_when_cut(self):
        call = {"role": "assistant", "kind": "tool_call", "name": "Write",
                "text": "START-" + "q" * 50_000 + "-END"}

        text = transcript.render([call], budget_bytes=5_000)

        assert "Tool call (Write): START-" in text
        assert "-END" not in text
        assert text.rstrip().endswith(transcript.TRIMMED)
        assert len(text.encode("utf-8")) <= 5_000

    def test_a_previous_handover_inside_the_transcript_is_trimmed_not_dropped(self):
        previous = self._text("user", "Summary of the conversation so far:\n" + "p" * 30_000
                              + transcript.SEPARATOR + "the question after the switch")

        text = transcript.render([previous], budget_bytes=3_000)

        assert "the question after the switch" in text
        assert len(text.encode("utf-8")) <= 3_000


class TestReaderEdges:
    def test_codex_turns_are_read_once_not_again_from_the_event_stream(self):
        text = transcript.render(transcript.read_codex(CODEX))

        assert text.count("Run the netsage-alert skill.") == 1

    def test_the_byte_ceiling_holds_for_a_real_transcript_with_no_budget_given(self):
        blocks = transcript.read_dsh(DSH_PLAIN) * 200

        text = transcript.render(blocks)

        assert 100_000 < len(text.encode("utf-8")) <= transcript.MAX_HANDOVER_BYTES
        assert "earlier entries omitted]" in text

    def test_a_claude_sidechain_entry_is_not_the_minds_conversation(self, tmp_path):
        log = tmp_path / "t.jsonl"
        log.write_text(CLAUDE.read_text() + '{"type": "user", "isSidechain": true, '
                       '"message": {"role": "user", "content": "DELEGATE WORK"}}\n')

        assert "DELEGATE WORK" not in transcript.render(transcript.read_claude(log))

    def test_one_line_tool_result_is_capped_in_bytes(self):
        result = {"role": "user", "kind": "tool_result", "name": "", "text": "r" * 10_000}

        rendered = transcript.render_block(result)

        assert rendered.endswith(transcript.TRIMMED)
        assert len(rendered.encode("utf-8")) <= transcript.RESULT_BYTES + 64


SCAFFOLDED = FIXTURES / "rollout-2026-10-10T14-17-31-01a1262d-156a-77e3-90c9-6f1621d96390.jsonl"


class TestRoundTwo:
    def test_codex_scaffolding_never_reads_as_something_the_user_said(self):
        blocks = transcript.read_codex(SCAFFOLDED)
        said = [b["text"] for b in blocks if b["role"] == "user" and b["kind"] == "text"]

        assert said[0].startswith("[Saturday, October 10, 2026 at 9:17 AM CDT]\nNope, remove")
        assert any(t.startswith("[Saturday, October 10, 2026 at 10:08 AM CDT]") for t in said)
        joined = "\n".join(said)
        assert "<recommended_plugins>" not in joined
        assert "# AGENTS.md instructions" not in joined

    def test_an_oversized_summary_keeps_its_tail_within_half_the_budget(self):
        summary = "SUMMARY-START " + "s" * 40_000 + " SUMMARY-END"
        blocks = [{"role": "user", "kind": "text", "text": "the latest question", "name": ""}]

        text = transcript.render(blocks, summary=summary, budget_bytes=10_000)

        assert len(text.encode("utf-8")) <= 10_000
        assert "SUMMARY-END" in text and "SUMMARY-START" not in text
        assert transcript.TRIMMED in text.split("Transcript of")[0]
        assert "User: the latest question" in text
        assert text.index("SUMMARY-END") < text.index("the latest question")

    def test_a_prior_handover_leads_the_transcript_and_keeps_its_tail(self):
        prior = "PRIOR-START " + "p" * 30_000 + " PRIOR-END"
        blocks = [{"role": "user", "kind": "text", "text": "after the switch", "name": ""}]

        text = transcript.render(blocks, prior_handover=prior, budget_bytes=6_000)

        assert len(text.encode("utf-8")) <= 6_000
        assert "PRIOR-END" in text and "PRIOR-START" not in text
        assert text.index("PRIOR-END") < text.index("after the switch")
