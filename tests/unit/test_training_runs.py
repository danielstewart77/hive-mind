"""Unit tests for the training run ledger."""

from __future__ import annotations

import time

import pytest

from core.training_runs import (
    KIND_CURATE,
    KIND_EXPORT,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    connect,
    finish_run,
    get_run,
    latest_run,
    list_runs,
    reap_stale_runs,
    start_run,
)


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "nested" / "training_runs.db"


def test_start_run_opens_a_running_row(ledger):
    run_id = start_run(ledger, KIND_CURATE, {"keep_per_cluster": 3})
    run = get_run(ledger, run_id)
    assert run.status == STATUS_RUNNING
    assert run.kind == KIND_CURATE
    assert run.options == {"keep_per_cluster": 3}
    assert run.finished_at is None


def test_finish_run_records_report_and_duration(ledger):
    run_id = start_run(ledger, KIND_EXPORT)
    finish_run(
        ledger,
        run_id,
        status=STATUS_SUCCEEDED,
        report={"train_examples": 12},
        artifact_path="/data/sets/v1",
    )
    run = get_run(ledger, run_id)
    assert run.status == STATUS_SUCCEEDED
    assert run.report == {"train_examples": 12}
    assert run.artifact_path == "/data/sets/v1"
    assert run.as_dict()["duration_seconds"] is not None


def test_finished_run_cannot_be_reopened_or_rewritten(ledger):
    run_id = start_run(ledger, KIND_CURATE)
    finish_run(ledger, run_id, status=STATUS_SUCCEEDED, report={"kept": 1})
    finish_run(ledger, run_id, status=STATUS_FAILED, error="second thoughts")
    run = get_run(ledger, run_id)
    assert run.status == STATUS_SUCCEEDED
    assert run.error is None


def test_unknown_kind_is_rejected(ledger):
    with pytest.raises(ValueError):
        start_run(ledger, "hallucinate")


def test_non_terminal_finish_status_is_rejected(ledger):
    run_id = start_run(ledger, KIND_CURATE)
    with pytest.raises(ValueError):
        finish_run(ledger, run_id, status=STATUS_RUNNING)


def test_list_runs_is_newest_first_and_filterable(ledger):
    first = start_run(ledger, KIND_CURATE)
    second = start_run(ledger, KIND_EXPORT)
    with connect(ledger) as conn:
        conn.execute(
            "UPDATE training_runs SET started_at = started_at + 10 WHERE id = ?",
            (second,),
        )
        conn.commit()

    assert [r.id for r in list_runs(ledger)] == [second, first]
    assert [r.id for r in list_runs(ledger, kind=KIND_CURATE)] == [first]
    assert latest_run(ledger, KIND_EXPORT).id == second


def test_latest_run_is_none_when_nothing_ran(ledger):
    assert latest_run(ledger, KIND_CURATE) is None


def test_reaper_fails_runs_that_outlived_their_budget(ledger):
    stale = start_run(ledger, KIND_CURATE)
    fresh = start_run(ledger, KIND_EXPORT)
    with connect(ledger) as conn:
        conn.execute(
            "UPDATE training_runs SET started_at = ? WHERE id = ?",
            (int(time.time()) - 99_999, stale),
        )
        conn.commit()

    assert reap_stale_runs(ledger, older_than_seconds=3_600) == 1
    assert get_run(ledger, stale).status == STATUS_FAILED
    assert "reaped" in get_run(ledger, stale).error
    assert get_run(ledger, fresh).status == STATUS_RUNNING


def test_get_run_returns_none_for_unknown_id(ledger):
    assert get_run(ledger, "no-such-run") is None
