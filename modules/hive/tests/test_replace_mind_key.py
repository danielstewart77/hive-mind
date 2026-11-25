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
import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

import credentials_data
import holder_credentials
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
    shared.write_text(f"MIND_KEY_ADA={OLD}\nMIND_KEY_BOB={BOB}\nUNRELATED=keep\n")
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
    assert credentials_data._read_env_value(hive.env, "MIND_KEY_ADA") == NEW
    assert credentials_data._read_env_value(hive.env, "MIND_KEY_BOB") == BOB


async def test_a_replacement_the_tools_cannot_take_puts_the_old_key_back_on_the_others(hive):
    hive.tools.fail = True

    with pytest.raises(holder_credentials.MintRefused):
        await _replace()

    assert await hive.status(NEW) == {"comms": 401, "lucent": 401, "tools": 401}
    answers = await hive.status(OLD)
    assert answers["comms"] == 200 and answers["lucent"] == 200
    assert credentials_data._read_env_value(hive.env, "MIND_KEY_ADA") == OLD
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200, "tools": 404}
    assert holder_credentials.key_journal.unfinished() == []


async def test_pasting_a_key_another_mind_holds_changes_nothing_and_both_minds_keep_theirs(hive):
    with pytest.raises(holder_credentials.KeyHeldByAnother):
        await _replace(BOB)

    assert await hive.status(OLD) == {"comms": 200, "lucent": 200, "tools": 404}
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200, "tools": 404}
    assert credentials_data._read_env_value(hive.env, "MIND_KEY_ADA") == OLD
