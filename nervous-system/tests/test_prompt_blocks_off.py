"""HIVE_PROMPT_BLOCKS_OFF withholds named prompt blocks from named minds.

The switch exists so one mind can run without what the hive adds to its
prompt beside a mind that has it. What it must never do is reach a mind it
did not name, or swallow a user turn that is still owed an answer.
"""
from __future__ import annotations

import asyncio

import pytest

from comms import bootstrap_loader as bl

SOUL = "<soul>\nI am Bob.\n</soul>"
RECENT = "<recent-memory>\n- (x) a thing [score=1.00]\n</recent-memory>"
MEMORY = "<session-memory>\n## Summary\nDid stuff.\n</session-memory>"
PENDING = ("<pending-continuation>\nRespond to it as the user's latest message."
           "\n- deploy the thing\n</pending-continuation>")


@pytest.fixture
def sources(monkeypatch):
    """Every source has something to say, so an absent block was withheld."""
    async def session_memory(**_):
        return f"{MEMORY}\n\n{PENDING}"

    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF_FILE", "/nonexistent")
    monkeypatch.setattr(bl, "_fetch_soul", lambda *_: SOUL)
    monkeypatch.setattr(bl, "_fetch_recent_memory", lambda *_: RECENT)
    monkeypatch.setattr(bl, "_fetch_session_memory", session_memory)


def _compose(mind_name: str = "Bob", mind_id: str = "b9f2") -> str:
    return asyncio.run(bl.compose_prompt_blocks(
        mind_id=mind_id, mind_name=mind_name, client_ref="cr"))


def test_unset_composes_every_block(sources, monkeypatch) -> None:
    monkeypatch.delenv("HIVE_PROMPT_BLOCKS_OFF", raising=False)
    assert _compose() == "\n\n".join([SOUL, RECENT, MEMORY, PENDING])


def test_all_leaves_only_the_unanswered_turn(sources, monkeypatch) -> None:
    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF", "bob=all")
    assert _compose() == PENDING


def test_only_the_named_blocks_are_withheld(sources, monkeypatch) -> None:
    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF", "bob=recent-memory")
    assert _compose() == "\n\n".join([SOUL, MEMORY, PENDING])


def test_a_mind_not_named_is_untouched(sources, monkeypatch) -> None:
    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF", "bob=all")
    assert _compose(mind_name="Bilby", mind_id="37cd") == "\n\n".join(
        [SOUL, RECENT, MEMORY, PENDING])


def test_each_mind_gets_its_own_entry(sources, monkeypatch) -> None:
    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF",
                       "bob=soul, bilby=recent-memory+session-memory")
    assert _compose() == "\n\n".join([RECENT, MEMORY, PENDING])
    assert _compose(mind_name="Bilby", mind_id="37cd") == "\n\n".join(
        [SOUL, PENDING])


def test_a_mind_is_named_by_id_as_well_as_name(sources, monkeypatch) -> None:
    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF", "B9F2=soul")
    assert SOUL not in _compose()


def test_star_names_every_mind(sources, monkeypatch) -> None:
    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF", "*=soul")
    assert SOUL not in _compose()
    assert SOUL not in _compose(mind_name="Bilby", mind_id="37cd")


@pytest.mark.parametrize("value", ["", "bob", "bob=", "bob=sole", "all", "=all"])
def test_a_value_that_names_nothing_withholds_nothing(
        sources, monkeypatch, value) -> None:
    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF", value)
    assert _compose() == "\n\n".join([SOUL, RECENT, MEMORY, PENDING])


def test_unset_variable_reads_the_file_on_every_compose(
        sources, monkeypatch, tmp_path) -> None:
    switch = tmp_path / "prompt_blocks_off"
    monkeypatch.delenv("HIVE_PROMPT_BLOCKS_OFF", raising=False)
    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF_FILE", str(switch))

    switch.write_text("# the baseline arm\nbob=soul\nbilby=all\n")
    assert _compose() == "\n\n".join([RECENT, MEMORY, PENDING])
    assert _compose(mind_name="Bilby", mind_id="37cd") == PENDING

    switch.unlink()
    assert _compose() == "\n\n".join([SOUL, RECENT, MEMORY, PENDING])


def test_the_variable_wins_over_the_file(sources, monkeypatch, tmp_path) -> None:
    switch = tmp_path / "prompt_blocks_off"
    switch.write_text("bob=all\n")
    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF_FILE", str(switch))
    monkeypatch.setenv("HIVE_PROMPT_BLOCKS_OFF", "")
    assert _compose() == "\n\n".join([SOUL, RECENT, MEMORY, PENDING])
