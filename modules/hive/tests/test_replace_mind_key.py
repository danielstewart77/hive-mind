"""Replacing a mind's key: the old one is refused at once, every other mind keeps working.

Protected behaviour 3 (the replaced half): a mind whose key has been replaced
is refused on its very next request while every other mind keeps working.

A mind's key is one row in the hive database, written by the console. The
gateway and the memory store are the real applications, driven over ASGI
against the suite's scratch Postgres, so "refused on the next request" is
their own answer to the old key and not a stub's.
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
import key_journal
import mind_key_files
from hive_site import testing

NERVOUS = Path(__file__).resolve().parents[3] / "nervous-system"
sys.path.insert(0, str(NERVOUS))

ADA_ID = "6a1c0000-0000-4000-8000-0000000000ad"
BOB_ID = "6a1c0000-0000-4000-8000-0000000000b0"
OLD, NEW = "ada-old-key-0123456789", "ada-new-key-0123456789"
BOB = "bob-own-key-0123456789"
ADMIN = {"comms": "gateway-admin-for-tests",
         "lucent": "memory-admin-for-tests"}  # secret-guard: allow - invented fixtures


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Hive:
    def __init__(self, env):
        self.env = env

    async def status(self, key):
        """What each service answers a key on its own authenticated route."""
        answers = {}
        for service in ("comms", "lucent"):
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
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN["comms"])

    # The gateway stand-in knows one administrator's value; the site presents
    # the signed-in person's key (story 905), so that person's key is it.
    async def _administrators_key(_person):
        return ADMIN["comms"]

    monkeypatch.setattr(holder_credentials, "_asking_person_key", _administrators_key)
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", ADMIN["lucent"])
    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.invalid:8899")

    apps = {
        "comms.test": httpx.ASGITransport(app=comms_server.app),
        "lucent.test": httpx.ASGITransport(app=create_app()),
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

    testing.run(seed())
    return Hive(shared)


async def _replace(key=NEW):
    return await holder_credentials.replace_mind_key("ada", "daniel", ADA_ID, key)


async def test_a_replaced_key_is_refused_by_all_three_services_on_the_next_request_and_bobs_keeps_working(hive):
    assert await hive.status(OLD) == {"comms": 200, "lucent": 200}

    result = await _replace()

    assert [s["service"] for s in result["services"]] == ["comms"]
    # Same process, same apps, nothing restarted.
    for service, code in (await hive.status(OLD)).items():
        assert code in (401, 403), service
    assert await hive.status(NEW) == {"comms": 200, "lucent": 200}
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200}
    assert mind_key_files.read("ada") == NEW
    assert mind_key_files.read("bob") == BOB




async def test_pasting_a_key_another_mind_holds_changes_nothing_and_both_minds_keep_theirs(hive):
    with pytest.raises(holder_credentials.KeyHeldByAnother):
        await _replace(BOB)

    assert await hive.status(OLD) == {"comms": 200, "lucent": 200}
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200}
    assert mind_key_files.read("ada") == OLD


async def test_a_cut_off_mind_stays_cut_off_when_a_replacement_fails_and_is_put_back(hive):
    await holder_credentials._record_revoke("ada")
    assert (await hive.status(OLD))["comms"] == 401
    hive.env.write_text(f"export MIND_PROXY_KEY={OLD}\n")  # a line the console cannot rewrite

    with pytest.raises(holder_credentials.MintRefused):
        await _replace()

    assert await hive.status(NEW) == {"comms": 401, "lucent": 401}
    assert await hive.status(OLD) == {"comms": 401, "lucent": 401}
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200}


async def test_a_key_a_service_already_accepts_is_refused_before_anything_changes(hive):
    from comms import user_access
    await user_access.put_credential("carol", "daniel", NEW, role="service")

    with pytest.raises(holder_credentials.KeyHeldByAnother):
        await _replace()

    assert await hive.status(OLD) == {"comms": 200, "lucent": 200}
    assert mind_key_files.read("ada") == OLD
    assert holder_credentials.key_journal.unfinished() == []






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
    assert await hive.status(OLD) == {"comms": 200, "lucent": 200}
    assert await hive.status(NEW) == {"comms": 401, "lucent": 401}


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
    assert await hive.status(OLD) == {"comms": 200, "lucent": 200}
    assert await hive.status(NEW) == {"comms": 401, "lucent": 401}


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


async def test_recovery_after_a_crash_also_cleans_the_saved_copies_of_the_old_key_and_the_journal_holds_no_key(hive, tmp_path):
    hive.env.write_text(f"MIND_PROXY_KEY={OLD}\nSERVICE_PROXY_KEY={OLD}\nOTHER={BOB}\n")
    backups = tmp_path / "backups"
    backups.mkdir()
    saved = backups / "hive_mind-MIND_PROXY_KEY-20261126T000000Z.env"
    saved.write_text(f"MIND_PROXY_KEY={OLD}\nOTHER={BOB}\n")
    real = holder_credentials._write_stored

    def stop_after_the_key_file(place, value, backup=True):
        written = real(place, value, backup)
        if place.is_key_file:
            raise KeyboardInterrupt  # the console is gone before the env lines
        return written

    holder_credentials._write_stored = stop_after_the_key_file
    try:
        with pytest.raises(KeyboardInterrupt):
            await _replace()
    finally:
        holder_credentials._write_stored = real
    assert OLD in saved.read_text()
    journal = " ".join(p.read_text() for p in key_journal.JOURNAL_DIR.glob("*.json"))
    assert OLD not in journal and NEW not in journal

    handled = await holder_credentials.recover_replacements()

    assert handled == [{"mind": "ada", "recovered": True}]
    assert _every_file_holding(tmp_path, OLD) == []
    assert f"OTHER={BOB}" in saved.read_text()


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
    assert await hive.status(NEW) == {"comms": 401, "lucent": 401}


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
