"""Linux connector lifecycle. Host timers call this independently of the website."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import ipaddress
import json
import os
import pwd
import re
import secrets
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")


def next_shutoff(now: datetime) -> datetime:
    """Next 02:00 or 20:00 Central; a nonexistent spring 02:00 closes at 03:00."""
    local = now.astimezone(CENTRAL)
    candidates = [datetime.combine(local.date() + timedelta(days=day), datetime.min.time(), CENTRAL).replace(hour=hour).astimezone(timezone.utc)
                  for day in (0, 1, 2) for hour in (2, 20)]
    return min(value for value in candidates if value > now)


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(data, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def public_url(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    host = parsed.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Use a public DNS hostname for your Cloudflare address")
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.path not in ("", "/") or parsed.query or parsed.fragment or not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host) or "." not in host:
        raise ValueError("Enter a public HTTPS hostname, without a path or password")
    return "https://" + host.lower()


def token_credentials(token: str) -> dict:
    """Cloudflare's documented a/s/t connector token fields, as local credentials."""
    try:
        if not isinstance(token, str) or len(token) > 16384:
            raise ValueError()
        decoded = json.loads(base64.b64decode(token.strip(), validate=True))
        account = decoded["a"]
        secret = decoded["s"]
        tunnel_id = str(uuid.UUID(decoded["t"]))
        if not re.fullmatch(r"[a-fA-F0-9]{32}", account) or len(base64.b64decode(secret, validate=True)) < 32:
            raise ValueError()
        if decoded.get("e"):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError("Paste only the Cloudflare tunnel connector token, not an account API key or install command") from None
    return {"AccountTag": account, "TunnelSecret": secret, "TunnelID": tunnel_id}


def process_identity(pid: int) -> dict | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if raw[0] == "Z":
            return None
        return {"pid": pid, "start": raw[19], "exe": os.readlink(f"/proc/{pid}/exe")}
    except (OSError, IndexError):
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Manager:
    def __init__(self, root: Path, cloudflared: str, *, site_origin="http://127.0.0.1:8710",
                 terminal_origin="http://127.0.0.1:8711", gateway_origin="http://127.0.0.1:49310",
                 timer_prefix="hive-remote-access", now=None, connector_user=None, system=False):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cloudflared = cloudflared
        self.site_origin, self.terminal_origin, self.gateway_origin = site_origin, terminal_origin, gateway_origin
        self.timer_prefix = timer_prefix
        self.system = system
        self.connector_user = pwd.getpwnam(connector_user) if connector_user else None
        if self.connector_user and self.connector_user.pw_uid == 0:
            raise ValueError("The connector must use an unprivileged account")
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.children = {}
        with self.lock():
            if not (self.root / "verifier.key").exists():
                private_json(self.root / "verifier.key", {"key": secrets.token_urlsafe(48)})

    @contextmanager
    def lock(self):
        with (self.root / "lock").open("a") as handle:
            os.chmod(self.root / "lock", 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def user_dir(self, owner: str) -> Path:
        if not isinstance(owner, str) or not owner or len(owner) > 100:
            raise ValueError("Invalid Hive user")
        return self.root / "users" / hashlib.sha256(owner.encode()).hexdigest()[:32]

    def configurations(self):
        return [read_json(path) for path in (self.root / "users").glob("*/configuration.json")]

    def allowed(self):
        return read_json(self.root / "global.json").get("allowed", True) is True

    def timers_ready(self):
        try:
            result = subprocess.run(["systemctl", *( [] if self.system else ["--user"]), "is-active", *(self.timer_prefix + suffix for suffix in ("-2000.timer", "-0200.timer", "-deadline.timer"))], capture_output=True, text=True, timeout=5)
            return result.stdout.splitlines() == ["active", "active", "active"]
        except (OSError, subprocess.TimeoutExpired):
            return False

    def http_get(self, url):
        opener = urllib.request.build_opener(NoRedirect())
        try:
            if url.startswith("https://"):
                # Public probes must not turn a saved address into a LAN probe.
                host = urlsplit(url).hostname
                if any(not ipaddress.ip_address(row[4][0]).is_global for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)):
                    return 0, {}
            with opener.open(url, timeout=3) as response:
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as response:
            return response.code, dict(response.headers)
        except (OSError, urllib.error.URLError):
            return 0, {}

    def running(self, owner):
        child = self.children.get(owner)
        if child:
            child.poll()
        record = read_json(self.user_dir(owner) / "process.json")
        identity = process_identity(record.get("pid", 0)) if record else None
        return record if identity and all(record.get(key) == value for key, value in identity.items()) else None

    def configured(self, owner):
        return read_json(self.user_dir(owner) / "configuration.json")

    def configure(self, owner, body):
        site, terminal = public_url(body.get("website_url", "")), public_url(body.get("terminal_url", ""))
        if site == terminal:
            raise ValueError("Use separate website and terminal hostnames")
        folder = self.user_dir(owner)
        with self.lock():
            if self.running(owner):
                raise ValueError("Disarm your connection before changing its configuration")
            credentials = token_credentials(body["tunnel_token"]) if body.get("tunnel_token") else read_json(folder / "credentials.json")
            if not credentials:
                raise ValueError("Paste your tunnel connector token")
            for other in self.configurations():
                if other["owner"] != owner and (credentials["TunnelID"] == other["tunnel_id"] or {site, terminal} & {other["website_url"], other["terminal_url"]}):
                    raise ValueError("This tunnel or hostname is already assigned to another user")
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_json(folder / "credentials.json", credentials)
            private_json(folder / "configuration.json", {"owner": owner, "website_url": site, "terminal_url": terminal, "tunnel_id": credentials["TunnelID"]})
            # Locally managed ingress confines this connector to the user's
            # two addresses and the account-checking gateway, with no catch-all.
            private_json(folder / "cloudflared.json", {"tunnel": credentials["TunnelID"], "credentials-file": str(folder / "credentials.json"), "ingress": [
                {"hostname": urlsplit(url).hostname, "service": self.gateway_origin}
                for url in (site, terminal)] + [{"service": "http_status:404"}]})
        return self.status(owner)

    def status(self, owner):
        config = self.configured(owner)
        running = self.running(owner)
        return {"configured": bool(config), "token_saved": bool(config), "allowed": self.allowed(),
                "state": running.get("phase", "connecting") if running else "disarmed", "website_url": config.get("website_url", ""),
                "terminal_url": config.get("terminal_url", ""), "closes_at": running.get("closes_at") if running else None,
                "next_shutoff": next_shutoff(self.now()).isoformat(), "timezone": "America/Chicago"}

    def active(self, owner):
        record = self.running(owner)
        return bool(self.allowed() and record and datetime.fromisoformat(record["closes_at"]) > self.now())

    def arm(self, owner):
        folder = self.user_dir(owner)
        with self.lock():
            if not self.allowed():
                raise ValueError("Remote access is disabled globally by an admin")
            if not self.configured(owner):
                raise ValueError("Save your Cloudflare connection first")
            if not self.timers_ready():
                raise ValueError("The host shutoff timers are not running; remote access cannot be armed")
            if self.running(owner):
                return self.status(owner)
            with socket.socket() as metrics:
                metrics.bind(("127.0.0.1", 0))
                port = metrics.getsockname()[1]
            try:
                # Secrets travel through a private credential file, never argv
                # or a copied host environment (which could contain another token).
                with (folder / "connector.log").open("w") as output:
                    os.chmod(folder / "connector.log", 0o600)
                    descriptors = []
                    options = {}
                    config_path = str(folder / "cloudflared.json")
                    if self.connector_user:
                        # Inherited read-only descriptors let an isolated UID read
                        # this credential without granting it filesystem access.
                        credential_fd = os.memfd_create("hive-tunnel-credential")
                        descriptors.append(credential_fd)
                        os.write(credential_fd, (folder / "credentials.json").read_bytes())
                        os.lseek(credential_fd, 0, os.SEEK_SET)
                        os.fchown(credential_fd, self.connector_user.pw_uid, self.connector_user.pw_gid)
                        os.fchmod(credential_fd, 0o400)
                        config = read_json(folder / "cloudflared.json")
                        config["credentials-file"] = f"/proc/self/fd/{credential_fd}"
                        config_fd = os.memfd_create("hive-tunnel-config")
                        descriptors.append(config_fd)
                        os.write(config_fd, json.dumps(config).encode())
                        os.lseek(config_fd, 0, os.SEEK_SET)
                        config_path = f"/proc/self/fd/{config_fd}"
                        options = {"user": self.connector_user.pw_uid, "group": self.connector_user.pw_gid, "extra_groups": [], "pass_fds": descriptors}
                    try:
                        child = subprocess.Popen([self.cloudflared, "tunnel", "--no-autoupdate", "--config", config_path, "--metrics", f"127.0.0.1:{port}", "run"], stdout=output, stderr=output, start_new_session=True,
                            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}, **options)
                    finally:
                        for descriptor in descriptors:
                            os.close(descriptor)
                self.children[owner] = child
                time.sleep(0.1)
                identity = process_identity(child.pid)
                if identity is None:
                    child.wait(timeout=2)
                    raise ValueError("The Cloudflare connector exited; check your tunnel configuration")
                private_json(folder / "process.json", {**identity, "phase": "connecting", "closes_at": next_shutoff(self.now()).isoformat(), "metrics_port": port})
            except OSError:
                raise ValueError("The Cloudflare connector could not start on this host") from None
        # Probes hold no process lock: a host sweep or global disarm must be
        # able to terminate a connector while this request is still waiting.
        try:
            config = self.configured(owner)
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if not self.active(owner):
                    raise ValueError("Your connection closed while it was starting")
                if self.http_get(f"http://127.0.0.1:{port}/ready")[0] == 200:
                    break
                time.sleep(0.2)
            else:
                raise ValueError("Cloudflare did not connect; check your tunnel credential and network")
            for url in (self.site_origin, self.terminal_origin):
                if self.http_get(url + "/health")[0] != 200:
                    raise ValueError("The website or web terminal is not answering on this host")
            for url in (config["website_url"], config["terminal_url"]):
                protected = False
                for attempt in range(5):
                    code, headers = self.http_get(url)
                    headers = {key.lower(): value for key, value in headers.items()}
                    target = urlsplit(headers.get("location", "")).hostname or ""
                    if code in (302, 303, 307) and target.endswith(".cloudflareaccess.com"):
                        protected = True
                        break
                    if 200 <= code < 400:
                        break
                    if not self.active(owner):
                        break
                    time.sleep(0.5)
                if not protected:
                    raise ValueError("Cloudflare Access must protect both your website and terminal addresses; your tunnel was disarmed")
            if not self.active(owner):
                raise ValueError("Your connection closed while it was starting")
            with self.lock():
                record = self.running(owner)
                if not record or record["pid"] != child.pid or not self.active(owner):
                    raise ValueError("Your connection closed while it was starting")
                private_json(folder / "process.json", {**record, "phase": "armed"})
        except Exception:
            self.off(owner, expected_pid=child.pid)
            raise
        return self.status(owner)

    def off(self, owner, expected_pid=None):
        with self.lock():
            record = self.running(owner)
            if expected_pid is not None and record and record["pid"] != expected_pid:
                return self.status(owner)
            if record:
                try:
                    os.killpg(record["pid"], signal.SIGTERM)
                    until = time.monotonic() + 2
                    while self.running(owner) and time.monotonic() < until:
                        time.sleep(0.02)
                    if self.running(owner):
                        os.killpg(record["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if self.running(owner):
                    until = time.monotonic() + 2
                    while self.running(owner) and time.monotonic() < until:
                        time.sleep(0.02)
                    if self.running(owner):
                        raise ValueError("The connector could not be stopped; its state remains armed")
            (self.user_dir(owner) / "process.json").unlink(missing_ok=True)
        return self.status(owner)

    def off_all(self):
        errors = []
        for config in self.configurations():
            try:
                self.off(config["owner"])
            except ValueError:
                errors.append(config["owner"])
        if errors:
            raise ValueError("Some connectors could not be stopped; remote access remains blocked")

    def set_allowed(self, allowed):
        if not isinstance(allowed, bool):
            raise ValueError("Allow remote access must be true or false")
        with self.lock():
            private_json(self.root / "global.json", {"allowed": allowed})
        if not allowed:
            self.off_all()
        return {"allowed": self.allowed()}

    def sweep(self):
        expired = []
        for config in self.configurations():
            owner = config["owner"]
            if self.running(owner) and not self.active(owner):
                self.off(owner)
                expired.append(owner)
        return expired
