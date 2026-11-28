"""P1: real Hive account authentication never grants another tunnel's owner."""
import json
import threading
import time

import uvicorn
from remote_access.manager import Manager
from remote_access.service import make_control
from remote_access.tests.test_manager import token

import remote_access_data


def test_members_configure_only_their_own_tunnel_and_admin_alone_controls_global(builtin_console, tmp_path, monkeypatch):
    manager = Manager(tmp_path / "remote", "unused")
    monkeypatch.setenv("HIVE_REMOTE_ACCESS_DIR", str(manager.root))
    server = uvicorn.Server(uvicorn.Config(make_control(manager), uds=str(manager.root / "control.sock"), log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    until = time.monotonic() + 5
    while not server.started and time.monotonic() < until:
        time.sleep(.02)
    admin = builtin_console.admin()
    member = builtin_console.settled_user(admin, "alice", "alice-own-password")
    other = builtin_console.settled_user(admin, "bob", "bob-own-password")
    try:
        body = {"owner": "bob", "website_url": "https://hive.alice.example.com", "terminal_url": "https://terminal.alice.example.com", "tunnel_token": token("00000000-0000-4000-8000-000000000001")}
        saved = member.put("/api/settings/remote-access", json=body)
        assert saved.status_code == 200, saved.text
        assert manager.configured("alice")["website_url"] == body["website_url"]
        assert other.get("/api/settings/remote-access").json()["configured"] is False
        assert body["tunnel_token"] not in saved.text
        page = member.get("/settings/remote-access")
        assert page.status_code == 200
        assert 'id="remote-access-form"' in page.text
        assert 'id="remote-global-allowed"' not in page.text
        assert member.put("/api/settings/remote-access/global", json={"allowed": False}).status_code == 403
        assert admin.put("/api/settings/remote-access/global", json={"allowed": False}).status_code == 200
        assert member.post("/api/remote-access/on").status_code == 400
        assert other.get("/api/settings/remote-access").json()["allowed"] is False
    finally:
        server.should_exit = True
        thread.join(5)


def test_private_authorization_requires_real_owner_cookie_including_for_admin(builtin_console, tmp_path, monkeypatch):
    manager = Manager(tmp_path / "remote", "unused")
    monkeypatch.setenv("HIVE_REMOTE_ACCESS_DIR", str(manager.root))
    admin = builtin_console.admin()
    member = builtin_console.settled_user(admin, "alice", "alice-own-password")
    key = json.loads((manager.root / "verifier.key").read_text())["key"]
    headers = {"x-hive-remote-key": key}
    assert member.post("/api/remote-access/authorize", json={"owner": "alice"}).status_code == 403
    assert member.post("/api/remote-access/authorize", json={"owner": "alice"}, headers=headers).status_code == 200
    assert admin.post("/api/remote-access/authorize", json={"owner": "alice"}, headers=headers).status_code == 403
    assert member.post("/api/remote-access/authorize", json={"owner": "bob"}, headers=headers).status_code == 403
    remote_headers = {**headers, "x-hive-remote-owner": "alice", "x-hive-remote-terminal": "https://terminal.alice.example.com"}
    assert 'href="https://terminal.alice.example.com"' in member.get("/settings/remote-access", headers=remote_headers).text
