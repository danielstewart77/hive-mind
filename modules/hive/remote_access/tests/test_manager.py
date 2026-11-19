"""Per-user connector isolation and independent expiration (P1/P2)."""
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from remote_access import manager


def token(tunnel_id):
    return base64.b64encode(json.dumps({"a": "a" * 32, "s": base64.b64encode(b"secret" * 6).decode(), "t": tunnel_id}).encode()).decode()


@pytest.fixture
def connectors(tmp_path, monkeypatch):
    executable = tmp_path / "cloudflared"
    executable.write_text("#!" + sys.executable + "\n" + '''
import json,sys,time
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
args=sys.argv[1:]
config=json.loads(Path(args[args.index('--config')+1]).read_text())
Path(config['credentials-file']).with_suffix('.arguments').write_text(json.dumps(args))
host,port=args[args.index('--metrics')+1].split(':')
class Ready(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self): self.send_response(200 if self.path=='/ready' else 404); self.end_headers()
HTTPServer((host,int(port)),Ready).serve_forever()
''')
    executable.chmod(0o700)
    clock = [datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)]
    instance = manager.Manager(tmp_path / "state", str(executable), now=lambda: clock[0])
    monkeypatch.setattr(instance, "timers_ready", lambda: True)
    original = instance.http_get
    def transport(url):
        if url.startswith("https://"):
            return 302, {"location": "https://personal.cloudflareaccess.com/cdn-cgi/access/login"}
        if url in (instance.site_origin + "/health", instance.terminal_origin + "/health"):
            return 200, {}
        return original(url)
    monkeypatch.setattr(instance, "http_get", transport)
    yield instance, clock
    instance.off_all()


def configure(instance, owner, number):
    return instance.configure(owner, {"website_url": f"https://hive.{owner}.example.com",
        "terminal_url": f"https://terminal.{owner}.example.com",
        "tunnel_token": token(f"00000000-0000-4000-8000-{number:012d}")})


def test_each_user_stops_only_their_own_connector_and_keeps_credentials_private(connectors):
    instance, _ = connectors
    configure(instance, "alice", 1)
    configure(instance, "bob", 2)
    assert instance.arm("alice")["state"] == "armed"
    assert instance.arm("bob")["state"] == "armed"
    assert instance.off("alice")["state"] == "disarmed"
    assert instance.status("bob")["state"] == "armed"
    state = instance.status("alice")
    assert state["website_url"] == "https://hive.alice.example.com"
    assert "tunnel_token" not in json.dumps(state)
    args = json.loads((instance.user_dir("alice") / "credentials.arguments").read_text())
    assert token("00000000-0000-4000-8000-000000000001") not in json.dumps(args)
    actual = json.loads((instance.user_dir("alice") / "cloudflared.json").read_text())
    assert [row.get("hostname") for row in actual["ingress"][:2]] == ["hive.alice.example.com", "terminal.alice.example.com"]
    assert all(row["service"] == instance.gateway_origin for row in actual["ingress"][:2])
    assert (instance.user_dir("alice") / "credentials.json").stat().st_mode & 0o777 == 0o600


def test_expiration_closes_processes_without_a_website_timer(connectors):
    instance, clock = connectors
    configure(instance, "alice", 1)
    result = instance.arm("alice")
    assert result["closes_at"] == "2026-11-19T02:00:00+00:00"
    clock[0] = datetime(2026, 11, 19, 2, 0, tzinfo=timezone.utc)
    assert instance.sweep() == ["alice"]
    assert instance.status("alice")["state"] == "disarmed"


@pytest.mark.parametrize("current,expected", [
    ("2026-11-19T06:30:00+00:00", "2026-11-19T08:00:00+00:00"),
    ("2026-11-19T08:00:00+00:00", "2026-11-20T02:00:00+00:00"),
    ("2026-03-08T05:00:00+00:00", "2026-03-08T08:00:00+00:00"),
])
def test_next_shutoff_uses_central_time_including_midnight_and_dst(current, expected):
    assert manager.next_shutoff(datetime.fromisoformat(current)).isoformat() == expected


def test_global_disable_closes_every_user_and_blocks_rearming(connectors):
    instance, _ = connectors
    configure(instance, "alice", 1)
    configure(instance, "bob", 2)
    instance.arm("alice")
    instance.arm("bob")
    assert instance.set_allowed(False)["allowed"] is False
    assert instance.status("alice")["state"] == "disarmed"
    assert instance.status("bob")["state"] == "disarmed"
    with pytest.raises(ValueError, match="disabled"):
        instance.arm("alice")
    instance.set_allowed(True)
    assert instance.status("alice")["state"] == "disarmed"
    assert instance.arm("alice")["state"] == "armed"


def test_an_unprotected_terminal_rolls_back_the_whole_users_tunnel(connectors, monkeypatch):
    instance, _ = connectors
    configure(instance, "alice", 1)
    transport = instance.http_get
    monkeypatch.setattr(instance, "http_get", lambda url: (200, {}) if url == "https://terminal.alice.example.com" else transport(url))
    with pytest.raises(ValueError, match="Cloudflare Access"):
        instance.arm("alice")
    assert instance.status("alice")["state"] == "disarmed"


def test_hosts_and_tunnel_credentials_cannot_be_shared_between_users(connectors):
    instance, _ = connectors
    configure(instance, "alice", 1)
    with pytest.raises(ValueError, match="another user"):
        instance.configure("bob", {"website_url": "https://hive.bob.example.com", "terminal_url": "https://terminal.bob.example.com", "tunnel_token": token("00000000-0000-4000-8000-000000000001")})
    with pytest.raises(ValueError, match="another user"):
        instance.configure("bob", {"website_url": "https://hive.alice.example.com", "terminal_url": "https://terminal.bob.example.com", "tunnel_token": token("00000000-0000-4000-8000-000000000002")})
