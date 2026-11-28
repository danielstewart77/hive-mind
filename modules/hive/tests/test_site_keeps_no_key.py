"""Story 913: the hive site and the terminal site take the sign-in key that
arrives with each click, use it for that request, and keep nothing; work that
needs a person who is not there waits and is finished with that person's key.

Everything goes through the real apps over HTTP and a recording stand-in for
the gateway, the memory store and the tools (`FakeComms`), which writes down
the credential each request carried.
"""
import os
import re
import types
from pathlib import Path

import httpx
import pytest
from hive_site import testing

import auth
import main
import mind_creation
import mind_rename
import route_access
from multiuser_support import SEED_PASSWORD, SEED_USERNAME

SERVICE_KEY = "a-service-key-that-stands-for-the-site"  # secret-guard: allow


def _key_of(client) -> str:
    return client.cookies.get(auth.SESSION_COOKIE_NAME)


def _everything_the_site_wrote(roots) -> list[tuple[str, bytes]]:
    found = []
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.is_file():
                found.append((str(path), path.read_bytes()))
    return found


def _module_state_holding(value: str) -> list[str]:
    """Every module-level name, anywhere in the site's own modules (one level
    into dicts, lists, sets, tuples and plain objects), that holds `value`."""
    hits = []
    own = ("main", "route_access", "auth", "mind_runtime", "mind_rename", "mind_creation",
           "design_delivery", "mind_soul", "holder_credentials", "key_journal",
           "console_access", "hive_site.route_access", "hive_site.auth", "hive_site.store")
    import sys

    def holds(obj, depth=0):
        if isinstance(obj, str):
            return value in obj
        if isinstance(obj, bytes):
            return value.encode() in obj
        if depth >= 3:
            return False
        if isinstance(obj, dict):
            return any(holds(k, depth + 1) or holds(v, depth + 1) for k, v in list(obj.items()))
        if isinstance(obj, (list, tuple, set, frozenset)):
            return any(holds(v, depth + 1) for v in list(obj))
        return False

    for name in own:
        module = sys.modules.get(name)
        if module is None:
            continue
        for attr, obj in list(vars(module).items()):
            if isinstance(obj, (types.ModuleType, type)) or callable(obj):
                continue
            if holds(obj):
                hits.append(f"{name}.{attr}")
    return hits


def _click_around(client):
    for path in ("/overview", "/minds", "/dashboard", "/usage", "/api/overview",
                 "/api/waiting-work", "/hive-errors", "/graph"):
        client.get(path, follow_redirects=False)


def test_after_signing_in_and_clicking_the_site_holds_the_key_nowhere_on_disk_or_in_memory(
        console, tmp_path, monkeypatch, fake_comms, capfd, caplog):
    monkeypatch.setenv("HIVE_KEY_ROOT", str(tmp_path / "keys"))
    (tmp_path / "keys").mkdir()
    # Nobody is acting in this test's own context (the suite's stand-in click
    # is set aside), so what is left after the requests is the site's own.
    route_access.begin_acting(None, None)
    admin = console.admin()
    key = _key_of(admin)
    _click_around(admin)
    admin.post("/api/minds/skippy/restart")
    admin.delete("/api/settings/hive-comms/sessions/s-skippy")

    carried = [c for c in fake_comms.seen if c["authorization"] == f"Bearer {key}"]
    assert carried, "nothing went out for the person: the test would prove nothing"

    on_disk = [path for path, data in _everything_the_site_wrote([tmp_path])
               if key.encode() in data]
    assert on_disk == []
    assert not any(key in value for value in os.environ.values())
    assert _module_state_holding(key) == []
    assert route_access.acting_key() is None
    out, err = capfd.readouterr()
    assert key not in out + err + caplog.text


#: What the site asks outside itself that carries no person's key on purpose:
#: the health probes (a public route, nothing is presented), and the rules page,
#: which is held (the board's decision, not this story's) and still reads the
#: memory store with the site's own key.
_UNAUTHENTICATED_PROBES = ("/health",)
_HELD_RULES_PAGE = ("/memory/list", "/memory/store")


def _every_get_route():
    for route in main.app.routes:
        methods = getattr(route, "methods", None)
        if methods and "GET" in methods and not route.path.startswith("/static"):
            yield re.sub(r"\{[^}:]+(:path)?\}", "x", route.path)


def test_every_call_the_site_makes_for_a_person_carries_that_persons_key_and_nothing_else(
        console, fake_comms, monkeypatch):
    monkeypatch.setenv("LUCENT_API_URL", fake_comms.url)
    monkeypatch.setenv("HIVE_TOOLS_URL", fake_comms.url)
    # The keys that used to stand for this site, present in the environment and
    # on disk: none may ever go out for a person.
    monkeypatch.setenv("LUCENT_BEARER_TOKEN", SERVICE_KEY)
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SERVICE_KEY)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", SERVICE_KEY)
    monkeypatch.setenv("HIVE_TOOLS_TOKEN", SERVICE_KEY)
    admin = console.admin()
    key = _key_of(admin)
    route_access.begin_acting(None, None)
    fake_comms.seen.clear()

    def walk(method, path, **kwargs):
        try:
            admin.request(method, path, follow_redirects=False, **kwargs)
        except Exception:  # noqa: BLE001 - a stand-in answering {} may break a page; the calls it saw are what counts
            pass

    for path in sorted(set(_every_get_route())):
        walk("GET", path)
    for method, path in (("POST", "/api/minds/skippy/restart"),
                         ("DELETE", "/api/settings/hive-comms/sessions/s-skippy"),
                         ("POST", "/approvals/x/decision"),
                         ("POST", "/api/canvas/review")):
        walk(method, path, json={})

    out = [c for c in fake_comms.seen
           if not c["path"].startswith(_UNAUTHENTICATED_PROBES)
           and not c["path"].startswith(_HELD_RULES_PAGE)]
    assert len(out) > 30, "the walk reached too little of the site to prove anything"
    wrong = [f"{c['method']} {c['path']} carried {c['authorization'][:20]!r}"
             for c in out if c["authorization"] != f"Bearer {key}"]
    assert wrong == []
    assert all(SERVICE_KEY not in c["authorization"] for c in fake_comms.seen
               if not c["path"].startswith(_HELD_RULES_PAGE))


def test_a_click_that_arrives_without_a_key_sends_nothing_out_and_falls_back_to_no_other_key(
        console, fake_comms, monkeypatch):
    monkeypatch.setenv("COMMS_BEARER_TOKEN", SERVICE_KEY)
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", SERVICE_KEY)
    admin = console.admin()
    route_access.begin_acting(None, None)
    fake_comms.seen.clear()
    admin.cookies.clear()

    for path in ("/api/overview", "/api/dashboard", "/graph/data", "/api/training/status"):
        response = admin.get(path, follow_redirects=False)
        assert response.status_code in (303, 401, 404), path

    assert [c for c in fake_comms.seen if not c["path"].startswith("/health")] == []
