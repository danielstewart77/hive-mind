"""Replacing a mind's key: the old one is refused at once, every other mind keeps working.

Protected behaviour 3 (the replaced half): a mind whose key has been replaced
is refused on its very next request while every other mind keeps working.

The gateway and the memory store are the real applications, driven over ASGI
against the suite's scratch Postgres, so "refused on the next request" is
their own answer to the old key and not a stub's. The tools run in another
tree whose module names collide with this console's, so they stand in at the
transport; their own refusal of a replaced key is guarded by the tools' own
suite (tests/api/test_credentials_routes.py).
"""
import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

import credentials_data
import holder_credentials
import mind_key_files
from hive_site import testing

NERVOUS = Path(__file__).resolve().parents[3] / "nervous-system"
sys.path.insert(0, str(NERVOUS))

ADA_ID = "6a1c0000-0000-4000-8000-0000000000ad"
BOB_ID = "6a1c0000-0000-4000-8000-0000000000b0"
OLD, NEW = "ada-old-key-0123456789", "ada-new-key-0123456789"
BOB = "bob-own-key-0123456789"
ADMIN = {"comms": "gateway-admin-for-tests", "lucent": "memory-admin-for-tests",
         "tools": "tools-admin-for-tests"}  # secret-guard: allow - invented fixtures


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class ToolsStandIn:
    """The far side of the tools' credential route: one value per holder."""

    def __init__(self):
        self.values = {}
        self.fail = False
        self.cancel = False
        self.down = False
        self.revoked = []
        self.refuse_delete = False
        self.ignore_delete = False
        self.error_status = None
        self.refuse_with_revoked = False

    def __call__(self, request):
        if self.down:
            raise httpx.ConnectError("the tools are down", request=request)
        if request.url.path.startswith("/credentials/") and request.method == "DELETE":
            holder = request.url.path.rsplit("/", 1)[-1]
            if self.error_status:
                # An error answer whose body claims the revoke: the status, not
                # the body, must decide.
                self.values.pop(holder, None)
                self.revoked.append(holder)
                return httpx.Response(self.error_status, json={"revoked": True})
            if self.refuse_with_revoked:
                return httpx.Response(500, json={"revoked": True})
            if self.ignore_delete:
                return httpx.Response(200, json={"holder": holder, "revoked": False})
            if self.refuse_delete:
                return httpx.Response(500, json={"error": "the tools could not revoke"})
            live = self.values.pop(holder, None) is not None
            if live:
                self.revoked.append(holder)
            return httpx.Response(200, json={"holder": holder, "revoked": live})
        if request.url.path == "/credentials" and request.method == "GET":
            return httpx.Response(200, json=
                [{"holder": h, "role": "mind", "live": True} for h in self.values]
                + [{"holder": h, "role": "mind", "live": False} for h in self.revoked
                   if h not in self.values])
        if request.url.path.startswith("/credentials/") and request.method == "PUT":
            holder = request.url.path.rsplit("/", 1)[-1]
            value = json.loads(request.content)["token"]
            if self.cancel:
                self.cancel = False  # the cancelling happens once
                raise asyncio.CancelledError()
            if self.fail:
                return httpx.Response(500, json={"error": "the tools are down"})
            for other, held in self.values.items():
                if other != holder and held == value:
                    return httpx.Response(400, json={"error": f"that value is already held by {other!r}"})
            self.values[holder] = value
            return httpx.Response(200, json={"holder": holder})
        if request.url.path == "/hitl/credential-probe-no-such-request":
            ok = request.headers["authorization"].split(" ", 1)[1].strip() in self.values.values()
            return httpx.Response(404 if ok else 401, json={})
        return httpx.Response(404, json={})


class Hive:
    def __init__(self, env, tools, transport_log):
        self.env, self.tools, self.log = env, tools, transport_log

    async def status(self, key):
        """What each service answers a key on its own authenticated route."""
        answers = {}
        for service in ("comms", "lucent", "tools"):
            service_object = holder_credentials.SERVICES[service]
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    service_object.url() + holder_credentials.PROBE_PATHS[service],
                    headers={"Authorization": f"Bearer {key}"})
            answers[service] = response.status_code
        return answers


@pytest.fixture
def hive(tmp_path, monkeypatch, builtin_console):
    from comms import server as comms_server, user_access
    from lucent_api import holder_access
    from lucent_api.server import create_app
    from hive_data import core

    shared = tmp_path / "hive_mind.env"
    # Each mind's key is its own file in a folder of its own (requirement 21);
    # the shared .env holds no mind key.
    shared.write_text("UNRELATED=keep\n")
    mind_key_files.write("ada", OLD)
    mind_key_files.write("bob", BOB)
    files = {"hive_mind": shared}
    monkeypatch.setattr(credentials_data, "ENV_FILES", files)
    monkeypatch.setattr(holder_credentials, "ENV_FILES", files)
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(holder_credentials.key_journal, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setenv("GATEWAY_API_URL", "http://comms.test")
    monkeypatch.setenv("LUCENT_API_URL", "http://lucent.test")
    monkeypatch.setenv("HIVE_TOOLS_URL", "http://tools.test")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN["comms"])
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", ADMIN["lucent"])
    monkeypatch.setenv("TOOLS_ADMIN_BEARER_TOKEN", ADMIN["tools"])
    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.invalid:8899")

    tools = ToolsStandIn()
    apps = {
        "comms.test": httpx.ASGITransport(app=comms_server.app),
        "lucent.test": httpx.ASGITransport(app=create_app()),
        "tools.test": httpx.MockTransport(tools),
    }

    class Routed(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await apps[request.url.host].handle_async_request(request)

    original = httpx.AsyncClient

    class Client(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = Routed()
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def seed():
        for mind_id, name in ((ADA_ID, "ada"), (BOB_ID, "bob")):
            await core.upsert_mind(
                mind_id=mind_id, name=name, gateway_url="http://mind.invalid:8420",
                model="m", harness="dsh", registered_at=1.0, last_seen=1.0, owner="daniel")
        for name, mind_id, key in (("ada", ADA_ID, OLD), ("bob", BOB_ID, BOB)):
            await user_access.put_credential(name, "daniel", key, role="mind")
            await holder_access.put_credential(name, "daniel", key, role="mind", mind_id=mind_id)
            tools.values[name] = key

    testing.run(seed())
    return Hive(shared, tools, None)


async def _replace(key=NEW):
    return await holder_credentials.replace_mind_key("ada", "daniel", ADA_ID, key)


async def test_a_replaced_key_is_refused_by_all_three_services_on_the_next_request_and_bobs_keeps_working(hive):
    assert await hive.status(OLD) == {"comms": 200, "lucent": 200, "tools": 404}

    result = await _replace()

    assert [s["service"] for s in result["services"]] == ["comms", "lucent", "tools"]
    # Same process, same apps, nothing restarted.
    for service, code in (await hive.status(OLD)).items():
        assert code in (401, 403), service
    assert await hive.status(NEW) == {"comms": 200, "lucent": 200, "tools": 404}
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200, "tools": 404}
    assert mind_key_files.read("ada") == NEW
    assert mind_key_files.read("bob") == BOB


async def test_a_replacement_the_tools_cannot_take_puts_the_old_key_back_on_the_others(hive):
    hive.tools.fail = True

    with pytest.raises(holder_credentials.MintRefused):
        await _replace()

    assert await hive.status(NEW) == {"comms": 401, "lucent": 401, "tools": 401}
    answers = await hive.status(OLD)
    assert answers["comms"] == 200 and answers["lucent"] == 200
    assert mind_key_files.read("ada") == OLD
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200, "tools": 404}
    assert holder_credentials.key_journal.unfinished() == []


async def test_pasting_a_key_another_mind_holds_changes_nothing_and_both_minds_keep_theirs(hive):
    with pytest.raises(holder_credentials.KeyHeldByAnother):
        await _replace(BOB)

    assert await hive.status(OLD) == {"comms": 200, "lucent": 200, "tools": 404}
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200, "tools": 404}
    assert mind_key_files.read("ada") == OLD


async def test_a_cut_off_mind_stays_cut_off_when_a_replacement_fails_and_is_put_back(hive):
    for name in ("comms", "lucent"):
        await holder_credentials.revoke(holder_credentials.SERVICES[name], "ada")
    assert (await hive.status(OLD))["comms"] == 401
    hive.tools.fail = True

    with pytest.raises(holder_credentials.MintRefused):
        await _replace()

    assert await hive.status(NEW) == {"comms": 401, "lucent": 401, "tools": 401}
    answers = await hive.status(OLD)
    assert answers["comms"] == 401 and answers["lucent"] == 401
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200, "tools": 404}


async def test_a_key_a_service_already_accepts_is_refused_before_anything_changes(hive):
    from lucent_api import holder_access
    await holder_access.put_credential("carol", "daniel", NEW, role="service")

    with pytest.raises(holder_credentials.KeyHeldByAnother):
        await _replace()

    assert await hive.status(OLD) == {"comms": 200, "lucent": 200, "tools": 404}
    assert mind_key_files.read("ada") == OLD
    assert holder_credentials.key_journal.unfinished() == []


async def test_a_request_cancelled_part_way_still_puts_the_old_key_back(hive):
    hive.tools.cancel = True

    with pytest.raises(asyncio.CancelledError):
        await _replace()

    assert await hive.status(NEW) == {"comms": 401, "lucent": 401, "tools": 401}
    answers = await hive.status(OLD)
    assert answers["comms"] == 200 and answers["lucent"] == 200
    assert mind_key_files.read("ada") == OLD


async def test_a_cut_off_mind_whose_old_key_is_unknown_is_not_left_live_on_the_new_key(hive):
    for name in ("comms", "lucent"):
        await holder_credentials.revoke(holder_credentials.SERVICES[name], "ada")
    mind_key_files.remove("ada")  # ada's key is not stored here
    hive.tools.fail = True

    with pytest.raises(holder_credentials.ReplacementFailed):
        await _replace()

    answers = await hive.status(NEW)
    assert answers["comms"] == 401 and answers["lucent"] == 401


def _every_file_holding(root, value):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file() and value.encode() in p.read_bytes())


async def test_after_a_replacement_no_file_under_the_install_holds_the_old_key(hive, tmp_path):
    # The install also kept this mind's key as the installer's proxy key, as the
    # hive's services' proxy key where they share it, quoted, and in a copy the
    # console made of the env file before an earlier write (requirement 22).
    hive.env.write_text(
        f"UNRELATED=keep\nMIND_PROXY_KEY='{OLD}'\nSERVICE_PROXY_KEY={OLD}\n"
        f"SOMETHING_ELSE={BOB}\n")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "hive_mind-MIND_PROXY_KEY-20261126T000000Z.env").write_text(
        f"MIND_PROXY_KEY={OLD}\n")
    assert _every_file_holding(tmp_path, OLD)  # the scan finds it while it is there

    await _replace()

    assert _every_file_holding(tmp_path, OLD) == []
    text = hive.env.read_text()
    assert f"MIND_PROXY_KEY={NEW}" in text and f"SERVICE_PROXY_KEY={NEW}" in text
    assert f"SOMETHING_ELSE={BOB}" in text and "UNRELATED=keep" in text
    assert mind_key_files.read("ada") == NEW
    assert _every_file_holding(tmp_path, BOB)  # another mind's key is left alone


async def test_a_replacement_the_tools_refuse_leaves_the_old_key_in_every_file_it_was_in(hive, tmp_path):
    hive.env.write_text(f"MIND_PROXY_KEY={OLD}\nSERVICE_PROXY_KEY={OLD}\n")
    hive.tools.fail = True

    with pytest.raises(holder_credentials.MintRefused):
        await _replace()

    assert hive.env.read_text() == f"MIND_PROXY_KEY={OLD}\nSERVICE_PROXY_KEY={OLD}\n"
    assert _every_file_holding(tmp_path, NEW) == []
    assert mind_key_files.read("ada") == OLD


async def test_a_mind_file_that_holds_the_key_literally_stops_a_replacement_before_anything_changes(hive, tmp_path, monkeypatch):
    import configuration_data
    minds = tmp_path / "minds"
    (minds / "ada" / "container").mkdir(parents=True)
    (minds / "ada" / "runtime.yaml").write_text("env: {}\n")
    (minds / "ada" / "container" / "compose.yaml").write_text(
        f"services:\n  ada:\n    environment:\n      - MIND_PROXY_KEY={OLD}\n")
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", minds)

    with pytest.raises(holder_credentials.MintRefused, match="container/compose.yaml"):
        await _replace()

    assert mind_key_files.read("ada") == OLD
    assert await hive.status(OLD) == {"comms": 200, "lucent": 200, "tools": 404}
    assert await hive.status(NEW) == {"comms": 401, "lucent": 401, "tools": 401}


async def test_a_backup_holding_the_old_key_keeps_the_other_credentials_it_holds(hive, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    saved = backups / "hive_mind-HIVE_TOOLS_TOKEN-20261126T000000Z.env"
    saved.write_text(f"HIVE_TOOLS_TOKEN=previous-tools-token\nMIND_PROXY_KEY={OLD}\n")

    await _replace()

    assert "previous-tools-token" in saved.read_text()
    assert OLD not in saved.read_text()


async def test_an_env_line_the_console_cannot_rewrite_that_holds_the_old_key_fails_the_replacement_and_puts_the_old_key_back(hive, tmp_path):
    hive.env.write_text(f"export MIND_PROXY_KEY={OLD}\n")

    with pytest.raises(holder_credentials.MintRefused, match="still holds the old key"):
        await _replace()

    assert mind_key_files.read("ada") == OLD
    assert await hive.status(OLD) == {"comms": 200, "lucent": 200, "tools": 404}
    assert await hive.status(NEW) == {"comms": 401, "lucent": 401, "tools": 401}


async def test_a_runtime_file_env_that_holds_the_key_stops_a_replacement_before_anything_changes(hive, tmp_path, monkeypatch):
    import configuration_data
    minds = tmp_path / "minds"
    (minds / "ada").mkdir(parents=True)
    (minds / "ada" / "runtime.yaml").write_text(f"env:\n  MIND_PROXY_KEY: {OLD}\n")
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", minds)

    with pytest.raises(holder_credentials.MintRefused, match="runtime.yaml"):
        await _replace()

    assert mind_key_files.read("ada") == OLD


async def test_a_console_stopped_between_the_key_file_and_the_env_lines_brings_the_env_lines_to_the_stored_key_on_start(hive, tmp_path):
    hive.env.write_text(f"MIND_PROXY_KEY={OLD}\nSERVICE_PROXY_KEY={OLD}\n")
    real = holder_credentials._write_stored
    seen = []

    def stop_after_the_key_file(place, value, backup=True):
        written = real(place, value, backup)
        seen.append(place)
        if place.is_key_file:
            raise KeyboardInterrupt  # the console is gone before the env lines
        return written

    holder_credentials._write_stored = stop_after_the_key_file
    try:
        with pytest.raises(KeyboardInterrupt):
            await _replace()
    finally:
        holder_credentials._write_stored = real
    assert mind_key_files.read("ada") == NEW and OLD in hive.env.read_text()

    handled = await holder_credentials.recover_replacements()

    assert handled == [{"mind": "ada", "recovered": True}]
    assert _every_file_holding(tmp_path, OLD) == []
    assert f"MIND_PROXY_KEY={NEW}" in hive.env.read_text()


async def test_a_saved_copy_that_cannot_be_cleaned_of_the_old_key_refuses_the_replacement_before_anything_changes(hive, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "hive_mind-X-1.env").write_bytes(b"\xff\xfe" + OLD.encode())  # not text: still a copy of the key
    stuck = backups / "stuck.env"
    stuck.mkdir()  # a directory in the way is skipped, a file that cannot be replaced is not
    (backups / "locked.env").write_text(f"MIND_PROXY_KEY={OLD}\n")
    import os
    os.chmod(backups, 0o500)
    try:
        if os.access(backups, os.W_OK):
            pytest.skip("this user can write a read-only folder")
        with pytest.raises(holder_credentials.MintRefused, match="cannot be cleaned"):
            await _replace()
    finally:
        os.chmod(backups, 0o700)
    assert mind_key_files.read("ada") == OLD
    assert await hive.status(NEW) == {"comms": 401, "lucent": 401, "tools": 401}


async def test_a_cleaned_saved_copy_stays_private_and_no_temporary_file_is_left(hive, tmp_path):
    import os, stat
    backups = tmp_path / "backups"
    backups.mkdir()
    saved = backups / "hive_mind-X-1.env"
    saved.write_text(f"MIND_PROXY_KEY={OLD}\nOTHER=keep\n")
    os.chmod(saved, 0o600)

    result = await _replace()

    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert sorted(p.name for p in backups.iterdir()) == [saved.name]
    assert result["copies_still_holding_old_key"] == []
