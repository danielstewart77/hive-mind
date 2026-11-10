"""The installer against the real comms and lucent, on a throwaway database.

Both apps run as their own processes (uvicorn on a free port each), exactly
as their containers run them: they share `hive_data`'s one module-level pool,
so two in one process would be one pool on two event loops. The installer's
client code talks to them over HTTP; what landed is read back on a plain
driver connection, never through the code under test.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import asyncpg
import pytest

from comms_broker_pg import broker_db, put_mind, scratch_dsn  # noqa: F401 - fixtures
from installer import collisions, compose, hive_api, probe
from installer.shell import StepFailed

NS = Path(__file__).resolve().parents[1]
REPO = NS.parent
COMMS_ADMIN = "installer-test-comms-admin"  # secret-guard: allow
LUCENT_ADMIN = "installer-test-lucent-admin"  # secret-guard: allow


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app: str, dsn: str, tmp: Path):
    port = _free_port()
    env = {**os.environ, "HIVE_DATABASE_URL": dsn, "COMMS_ADMIN_BEARER_TOKEN": COMMS_ADMIN,
           "LUCENT_ADMIN_BEARER_TOKEN": LUCENT_ADMIN, "PYTHONPATH": f"{NS}:{REPO}",
           "COMMS_PROJECT_DIR": str(tmp), "PRUNE_LOG_PATH": str(tmp / "prune.log")}
    log = open(tmp / f"{app.split('.')[0]}.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1",
                             "--port", str(port)], cwd=NS, env=env, stdout=log, stderr=log)
    url = f"http://127.0.0.1:{port}"
    admin = COMMS_ADMIN if app.startswith("comms") else LUCENT_ADMIN
    for _ in range(150):
        try:
            req = urllib.request.Request(url + "/health",
                                         headers={"Authorization": f"Bearer {admin}"})
            with urllib.request.urlopen(req, timeout=2):
                return proc, url
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"{app} did not start: {(tmp / (app.split('.')[0] + '.log')).read_text()[-2000:]}")


@pytest.fixture
def hive(broker_db, tmp_path):  # noqa: F811 - the imported fixture
    procs = []
    try:
        comms_proc, comms_url = _serve("comms.server:app", broker_db, tmp_path)
        procs.append(comms_proc)
        lucent_proc, lucent_url = _serve("lucent_api.server:app", broker_db, tmp_path)
        procs.append(lucent_proc)
        yield (hive_api.Api("comms", comms_url, COMMS_ADMIN),
               hive_api.Api("lucent", lucent_url, LUCENT_ADMIN), broker_db)
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _status(url: str, token: str) -> int:
    """A plain request, outside the code under test."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


async def _rows(dsn: str, sql: str, *args):
    conn = await asyncpg.connect(dsn)
    try:
        return [dict(r) for r in await conn.fetch(sql, *args)]
    finally:
        await conn.close()


def _rows_sync(dsn, sql, *args):
    return asyncio.run(_rows(dsn, sql, *args))


def _holders(answer_set: dict) -> dict[str, dict[str, str]]:
    """The service holders the installer mints for these pieces, each with a
    value of its own."""
    suffix = uuid.uuid4().hex[:8]
    return {store: {f"{h}-{suffix}": f"value-{store}-{h}-{suffix}" for h in holders}
            for store, holders in compose.service_holders(answer_set).items()}


def test_each_minted_service_holder_is_listed_as_a_service_is_accepted_and_kept_on_rerun(hive):
    comms, lucent, dsn = hive
    holders = _holders({"pieces": ["console", "terminal", "tools", "telegram"], "modes": {}})
    tables = {"comms": "core.credentials", "lucent": "memory.credentials"}
    for store, api in (("comms", comms), ("lucent", lucent)):
        assert hive_api.mint(api, holders[store], "ann") == {h: "minted" for h in holders[store]}

    for store, api in (("comms", comms), ("lucent", lucent)):
        listed = {r["holder"]: r for r in _rows_sync(
            dsn, f"SELECT holder, role, created_at FROM {tables[store]} WHERE holder = ANY($1)",
            list(holders[store]))}
        assert {h: r["role"] for h, r in listed.items()} == \
            {h: "service" for h in holders[store]}
        for holder, token in holders[store].items():
            assert _status(api.base + hive_api.ACCEPTS[store], token) == 200, (store, holder)
            assert _status(api.base + hive_api.ACCEPTS[store], token + "-wrong") == 401
        before = {h: r["created_at"] for h, r in listed.items()}

        again = hive_api.mint(api, holders[store], "ann")

        assert again == {h: "kept" for h in holders[store]}
        after = {r["holder"]: r["created_at"] for r in _rows_sync(
            dsn, f"SELECT holder, created_at FROM {tables[store]} WHERE holder = ANY($1)",
            list(holders[store]))}
        assert after == before


def test_a_holder_this_install_does_not_hold_is_refused_and_left_as_it_was(hive):
    comms, _, dsn = hive
    holder = f"hive-site-{uuid.uuid4().hex[:8]}"
    hive_api.mint(comms, {holder: "the-running-sites-value"}, "ann")
    before = _rows_sync(dsn, "SELECT * FROM core.credentials WHERE holder = $1", holder)

    with pytest.raises(StepFailed, match=holder):
        hive_api.mint(comms, {holder: "a-value-made-by-a-new-install"}, "ann")

    assert _rows_sync(dsn, "SELECT * FROM core.credentials WHERE holder = $1", holder) == before


def test_a_mint_that_stops_part_way_has_recorded_each_holder_it_made(hive):
    comms, _, _ = hive
    suffix = uuid.uuid4().hex[:8]
    taken = f"taken-{suffix}"
    hive_api.mint(comms, {taken: f"someone-elses-{suffix}"}, "ann")
    recorded = []

    with pytest.raises(StepFailed):
        hive_api.mint(comms, {f"first-{suffix}": f"v1-{suffix}", taken: f"v2-{suffix}"},
                      "ann", on_minted=recorded.append)

    assert recorded == [f"first-{suffix}"]


def test_the_registered_mind_is_stored_on_dsh_owned_by_the_admin_on_the_chosen_model(hive):
    comms, _, dsn = hive
    mind_id = str(uuid.uuid4())

    hive_api.register_mind(comms, mind_id=mind_id, name="wren", gateway_url="http://wren:8420",
                           model="claude-sonnet-5", owner="ann")

    row = _rows_sync(dsn, "SELECT name, harness, owner, model FROM core.minds WHERE mind_id = $1",
                     mind_id)
    assert row == [{"name": "wren", "harness": "dsh", "owner": "ann", "model": "claude-sonnet-5"}]


def _counts(dsn: str) -> dict:
    tables = ["core.users", "core.minds", "core.credentials", "memory.credentials"]
    return {t: _rows_sync(dsn, f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in tables}


def test_against_an_existing_hive_an_unknown_admin_or_a_taken_mind_name_is_refused_unwritten(
        broker_db):  # noqa: F811 - the imported fixture
    dsn = broker_db
    empty = asyncio.run(probe.gather(dsn))
    assert collisions.refusals(
        empty, admin="ann", mind_name="my-mind", mind_id=str(uuid.uuid4()),
        holders={"comms": ["hive-site"], "lucent": []}, ours={}) == []
    _rows_sync(dsn, "INSERT INTO core.users (username, password_hash, created_at) "
                    "VALUES ('daniel', 'x', 1) RETURNING id")
    asyncio.run(put_mind(str(uuid.uuid4()), "my_mind", owner="daniel"))
    before = _counts(dsn)

    facts = asyncio.run(probe.gather(dsn))
    refused = collisions.refusals(
        facts, admin="ann", mind_name="my-mind", mind_id=str(uuid.uuid4()),
        holders={"comms": ["hive-site"], "lucent": []}, ours={})

    assert any("'ann'" in r and "no account" in r for r in refused)
    assert any("'my_mind'" in r for r in refused)
    assert _counts(dsn) == before
    assert collisions.refusals(
        facts, admin="Daniel", mind_name="wren", mind_id=str(uuid.uuid4()),
        holders={"comms": ["hive-site"], "lucent": []}, ours={}) == []
