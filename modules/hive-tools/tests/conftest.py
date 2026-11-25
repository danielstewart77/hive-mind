"""Shared test fixtures for hive-tools."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import scratch


@pytest.fixture(scope="session")
def scratch_database():
    """One scratch database for the run, claimed, with the schemas applied.

    Fails rather than skips without a server: a suite that skipped every
    test would read as green while testing nothing.
    """
    if not scratch.ADMIN_DSN:
        pytest.fail(scratch.NO_SERVER)
    with scratch.testclients_on_the_suite_loop():
        database = scratch.run(scratch.ScratchDatabase.create())
        try:
            yield database
        finally:
            scratch.run(database.close())
            scratch.stop_suite_portal()


@pytest.fixture(autouse=True)
def _on_the_scratch_database(scratch_database, monkeypatch):
    """Every test on an emptied scratch database.

    `HIVE_DATABASE_URL` is pointed at it here, whatever this host exports:
    on the machine that runs the service that variable names the live
    database, and a suite that inherited it would edit production and report
    green.
    """
    monkeypatch.setenv("HIVE_DATABASE_URL", scratch_database.dsn)
    scratch.run(scratch_database.reset())
    yield


@pytest.fixture(autouse=True)
def _block_outbound_telegram() -> Generator[None, None, None]:
    """Keep HITL approval prompts inside the test run.

    Any request to a HITL-gated endpoint whose mode is ``on`` creates an
    approval request and calls ``notify_daniel``, which POSTs to the real
    Telegram API using the bot token in the environment. A test suite that
    exercises gated endpoints therefore pages a human — once per test, with
    no context explaining why.

    The training launcher has its own notifier — ``training_gpu.notify_owner``
    talks to Telegram directly rather than through HITL — and the launch path
    arms a watcher holding a reference to it. Emptying the bot credentials for
    the whole run makes that notifier short-circuit at its own unconfigured
    check, which covers every notification path rather than the two we
    happen to know the names of.

    Tests that assert on notification behaviour patch ``httpx.post``
    themselves and are unaffected; this only stops the ones that never meant
    to send anything.
    """
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_OWNER_CHAT_ID": ""}):
        with patch("hitl.notify_daniel", return_value=None):
            yield


@pytest.fixture(autouse=True)
def _no_real_docker_from_the_gpu_arbiter(tmp_path: Path) -> Generator[None, None, None]:
    """Keep the arbiter's side effects off this machine.

    ``training_gpu`` stops the voice containers, evicts Ollama's models and
    writes a journal at a path baked in from the environment. A test that
    reaches the launch path without patching those does all of it for real,
    to the host running the tests. Anything genuinely exercising docker
    patches ``_docker`` or ``trainer_status`` itself and never gets here.
    """
    import tools.training_gpu as training_gpu

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "training_gpu shelled out to docker during a test; patch _docker "
            "or trainer_status in the test that needs it"
        )

    with patch.object(training_gpu.subprocess, "run", _refuse):
        with patch.object(training_gpu, "JOURNAL_PATH", tmp_path / "gpu_arbitration.json"):
            yield


@pytest.fixture()
def db_conn() -> Generator[scratch.RawSQL, None, None]:
    """Open the store and hand back raw SQL against the same database.

    The operations are module-level and async, so a unit test needs the
    store open. What is yielded reads and writes the database directly, so
    a test can set up a row the operations will see and read back what they
    wrote.
    """
    import store

    scratch.run(store.open())
    try:
        yield scratch.db
    finally:
        scratch.run(store.close())


def pytest_pyfunc_call(pyfuncitem):
    """Run coroutine tests on the suite's loop, without a plugin for it.

    The suite is otherwise synchronous, and one async module is not worth a
    dependency whose default event-loop scoping would then apply to every
    test in the repo. The loop is the one the store's pool belongs to.
    """
    import inspect

    func = pyfuncitem.obj
    if not inspect.iscoroutinefunction(func):
        return None
    kwargs = {
        name: pyfuncitem.funcargs[name]
        for name in pyfuncitem._fixtureinfo.argnames
        if name in pyfuncitem.funcargs
    }
    scratch.run(func(**kwargs))
    return True
