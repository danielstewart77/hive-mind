"""Per-holder rotation: one value, one holder, one file.

The behaviour these guard is the one whose absence took the hive down for four
hours: a rotation that writes one value into five files, and a mint that lands
while the write it was for does not.
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


def _value(path, key):
    return credentials_data._read_env_value(path, key)


@pytest.fixture()
def hive(tmp_path, monkeypatch):
    """The shared `.env` several holders read, plus one holder's own file."""
    shared = tmp_path / "hive_mind.env"
    _write_env(
        shared,
        COMMS_TOKEN_ADA="ada-old",
        COMMS_TOKEN_BILBY_BOT="bilby-bot-old",  # secret-guard: allow — invented fixture
        LUCENT_TOKEN_ADA="ada-lucent-old",  # secret-guard: allow — invented fixture
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
        sent.append({"url": str(request.url), **body})
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


# --- rotation ------------------------------------------------------------


def test_rotating_one_holder_leaves_every_other_holders_value_alone(hive, service):
    _run(holder_credentials.rotate(holder_credentials.SERVICES["comms"], "ada"))

    assert _value(hive["shared"], "COMMS_TOKEN_ADA") != "ada-old"
    assert _value(hive["shared"], "COMMS_TOKEN_BILBY_BOT") == "bilby-bot-old"
    assert _value(hive["shared"], "LUCENT_TOKEN_ADA") == "ada-lucent-old"
    assert _value(hive["own"], "COMMS_BEARER_TOKEN") == "skippy-old"


def test_the_value_minted_is_the_value_written(hive, service):
    """Minting A and writing B passes every other assertion here and leaves
    the holder refused on its next request."""
    _run(holder_credentials.rotate(holder_credentials.SERVICES["comms"], "ada"))

    mint = next(call for call in service["sent"] if call.get("token"))
    assert mint["token"] == _value(hive["shared"], "COMMS_TOKEN_ADA")


def test_a_rotation_keeps_the_role_the_service_reports(hive, service):
    """Re-minting without the role retypes the holder, and comms infers
    `person` for anything it cannot find a mind row for — which is refused
    every machinery route the holder exists to call."""
    _run(holder_credentials.rotate(holder_credentials.SERVICES["comms"], "bilby-bot"))

    mint = next(call for call in service["sent"] if call.get("token"))
    assert mint["role"] == "service"


def test_a_refused_mint_changes_no_file(hive, service):
    service["state"]["status"] = 400

    with pytest.raises(holder_credentials.MintRefused):
        _run(holder_credentials.rotate(holder_credentials.SERVICES["comms"], "ada"))

    assert _value(hive["shared"], "COMMS_TOKEN_ADA") == "ada-old"


def test_a_mint_the_file_will_not_take_puts_the_old_value_back(hive, service, monkeypatch):
    """The lethal order. The mint has already replaced the holder's digest, so
    a failed write leaves it with no working value anywhere — the previous one
    is only recoverable because it was read before the mint."""
    def refuse(path, key, value, label):
        return {"file": label, "path": str(path), "env_key": key,
                "written": False, "reason": "read-only mount"}

    monkeypatch.setattr(credentials_data, "_write_location", refuse)

    with pytest.raises(holder_credentials.Stranded) as raised:
        _run(holder_credentials.rotate(holder_credentials.SERVICES["comms"], "ada"))

    assert raised.value.restored is True
    tokens = [call["token"] for call in service["sent"] if call.get("token")]
    assert tokens[-1] == "ada-old"


def test_a_write_that_does_not_read_back_is_treated_as_a_failed_write(
    hive, service, monkeypatch
):
    """A folded line, a duplicate key lower down, a filesystem that reported a
    write it dropped: the file is what the holder presents, so the file is
    what is believed."""
    def claim_success(path, key, value, label):
        return {"file": label, "path": str(path), "env_key": key,
                "written": True, "reason": None}

    monkeypatch.setattr(credentials_data, "_write_location", claim_success)

    with pytest.raises(holder_credentials.Stranded):
        _run(holder_credentials.rotate(holder_credentials.SERVICES["comms"], "ada"))

    assert _value(hive["shared"], "COMMS_TOKEN_ADA") == "ada-old"


def test_a_holder_with_nowhere_to_read_is_never_minted(hive, service):
    """Minting with nowhere to put the value is how a holder is killed: the
    service stops accepting what that box still presents."""
    with pytest.raises(holder_credentials.MintRefused):
        _run(holder_credentials.rotate(holder_credentials.SERVICES["comms"], "hex"))

    assert [call for call in service["sent"] if call.get("token")] == []


def test_minting_needs_the_services_admin_credential(hive, service, monkeypatch):
    monkeypatch.delenv("LUCENT_ADMIN_BEARER_TOKEN", raising=False)

    with pytest.raises(holder_credentials.MintRefused) as raised:
        _run(holder_credentials.rotate(holder_credentials.SERVICES["lucent"], "ada"))

    assert "LUCENT_ADMIN_BEARER_TOKEN" in str(raised.value)


# --- a new mind ----------------------------------------------------------


def test_a_new_minds_credentials_are_minted_as_a_mind_under_its_uuid(hive, service):
    """Minted before the broker row exists, so the role cannot be inferred;
    and the memory store keys a mind's rows by the uuid its hooks send."""
    minted, failed = _run(
        holder_credentials.provision_mind("quokka", "uuid-1234", "daniel")
    )

    mints = [call for call in service["sent"] if call.get("token")]
    assert failed is None
    assert [call["role"] for call in mints] == ["mind", "mind"]
    assert [call.get("mind_id") for call in mints] == [None, "uuid-1234"]
    assert {entry["service"] for entry in minted} == {"comms", "lucent"}


def test_one_service_refusing_still_reports_the_credential_that_landed(hive, service):
    """A mind holding a gateway credential and no memory-store one
    authenticates, answers, and silently remembers nothing. Reported as the
    two facts it is, rather than as "nothing was issued"."""
    service["state"]["refuse"]["hive-lucent"] = 400

    minted, failed = _run(
        holder_credentials.provision_mind("quokka", "uuid-1234", "daniel")
    )

    assert [entry["service"] for entry in minted] == ["comms"]
    assert "memory store" in failed


def test_a_new_minds_variables_are_added_to_the_shared_file(hive, service):
    _run(holder_credentials.provision_mind("quokka", "uuid-1234", "daniel"))

    assert _value(hive["shared"], "COMMS_TOKEN_QUOKKA")
    assert _value(hive["shared"], "LUCENT_TOKEN_QUOKKA")
    assert _value(hive["shared"], "COMMS_TOKEN_ADA") == "ada-old"


def test_adding_a_variable_never_lands_on_the_last_line(hive, service):
    """A `.env` whose final line has no newline would otherwise take the new
    key onto the end of it, destroying whichever variable was last."""
    hive["shared"].write_text("COMMS_TOKEN_ADA=ada-old\nLAST_ONE=matters")  # secret-guard: allow — invented fixture

    _run(holder_credentials.provision_mind("quokka", "uuid-1234", "daniel"))

    assert _value(hive["shared"], "LAST_ONE") == "matters"
    assert _value(hive["shared"], "COMMS_TOKEN_QUOKKA")


# --- what a rotation must not be confused with ---------------------------


def test_two_rotations_of_one_holder_do_not_interleave(hive, service):
    """A reentrant thread lock is no lock here: every request runs on one
    event-loop thread, so the second coroutine takes it during the first
    one's await and the file and the service's table end up disagreeing —
    both reporting success."""
    order: list[str] = []
    real_put = holder_credentials._put

    async def watched(*args, **kwargs):
        order.append("mint")
        await asyncio.sleep(0)
        return await real_put(*args, **kwargs)

    holder_credentials._put = watched
    try:
        real_write = credentials_data._write_location

        def watched_write(*args, **kwargs):
            order.append("write")
            return real_write(*args, **kwargs)

        original = credentials_data._write_location
        credentials_data._write_location = watched_write
        try:
            async def both():
                await asyncio.gather(
                    holder_credentials.mint(
                        holder_credentials.SERVICES["comms"], "ada", "daniel", role="mind"
                    ),
                    holder_credentials.mint(
                        holder_credentials.SERVICES["comms"], "bob", "daniel", role="mind",
                        location=holder_credentials.Location(
                            "hive_mind", "COMMS_TOKEN_BILBY_BOT"
                        ),
                    ),
                )

            asyncio.run(both())
        finally:
            credentials_data._write_location = original
    finally:
        holder_credentials._put = real_put

    assert order == ["mint", "write", "mint", "write"]


def test_a_mint_that_is_never_answered_is_not_reported_as_refused(hive, service):
    """A read timeout on a PUT most often means the request landed. Reported
    as a refusal the operator retries; reported as stranded they go looking
    for a value that was never written."""
    service["state"]["raise_on_put"] = httpx.ReadTimeout

    with pytest.raises(holder_credentials.MintUncertain):
        _run(
            holder_credentials.mint(
                holder_credentials.SERVICES["comms"], "ada", "daniel", role="mind"
            )
        )

    assert _value(hive["shared"], "COMMS_TOKEN_ADA") == "ada-old"


def test_a_redirect_is_not_a_mint(hive, service):
    """A 3xx is what a reverse proxy or an http-to-https hop gives, and httpx
    does not follow one on a PUT — so the service never saw the value."""
    service["state"]["status"] = 307

    with pytest.raises(holder_credentials.MintRefused):
        _run(holder_credentials.rotate(holder_credentials.SERVICES["comms"], "ada"))

    assert _value(hive["shared"], "COMMS_TOKEN_ADA") == "ada-old"


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


def test_a_quoted_previous_value_is_reinstated_as_the_holder_presents_it(
    hive, service, monkeypatch
):
    """Quoting is legal in a `.env`. Re-minting the raw text hands the holder
    a value it will never present, under a message saying it was recovered."""
    hive["shared"].write_text('COMMS_TOKEN_ADA="ada-old"\n')

    def refuse(path, key, value, label):
        return {"file": label, "path": str(path), "env_key": key,
                "written": False, "reason": "read-only mount"}

    monkeypatch.setattr(credentials_data, "_write_location", refuse)

    with pytest.raises(holder_credentials.Stranded):
        _run(holder_credentials.rotate(holder_credentials.SERVICES["comms"], "ada"))

    tokens = [call["token"] for call in service["sent"] if call.get("token")]
    assert tokens[-1] == "ada-old"


def test_a_name_another_holder_already_has_is_named_as_taken(hive, service):
    """`scheduler` and `spark-to-bloom` hold credentials and are nobody's
    mind. Minting over one replaces a running app's value and retypes it."""
    taken = _run(holder_credentials.name_held_by_something_else("bilby-bot"))
    free = _run(holder_credentials.name_held_by_something_else("quokka"))

    assert taken == holder_credentials.SERVICES["comms"].label
    assert free is None
