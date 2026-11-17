"""Per-holder credentials: where a holder's value is read from, and the rows
the page is built from.

No test here calls rotation, minting or provisioning: that code's job is to write into the
running hive, and a test that wraps it performs the write (see "Code whose
job is to write into production" in the code skill's test-standard.md;
Daniel, 2026-11-17). Those tests were deleted, not redirected, and the
behaviour is unprotected until he has agreed how it can be tested safely.
"""

import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import credentials_data
import holder_credentials


def _run(coro):
    """This repo has no pytest-asyncio; `asyncio.run` is its convention."""
    return asyncio.run(coro)


def _write_env(path, **kv):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{k}={v}" for k, v in kv.items()) + "\n")


@pytest.fixture()
def hive(tmp_path, monkeypatch):
    """The shared `.env` several holders read, plus one holder's own file."""
    shared = tmp_path / "hive_mind.env"
    _write_env(
        shared,
        COMMS_TOKEN_ADA="ada-old",
        COMMS_TOKEN_BILBY_BOT="bilby-bot-old",  # secret-guard: allow — invented fixture
        LUCENT_TOKEN_ADA="ada-lucent-old",  # secret-guard: allow — invented fixture
        # Read off the file at each call, so issuing it restarts nothing.
        PROXY_CREDENTIAL_ADMIN_KEY="proxy-minting-key-for-tests",  # secret-guard: allow — invented fixture
    )
    own = tmp_path / "skippy.env"
    _write_env(own, COMMS_BEARER_TOKEN="skippy-old", OTHER_KEY="untouched")
    env_files = {
        label: tmp_path / f"missing-{label}.env"
        for label in credentials_data.ENV_FILES
    }
    env_files["hive_mind"] = shared
    env_files["skippy"] = own
    monkeypatch.setattr(credentials_data, "ENV_FILES", env_files)
    monkeypatch.setattr(holder_credentials, "ENV_FILES", env_files)
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-for-tests")
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", "lucent-admin-for-tests")
    monkeypatch.delenv("PROXY_CREDENTIAL_ADMIN_KEY", raising=False)
    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.invalid:8899")
    return {"shared": shared, "own": own}


@pytest.fixture()
def service(monkeypatch):
    """The gateway, standing in only at the transport — the point our process
    ends. Records every mint it is sent and answers the holder listing."""
    sent: list[dict] = []
    state = {"status": 200, "refuse": {}, "raise_on_put": None, "holders": [
        {"holder": "ada", "username": "daniel", "role": "mind", "revoked_at": None},
        {"holder": "bilby-bot", "username": "daniel", "role": "service", "revoked_at": None},
        {"holder": "skippy", "username": "daniel", "role": "mind", "revoked_at": None},
        {"holder": "hex", "username": "daniel", "role": "mind", "revoked_at": None},
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=state["holders"])
        if request.method == "DELETE":
            sent.append({"method": "DELETE", "url": str(request.url)})
            return httpx.Response(200, json={"holder": "x", "revoked": True})
        import json as _json

        if state["raise_on_put"] is not None:
            raise state["raise_on_put"](
                "the service did not answer", request=request
            )
        body = _json.loads(request.content)
        sent.append({"url": str(request.url),
                     "bearer": request.headers.get("authorization", ""), **body})
        status = state["status"]
        for fragment, refusal in state["refuse"].items():
            if fragment in str(request.url):
                status = refusal
        if status in (301, 302, 307, 308):
            return httpx.Response(status, headers={"location": "https://elsewhere/"})
        if status >= 400:
            return httpx.Response(status, json={"error": "refused"})
        return httpx.Response(200, json={"holder": body.get("username")})

    original = httpx.AsyncClient

    class _Client(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return {"sent": sent, "state": state}


# --- where a holder reads ------------------------------------------------


def test_a_holder_in_the_shared_file_is_found_by_its_own_variable(hive):
    found = holder_credentials.locate(
        holder_credentials.SERVICES["comms"], "bilby-bot"
    )

    assert (found.file, found.env_key) == ("hive_mind", "COMMS_TOKEN_BILBY_BOT")


def test_a_holder_with_its_own_file_reads_the_plain_bearer_variable(hive):
    found = holder_credentials.locate(holder_credentials.SERVICES["comms"], "skippy")

    assert (found.file, found.env_key) == ("skippy", "COMMS_BEARER_TOKEN")


def test_a_holder_no_mounted_file_holds_a_value_for_has_nowhere_to_read(hive):
    """Hex is on another machine; this console's own file and the browser
    terminal's are on this one and simply not mounted. One answer for both,
    because to this container they are the same fact."""
    assert holder_credentials.locate(holder_credentials.SERVICES["comms"], "hex") is None


# --- the rows the page is built from ------------------------------------


def test_the_rows_name_the_holders_the_service_itself_reports(hive, service):
    service["state"]["holders"].append(
        {"holder": "quokka", "username": "daniel", "role": "service", "revoked_at": None}
    )

    rows = _run(holder_credentials.holder_rows(holder_credentials.SERVICES["comms"]))

    assert "quokka" in [row["holder"] for row in rows]


def test_a_holder_with_nowhere_to_read_is_offered_no_rotation(hive, service):
    rows = _run(holder_credentials.holder_rows(holder_credentials.SERVICES["comms"]))
    by_holder = {row["holder"]: row for row in rows}

    assert by_holder["hex"]["rotatable"] is False
    assert by_holder["hex"]["unrotatable_reason"]
    assert by_holder["ada"]["rotatable"] is True


def test_a_rows_restart_target_is_the_one_that_reads_its_file(hive, service):
    rows = _run(holder_credentials.holder_rows(holder_credentials.SERVICES["comms"]))
    by_holder = {row["holder"]: row for row in rows}

    assert by_holder["skippy"]["restart_target"] == "skippy.service"
    assert by_holder["ada"]["restart_target"] != by_holder["skippy"]["restart_target"]


def test_an_unreadable_shared_file_is_not_reported_as_nowhere_to_read(hive, service):
    """Fourteen holders read that one file. Reported as "nowhere", every one
    of them loses its rotate control and is offered revocation in the same
    cell — so an operator who came to rotate a mind revokes it instead."""
    hive["shared"].write_bytes(b"COMMS_TOKEN_ADA=caf\xe9\n")

    rows = _run(holder_credentials.holder_rows(holder_credentials.SERVICES["comms"]))
    ada = next(row for row in rows if row["holder"] == "ada")

    assert "could not be read" in ada["unrotatable_reason"]
    assert _run(
        holder_credentials.check(holder_credentials.SERVICES["comms"], "ada")
    )["status"] == "unreadable"


def test_a_holder_named_in_two_files_is_refused_rather_than_guessed(hive, service):
    """Nothing says the holder reads the file declared first, and writing the
    wrong one reports a working row with a restart target pointed at the
    wrong machine."""
    _write_env(hive["own"], COMMS_TOKEN_ADA="a-stray-copy")  # secret-guard: allow — invented fixture

    rows = _run(holder_credentials.holder_rows(holder_credentials.SERVICES["comms"]))
    ada = next(row for row in rows if row["holder"] == "ada")

    assert ada["rotatable"] is False
    assert "each name" in ada["unrotatable_reason"]


def test_a_holder_with_its_own_file_is_read_from_that_file(hive, service):
    """The stray copy must not move a holder that has a file of its own."""
    _write_env(
        hive["shared"], COMMS_TOKEN_SKIPPY="a-stray-copy", COMMS_TOKEN_ADA="ada-old"  # secret-guard: allow — invented fixture
    )

    found = holder_credentials.locate(holder_credentials.SERVICES["comms"], "skippy")

    assert (found.file, found.env_key) == ("skippy", "COMMS_BEARER_TOKEN")


def test_a_name_another_holder_already_has_is_named_as_taken(hive, service):
    """`scheduler` and `spark-to-bloom` hold credentials and are nobody's
    mind. Minting over one replaces a running app's value and retypes it."""
    taken = _run(holder_credentials.name_held_by_something_else("bilby-bot"))
    free = _run(holder_credentials.name_held_by_something_else("quokka"))

    assert taken == holder_credentials.SERVICES["comms"].label
    assert free is None
