"""What the ledger's command line turns Daniel's words into.

Only the translation, which is this tool's own: every operation behind it is
hive-data's and is guarded there, against a real database. What is here is
the one place the two disagree about `None` — `--answered` clears a question
by writing None, while every other option reads None as "not passed" — and
the refusals that keep a mistyped command from writing anything.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TOOL = (Path(__file__).resolve().parents[2]
        / "tools" / "stateless" / "staff_ledger" / "staff_ledger.py")


@pytest.fixture(scope="module")
def ledger():
    spec = importlib.util.spec_from_file_location("staff_ledger", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _edit(ledger, *argv):
    return ledger._edits(ledger._parser().parse_args(["edit", "t1", *argv]))


def test_the_board_item_a_task_covers_is_written_as_given(ledger):
    assert _edit(ledger, "--board-item", "f1deb69399136e16d") == {
        "board_item": "f1deb69399136e16d"}


def test_a_question_for_daniel_is_written_as_given(ledger):
    assert _edit(ledger, "--question", "Which model judges?") == {
        "question": "Which model judges?"}


def test_an_answered_question_is_cleared_rather_than_left_standing(ledger):
    """None is the value here and means "not passed" everywhere else."""
    assert _edit(ledger, "--answered") == {"question": None}


def test_clearing_a_question_touches_nothing_else_about_the_task(ledger):
    assert _edit(ledger, "--name", "Renamed", "--answered") == {
        "name": "Renamed", "question": None}


def test_an_edit_naming_nothing_is_refused_rather_than_written(ledger):
    with pytest.raises(ledger.Refused):
        _edit(ledger)


def test_asking_and_answering_in_one_command_is_refused_at_the_parser(
        ledger):
    """Which of the two won would be whichever the code happened to read."""
    with pytest.raises(SystemExit) as exit_code:
        ledger._parser().parse_args(
            ["edit", "t1", "--question", "Which?", "--answered"])
    assert exit_code.value.code == 2


def test_a_sub_agent_is_started_against_a_task_with_its_brief(ledger):
    args = ledger._parser().parse_args(
        ["agent-start", "t1", "read-and-judge.md", "--about", "session 1a2b"])
    assert (args.task, args.brief, args.about) == (
        "t1", "read-and-judge.md", "session 1a2b")


def test_a_sub_agent_listing_can_be_narrowed_to_one_task_still_working(
        ledger):
    args = ledger._parser().parse_args(["agents", "--task", "t1", "--working"])
    assert (args.task, args.working) == ("t1", True)


def test_listing_sub_agents_defaults_to_every_task_and_both_states(ledger):
    """A default of "working" would hide the children that already reported."""
    args = ledger._parser().parse_args(["agents"])
    assert (args.task, args.working) == (None, False)
