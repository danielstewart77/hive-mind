"""Moving existing minds onto the proxy key each already holds, and retiring the old keys.

The gateway and the memory store are the real applications over ASGI against
the suite's scratch Postgres, so "refused" and "accepted" are their own
answers to a key. The tools run in another tree whose module names collide
with this console's, so they stand in at the transport (their own routes are
guarded by the tools' suite). The proxy is a stand-in that accepts the keys
this scenario gives it.
"""
import json

import httpx
import pytest

import configuration_data
import credentials_data
import holder_credentials
import key_migration
from hive_site import testing

from .test_replace_mind_key import (  # noqa: F401 - fixtures and constants
    ADA_ID, ADMIN, BOB_ID, ToolsStandIn, hive as _base_hive)  # noqa: F401
from .test_replace_mind_key import Hive

import asyncio
import sys
from pathlib import Path

NERVOUS = Path(__file__).resolve().parents[3] / "nervous-system"
sys.path.insert(0, str(NERVOUS))

CAROL_ID = "6a1c0000-0000-4000-8000-0000000000c0"
ADA_PROXY, BOB_PROXY = "ada-proxy-key-0123456789", "bob-proxy-key-0123456789"
ADA_COMMS, ADA_LUCENT = "ada-comms-old-0123456789", "ada-lucent-old-0123456789"
BOB_COMMS, BOB_LUCENT = "bob-comms-old-0123456789", "bob-lucent-old-0123456789"
SCHEDULER = "scheduler-key-0123456789"
EVERY_VALUE = (ADA_PROXY, BOB_PROXY, ADA_COMMS, ADA_LUCENT, BOB_COMMS, BOB_LUCENT, SCHEDULER)


class MigrationTools(ToolsStandIn):
    """The tools' stand-in, with the route that retires the shared token."""

    def __init__(self):
        super().__init__()
        self.retired = False
        self.services = {"hive-site", "scheduler"}

    def __call__(self, request):
        if request.url.path == "/credentials/shared-token/retire" and request.method == "POST":
            self.retired = True
            return httpx.Response(200, json={"holder": "skippy", "revoked": True})
        if request.url.path == "/credentials" and request.method == "GET":
            return httpx.Response(200, json=[
                {"holder": h, "role": "service" if h in self.services else "mind", "live": True}
                for h in self.values])
        return super().__call__(request)


@pytest.fixture
def old_hive(tmp_path, monkeypatch, builtin_console):
    from comms import server as comms_server, user_access
    from lucent_api import holder_access
    from lucent_api.server import create_app
    from hive_data import core

    shared = tmp_path / "hive_mind.env"
    shared.write_text(
        f"PROXY_TOKEN_ADA={ADA_PROXY}\nCOMMS_TOKEN_ADA={ADA_COMMS}\nLUCENT_TOKEN_ADA={ADA_LUCENT}\n"
        f"COMMS_TOKEN_BOB={BOB_COMMS}\nLUCENT_TOKEN_BOB={BOB_LUCENT}\nUNRELATED=keep\n")
    minds_dir = tmp_path / "minds"
    (minds_dir / "bob").mkdir(parents=True)
    (minds_dir / "bob" / "runtime.yaml").write_text(
        f"name: bob\nenv:\n  OPENAI_API_KEY: {BOB_PROXY}\n")
    files = {"hive_mind": shared}
    monkeypatch.setattr(credentials_data, "ENV_FILES", files)
    monkeypatch.setattr(holder_credentials, "ENV_FILES", files)
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(holder_credentials.key_journal, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", minds_dir)
    monkeypatch.setenv("GATEWAY_API_URL", "http://comms.test")
    monkeypatch.setenv("LUCENT_API_URL", "http://lucent.test")
    monkeypatch.setenv("HIVE_TOOLS_URL", "http://tools.test")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", ADMIN["comms"])
    monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", ADMIN["lucent"])
    monkeypatch.setenv("TOOLS_ADMIN_BEARER_TOKEN", ADMIN["tools"])
    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.invalid:8899")
    monkeypatch.setenv("HIVE_TOOLS_CONSUMERS", "hive-site")

    tools = MigrationTools()
    tools.values["hive-site"] = "hive-site-tools-key-0123456789"  # the console's own credential

    def proxy(request):
        key = request.headers.get("authorization", "").split(" ", 1)[-1]
        return httpx.Response(200 if key in (ADA_PROXY, BOB_PROXY) else 401, json={"data": []})

    apps = {
        "comms.test": httpx.ASGITransport(app=comms_server.app),
        "lucent.test": httpx.ASGITransport(app=create_app()),
        "tools.test": httpx.MockTransport(tools),
        "proxy.invalid": httpx.MockTransport(proxy),
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
        for mind_id, name in ((ADA_ID, "ada"), (BOB_ID, "bob"), (CAROL_ID, "carol")):
            await core.upsert_mind(
                mind_id=mind_id, name=name, gateway_url="http://mind.invalid:8420",
                model="m", harness="dsh", registered_at=1.0, last_seen=1.0, owner="daniel")
        for name, mind_id, comms, lucent in (("ada", ADA_ID, ADA_COMMS, ADA_LUCENT),
                                             ("bob", BOB_ID, BOB_COMMS, BOB_LUCENT)):
            await user_access.put_credential(name, "daniel", comms, role="mind")
            await holder_access.put_credential(name, "daniel", lucent, role="mind", mind_id=mind_id)
        await user_access.put_credential("scheduler", "daniel", SCHEDULER, role="service")

    testing.run(seed())
    return Hive(shared, tools, None)


async def _carol_already_on_one_key(hive):
    """Carol, made after the one-key change, so only Ada and Bob need moving."""
    from comms import user_access
    from lucent_api import holder_access
    key = "carol-one-key-0123456789"
    hive.env.write_text(hive.env.read_text() + f"MIND_KEY_CAROL={key}\n")
    await user_access.put_credential("carol", "daniel", key, role="mind")
    await holder_access.put_credential("carol", "daniel", key, role="mind", mind_id=CAROL_ID)
    hive.tools.values["carol"] = key
    return key


async def _answers(hive, key):
    return await hive.status(key)


def _refused(answers):
    return all(code in (401, 403) for code in answers.values())


async def test_moving_a_mind_registers_the_proxy_key_it_holds_on_all_three_services_and_leaves_bob_alone(old_hive):
    moved = await key_migration.move_mind("ada")

    assert moved["mind"] == "ada"
    assert await _answers(old_hive, ADA_PROXY) == {"comms": 200, "lucent": 200, "tools": 404}
    # The old per-service values are the ones replaced, so they stop at once.
    for old in (ADA_COMMS, ADA_LUCENT):
        answers = await _answers(old_hive, old)
        assert answers["comms"] in (401, 403) and answers["lucent"] in (401, 403)
    # Bob, untouched, keeps what he had.
    answers = await _answers(old_hive, BOB_COMMS)
    assert answers["comms"] == 200
    assert (await _answers(old_hive, BOB_LUCENT))["lucent"] == 200
    # The old lines are kept in their file until retirement; the one line is new.
    assert credentials_data._read_env_value(old_hive.env, "MIND_KEY_ADA") == ADA_PROXY
    assert credentials_data._read_env_value(old_hive.env, "COMMS_TOKEN_ADA") == ADA_COMMS
    assert credentials_data._read_env_value(old_hive.env, "MIND_KEY_BOB") is None


async def test_undoing_a_move_puts_the_mind_back_on_its_old_values_and_removes_the_one_key_registration(old_hive):
    await key_migration.move_mind("ada")

    undone = await key_migration.undo_move("ada")

    assert undone["mind"] == "ada"
    assert (await _answers(old_hive, ADA_COMMS))["comms"] == 200
    assert (await _answers(old_hive, ADA_LUCENT))["lucent"] == 200
    proxy_key = await _answers(old_hive, ADA_PROXY)
    assert proxy_key["comms"] in (401, 403) and proxy_key["lucent"] in (401, 403)
    assert proxy_key["tools"] == 401  # the tools registration is gone
    assert credentials_data._read_env_value(old_hive.env, "MIND_KEY_ADA") is None
    assert (await _answers(old_hive, BOB_COMMS))["comms"] == 200


async def test_a_move_the_tools_cannot_take_leaves_the_mind_on_its_old_values(old_hive):
    old_hive.tools.fail = True

    with pytest.raises(holder_credentials.MintRefused):
        await key_migration.move_mind("ada")

    assert (await _answers(old_hive, ADA_COMMS))["comms"] == 200
    assert (await _answers(old_hive, ADA_LUCENT))["lucent"] == 200
    assert credentials_data._read_env_value(old_hive.env, "MIND_KEY_ADA") is None


async def test_the_report_names_every_holder_and_mind_with_a_state_and_prints_no_value(old_hive):
    await key_migration.move_mind("ada")

    report = await key_migration.account()

    text = json.dumps(report)
    for value in EVERY_VALUE:
        assert value not in text
    states = {m["mind"]: m["state"] for m in report["minds"]}
    assert states == {"ada": "moved", "bob": "not moved", "carol": "not moved"}
    kinds = {h["holder"]: h["kind"]
             for service in report["services"] for h in service["holders"]}
    assert kinds["scheduler"] == "service"
    assert kinds["ada"] == "mind"
    assert {s["service"] for s in report["services"]} == {"comms", "lucent", "tools"}


async def test_the_report_names_a_mind_on_another_machine_and_a_mind_with_no_key_plainly(old_hive):
    report = await key_migration.account()

    carol = next(m for m in report["minds"] if m["mind"] == "carol")
    assert carol["state"] == "not moved"
    assert any("another machine" in p for p in carol["problems"])


async def test_two_minds_with_the_same_proxy_key_are_both_named_and_neither_is_moved(old_hive):
    old_hive.env.write_text(old_hive.env.read_text() + f"PROXY_TOKEN_BOB={ADA_PROXY}\n")

    report = await key_migration.account()

    by_name = {m["mind"]: m for m in report["minds"]}
    assert any("shares its proxy key with bob" in p for p in by_name["ada"]["problems"])
    assert any("shares its proxy key with ada" in p for p in by_name["bob"]["problems"])
    with pytest.raises(holder_credentials.KeyHeldByAnother):
        await key_migration.move_mind("ada")
    assert credentials_data._read_env_value(old_hive.env, "MIND_KEY_ADA") is None


async def test_a_mind_whose_files_hold_two_different_proxy_keys_is_not_moved(old_hive):
    old_hive.env.write_text(old_hive.env.read_text() + f"ADA_PROXY_KEY={BOB_PROXY}\n")

    with pytest.raises(holder_credentials.MintRefused):
        await key_migration.move_mind("ada")

    assert (await _answers(old_hive, ADA_COMMS))["comms"] == 200


async def test_a_mind_with_no_proxy_key_in_any_file_is_refused_the_move(old_hive):
    with pytest.raises(holder_credentials.MintRefused) as refused:
        await key_migration.move_mind("carol")

    assert "another machine" in str(refused.value) or "no proxy key" in str(refused.value)


async def test_a_holder_that_exists_on_one_service_and_not_the_others_is_named(old_hive):
    from lucent_api import holder_access
    await holder_access.put_credential("memory-only", "daniel", "memory-only-key-0123456789", role="service")

    report = await key_migration.account()

    assert any(item.startswith("memory-only (only on lucent") for item in report["names_on_some_services_only"])


async def test_retirement_refuses_while_any_mind_is_not_moved_naming_it_and_changes_nothing(old_hive):
    await key_migration.move_mind("ada")

    with pytest.raises(key_migration.RetirementRefused) as refused:
        await key_migration.retire()

    assert any("bob is not moved" in line for line in refused.value.blocking)
    assert credentials_data._read_env_value(old_hive.env, "COMMS_TOKEN_BOB") == BOB_COMMS
    assert (await _answers(old_hive, BOB_COMMS))["comms"] == 200
    assert old_hive.tools.retired is False


async def test_retirement_refuses_while_a_tools_consumer_has_no_credential_of_its_own(old_hive, monkeypatch):
    old_hive.tools.values.pop("hive-site")
    await key_migration.move_mind("ada")
    await key_migration.move_mind("bob")
    await _carol_already_on_one_key(old_hive)

    report = await key_migration.account()

    assert "hive-site" in report["tools_consumers_without_own_credential"]
    assert report["ready_to_retire"] is False


async def test_retirement_of_the_old_keys_leaves_service_holders_and_moved_minds_working(old_hive, monkeypatch):
    await key_migration.move_mind("ada")
    await key_migration.move_mind("bob")
    await _carol_already_on_one_key(old_hive)
    stack = old_hive.env.parent / "docker-compose.yml"
    stack.write_text("services: {}\n")
    monkeypatch.setattr(configuration_data, "stack_compose_path", lambda: stack)

    result = await key_migration.retire()

    assert result["retired"] is True
    assert old_hive.tools.retired is True
    for name in ("COMMS_TOKEN_ADA", "LUCENT_TOKEN_ADA", "COMMS_TOKEN_BOB", "LUCENT_TOKEN_BOB"):
        assert credentials_data._read_env_value(old_hive.env, name) is None
    assert credentials_data._read_env_value(old_hive.env, "UNRELATED") == "keep"
    assert await _answers(old_hive, ADA_PROXY) == {"comms": 200, "lucent": 200, "tools": 404}
    assert await _answers(old_hive, BOB_PROXY) == {"comms": 200, "lucent": 200, "tools": 404}
    assert (await _answers(old_hive, SCHEDULER))["comms"] == 200
    for old in (ADA_COMMS, BOB_COMMS):
        assert (await _answers(old_hive, old))["comms"] in (401, 403)
    assert "going back a release" in result["warning"]


# ---- who may reach the routes ------------------------------------------------

def test_the_migration_routes_answer_to_an_admin_or_the_operators_bearer_and_to_no_mind(
        builtin_console, monkeypatch):
    from starlette.testclient import TestClient
    import main

    monkeypatch.setenv("KEY_MIGRATION_TOKEN", "operator-bearer-0123456789")  # secret-guard: allow
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "a-minds-gateway-key-0123456789")  # secret-guard: allow
    seen = []

    async def report():
        seen.append("report")
        return {"minds": [], "services": [], "blocking": [], "ready_to_retire": True,
                "retirement_warning": ""}

    monkeypatch.setattr(key_migration, "account", report)
    admin = builtin_console.admin()
    sloan = builtin_console.settled_user(admin, "sloan", "sloans-own-pass")
    path = "/api/settings/key-migration"

    assert admin.get(path).status_code == 200
    assert sloan.get(path).status_code == 403
    anonymous = TestClient(main.app)
    for bearer in ("a-minds-gateway-key-0123456789", "wrong", ""):  # secret-guard: allow
        refused = anonymous.get(path, headers={"Authorization": f"Bearer {bearer}"})
        assert refused.status_code in (401, 403), bearer
    assert anonymous.post(path + "/retire", headers={
        "Authorization": "Bearer a-minds-gateway-key-0123456789"}).status_code in (401, 403)  # secret-guard: allow
    assert seen == ["report"]
    ok = anonymous.get(path, headers={"Authorization": "Bearer operator-bearer-0123456789"})  # secret-guard: allow
    assert ok.status_code == 200
    assert seen == ["report", "report"]


async def test_a_mind_cut_off_before_the_move_is_still_cut_off_after_it_and_after_undo(old_hive):
    for name in ("comms", "lucent"):
        await holder_credentials.revoke(holder_credentials.SERVICES[name], "ada")

    await key_migration.move_mind("ada")
    moved = await _answers(old_hive, ADA_PROXY)
    await key_migration.undo_move("ada")
    undone = await _answers(old_hive, ADA_COMMS)

    assert moved["comms"] in (401, 403) and moved["lucent"] in (401, 403)
    assert undone["comms"] in (401, 403) and undone["lucent"] in (401, 403)


async def test_issuing_a_tools_credential_never_replaces_one_a_holder_already_has(old_hive):
    old_hive.tools.values["scheduler"] = "scheduler-tools-key-0123456789"

    with pytest.raises(holder_credentials.MintRefused):
        await key_migration.issue_tools_credential("scheduler")

    assert old_hive.tools.values["scheduler"] == "scheduler-tools-key-0123456789"
    assert credentials_data._read_env_value(old_hive.env, "TOOLS_TOKEN_SCHEDULER") is None


async def test_retirement_keeps_a_line_when_the_stack_file_that_might_read_it_cannot_be_read(old_hive):
    await key_migration.move_mind("ada")
    await key_migration.move_mind("bob")
    await _carol_already_on_one_key(old_hive)

    result = await key_migration.retire()

    # No stack file exists in this scenario, so nothing may be deleted on a guess.
    assert result["old_lines_removed"] == []
    assert credentials_data._read_env_value(old_hive.env, "COMMS_TOKEN_ADA") == ADA_COMMS
