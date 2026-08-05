"""Unit tests for the training run ledger."""

from __future__ import annotations

import time

import pytest

from core.training_runs import (
    KIND_CURATE,
    KIND_EXPORT,
    KIND_TRAIN,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    connect,
    finish_run,
    get_run,
    init_db,
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


def test_a_long_run_whose_trainer_is_alive_is_not_reaped(ledger):
    """Requirement 12: a healthy LoRA outlives the cutoff and must survive.

    The reaper fires on every status and runs poll, so opening the console
    mid-afternoon used to mark a still-training job failed — and anything
    keyed to that row then acted on a GPU the trainer was still holding.
    """
    from core.training_runs import KIND_TRAIN

    alive = start_run(ledger, KIND_TRAIN, options={"output_name": "still-going"})
    dead = start_run(ledger, KIND_TRAIN, options={"output_name": "gone"})
    with connect(ledger) as conn:
        conn.execute(
            "UPDATE training_runs SET started_at = ?",
            (int(time.time()) - 99_999,),
        )
        conn.commit()

    def is_alive(run):
        return (run.options or {}).get("output_name") == "still-going"

    assert reap_stale_runs(ledger, older_than_seconds=3_600, is_alive=is_alive) == 1
    assert get_run(ledger, alive).status == STATUS_RUNNING
    assert get_run(ledger, dead).status == STATUS_FAILED


def test_a_cancelled_run_closes_once_and_is_not_a_failure(tmp_path):
    """Requirement: a run stopped on purpose is recorded as cancelled.

    A ledger that only has "failed" makes a run you killed deliberately
    indistinguishable from one that broke, and the difference is the
    whole reason anyone opens the ledger.
    """
    ledger = tmp_path / "runs.db"
    init_db(ledger)
    run_id = start_run(ledger, KIND_TRAIN, options={"output_name": "run-a"})

    finish_run(ledger, run_id, status=STATUS_CANCELLED, error="stopped by the operator")
    closed = get_run(ledger, run_id)
    assert closed.status == STATUS_CANCELLED
    assert closed.status != STATUS_FAILED
    assert closed.finished_at is not None

    # A watcher retrying a dropped call must not rewrite a recorded outcome.
    finish_run(ledger, run_id, status=STATUS_SUCCEEDED)
    assert get_run(ledger, run_id).status == STATUS_CANCELLED
