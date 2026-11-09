"""Facts about the machine the installed hive takes instead of hard-coding.

The user and group the containers write as, the group that may use the Docker
socket, the time zone, the web proxy, and the address the page and the links
are printed with.
"""

from __future__ import annotations

import grp
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")


def _docker_gid() -> int:
    """The group owning the Docker socket, else the `docker` group, else ours."""
    try:
        return os.stat("/var/run/docker.sock").st_gid
    except OSError:
        pass
    try:
        return grp.getgrnam("docker").gr_gid
    except KeyError:
        return os.getgid()


def _timezone() -> str:
    tz = os.environ.get("TZ", "").strip()
    if tz:
        return tz
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def _web_proxy() -> dict[str, str]:
    """The host's web proxy, upper-case names; lower-case forms count too."""
    out: dict[str, str] = {}
    for name in PROXY_VARS:
        value = os.environ.get(name) or os.environ.get(name.lower()) or ""
        if value:
            out[name] = value
    return out


def address() -> str:
    """The address another machine on the network reaches this one at.

    Routing a UDP socket sends nothing; it only asks the kernel which local
    address it would leave from.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


@dataclass(frozen=True)
class Host:
    uid: int
    gid: int
    docker_gid: int
    tz: str
    web_proxy: dict = field(default_factory=dict)
    address: str = "127.0.0.1"

    @classmethod
    def detect(cls) -> "Host":
        return cls(uid=os.getuid(), gid=os.getgid(), docker_gid=_docker_gid(),
                   tz=_timezone(), web_proxy=_web_proxy(), address=address())

    def no_proxy(self, names) -> str:
        """The host's NO_PROXY plus every hive service, so a proxy that reaches
        the builds and the outside world never carries hive traffic."""
        parts = [p for p in (self.web_proxy.get("NO_PROXY") or "").split(",") if p.strip()]
        for name in ("localhost", "127.0.0.1", *names):
            if name not in parts:
                parts.append(name)
        return ",".join(parts)

    def proxy_env(self, names) -> dict[str, str]:
        """Web proxy settings for a build or a container; empty without one."""
        if not (self.web_proxy.get("HTTP_PROXY") or self.web_proxy.get("HTTPS_PROXY")):
            return {}
        env = {k: v for k, v in self.web_proxy.items() if k != "NO_PROXY"}
        env["NO_PROXY"] = self.no_proxy(names)
        for key in list(env):
            env[key.lower()] = env[key]
        return env
