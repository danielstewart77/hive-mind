"""What the ledger's command line turns Daniel's words into.

Only the translation, which is this tool's own: every operation behind it is
hive-data's and is guarded there, against a real database. What is here is
the one place the two disagree about `None` — `--answered` clears a question
by writing None, while every other option reads None as "not passed" — and
the refusals that keep a mistyped command from writing anything.
"""

from __future__ import annotations

import importlib.util
import json
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


# ------------------------------------------- a hive_data that is behind it
#
# The tool is bind-mounted live from this checkout and the library reaches
# the interpreter by its own route, so the two can stand at different
# commits. On 2026-11-15 they did, and every question and sub-agent Stash
# recorded failed as a TypeError about one keyword. These hold that a
# library behind the tool is refused by name, before the database.


def _run(ledger, capsys, monkeypatch, *argv):
    # No database: a command the check let through would fail on this, as
    # a different kind, so the kind asserted says which of the two ran.
    monkeypatch.delenv("HIVE_DATABASE_URL", raising=False)
    code = ledger.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_every_command_finds_what_it_calls_in_the_hive_data_beside_it(ledger):
    # The suite imports the host's hive-data checkout, which is what the
    # containers mount. Failing here is that checkout standing behind this
    # one - or a command named as needing something hive_data never had.
    assert {c: ledger.missing(c) for c in ledger.NEEDS} == {
        c: [] for c in ledger.NEEDS}


def test_a_question_against_an_update_task_that_predates_it_is_named_as_behind(
        ledger, capsys, monkeypatch):
    async def update_task(task_id, *, name=None, description=None,
                          status=None, expected_status=None, now=None,
                          unit=None):
        raise AssertionError("reached the library")

    monkeypatch.setattr(ledger.staff, "update_task", update_task)
    code, out = _run(ledger, capsys, monkeypatch,
                     "edit", "t1", "--question", "Which model judges?")
    assert code == 1
    assert out["kind"] == "HiveDataBehind"
    assert "update_task(question=)" in out["error"]


def test_a_sub_agent_against_a_hive_data_with_no_start_agent_is_named_as_behind(
        ledger, capsys, monkeypatch):
    monkeypatch.delattr(ledger.staff, "start_agent")
    code, out = _run(ledger, capsys, monkeypatch,
                     "agent-start", "t1", "brief.md")
    assert code == 1
    assert out["kind"] == "HiveDataBehind"
    assert "hive_data.staff.start_agent" in out["error"]


def test_a_hive_data_with_no_ledger_module_is_named_as_behind(
        ledger, capsys, monkeypatch):
    monkeypatch.setattr(ledger, "staff", None)
    code, out = _run(ledger, capsys, monkeypatch, "statuses")
    assert code == 1
    assert out["kind"] == "HiveDataBehind"
    assert "hive_data.staff" in out["error"]
