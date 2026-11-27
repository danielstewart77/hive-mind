"""Story 900: the console sends every change to a mind through the gateway,
with the signed-in person's own key and nothing else (requirements 23, 24, 28).

The far side (the gateway) is a transport that records what it was sent; the
registry and the key store are the boundaries faked, the hop under test is real.
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import mind_runtime

MIND_ID = "11111111-1111-4111-8111-111111111111"
REGISTRY = [{"id": MIND_ID, "name": "alpha", "owner": "daniel", "gateway_url": "http://alpha:8420"}]


@pytest.fixture
def world(monkeypatch):
    import holder_credentials
    import route_access

    monkeypatch.setenv("GATEWAY_API_URL", "http://gateway.test:8424")
    monkeypatch.setattr(route_access, "registered_minds", AsyncMock(return_value=REGISTRY))
    keys = {"daniel": "daniel-person-key", "sloan": "sloan-person-key"}  # secret-guard: allow

    async def user_key(name):
        return keys.get(name)

    monkeypatch.setattr(holder_credentials, "user_key", user_key)
    sent = []
    reply = {"response": httpx.Response(200, json={"configuration": {}}, headers={"x-hive-forwarded": "1"})}

    def handler(request):
        sent.append(request)
        return reply["response"]

    class W:
        pass

    w = W()
    w.sent, w.reply, w.keys = sent, reply, keys
    w.client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))

    tokens = []

    def as_person(name, admin=False):
        tokens.append((mind_runtime._PERSON.set(name), mind_runtime._PERSON_IS_ADMIN.set(admin)))

    w.as_person = as_person
    yield w
    for person, admin in reversed(tokens):
        mind_runtime._PERSON_IS_ADMIN.reset(admin)
        mind_runtime._PERSON.reset(person)


def _send(w, method="GET", url="http://alpha:8420/runtime", **kw):
    async def go():
        async with w.client() as client:
            return await mind_runtime.send(client, method, url, **kw)

    return asyncio.run(go())


def test_the_owner_s_change_goes_to_the_gateway_with_their_own_key_and_never_to_the_mind(world):
    world.as_person("daniel")
    _send(world, "PATCH", "http://alpha:8420/runtime", json={"default_model": "opus"})
    (request,) = world.sent
    assert str(request.url) == f"http://gateway.test:8424/broker/minds/{MIND_ID}/forward/runtime"
    assert request.headers["authorization"] == "Bearer daniel-person-key"
    assert request.url.host != "alpha"


def test_an_administrator_changes_a_mind_they_do_not_own_with_their_own_key(world):
    world.as_person("sloan", admin=True)
    _send(world)
    assert world.sent[0].headers["authorization"] == "Bearer sloan-person-key"


def test_a_person_who_is_neither_owner_nor_administrator_is_refused_before_anything_is_sent(world):
    world.as_person("sloan")
    with pytest.raises(HTTPException) as refused:
        _send(world)
    assert refused.value.status_code == 403
    assert world.sent == []


def test_with_nobody_signed_in_nothing_is_sent(world):
    with pytest.raises(HTTPException) as refused:
        _send(world)
    assert refused.value.status_code == 403
    assert world.sent == []


def test_a_person_whose_key_cannot_be_had_gets_a_sentence_not_a_fallback(world):
    world.keys.pop("daniel")
    world.as_person("daniel")
    os.environ["COMMS_ADMIN_BEARER_TOKEN"] = "an-admin-credential"  # secret-guard: allow
    try:
        with pytest.raises(HTTPException) as refused:
            _send(world)
    finally:
        del os.environ["COMMS_ADMIN_BEARER_TOKEN"]
    assert refused.value.status_code == 503
    assert world.sent == []


def test_the_gateways_own_refusals_never_become_a_401_that_signs_the_person_out(world):
    world.as_person("daniel")
    world.reply["response"] = httpx.Response(401, json={"detail": "Invalid token"})
    with pytest.raises(HTTPException) as refused:
        _send(world)
    assert refused.value.status_code == 502


def test_an_old_gateway_is_not_blamed_on_the_mind(world):
    world.as_person("daniel")
    world.reply["response"] = httpx.Response(404, json={"detail": "Not Found"})
    with pytest.raises(HTTPException) as refused:
        _send(world)
    assert refused.value.status_code == 502
    assert "gateway" in refused.value.detail and "mind" not in refused.value.detail.split("gateway")[0]


def test_a_mind_s_own_404_is_passed_on_as_the_mind_s_answer(world):
    world.as_person("daniel")
    world.reply["response"] = httpx.Response(404, json={"error": "no such file"}, headers={"x-hive-forwarded": "1"})
    assert _send(world).status_code == 404


def test_no_answer_is_not_known_whether_applied_and_a_down_gateway_could_not_be_reached(world, monkeypatch):
    world.as_person("daniel")

    def timeout(request):
        raise httpx.ReadTimeout("slow")

    async def go(handler):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await mind_runtime.send(client, "GET", "http://alpha:8420/runtime")

    with pytest.raises(HTTPException) as slow:
        asyncio.run(go(timeout))
    assert slow.value.status_code == 504 and "not known" in slow.value.detail

    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(HTTPException) as gone:
        asyncio.run(go(down))
    assert gone.value.status_code == 502 and "gateway could not be reached" in gone.value.detail
