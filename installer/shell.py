"""Running a command with a deadline, and docker from inside the hive.

Every command has a deadline. A libpq connect with no timeout against a paused
container, a `docker pull` behind a proxy that swallows the connection, an
image build waiting on a mirror — each looks like a step still working, for
ever. So nothing here waits without a bound, and a bound passed is a failure
that says what was running.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

NETWORK = "hivemind"
#: The image every in-network check runs in: it has python and asyncpg.
PROBE_IMAGE = "hive/comms:local"


class StepFailed(RuntimeError):
    """A step that cannot go on, with the sentence the page shows."""


class Shell:
    def __init__(self, log_path: Path | None = None, deadline: float | None = None):
        self.log_path = log_path
        self.deadline = deadline

    def log(self, text: str) -> None:
        if self.log_path is None:
            return
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {text}\n")

    def _bound(self, timeout: float) -> float:
        if self.deadline is None:
            return timeout
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise StepFailed("the step ran out of time")
        return min(timeout, left)

    def run(self, argv: list[str], *, timeout: float = 60, input: str | None = None,
            cwd: str | os.PathLike | None = None, env: dict | None = None,
            check: bool = True, redact: tuple[str, ...] = (),
            quiet: bool = False) -> subprocess.CompletedProcess:
        """`quiet` keeps the command's output out of the log: for a command
        that prints a secret it has just made (a proxy key, a tools token)."""
        shown = " ".join(argv)
        for secret in redact:
            if secret:
                shown = shown.replace(secret, "***")
        self.log(f"$ {shown}")
        bound = self._bound(timeout)
        if argv[:2] == ["docker", "compose"]:
            from installer.compose import compose_env
            full_env = {**compose_env(), **(env or {})}
        else:
            full_env = {**os.environ, **(env or {})} if env else None
        try:
            proc = subprocess.run(
                argv, input=input, capture_output=True, text=True, timeout=bound,
                cwd=cwd, env=full_env,
            )
        except subprocess.TimeoutExpired as exc:
            raise StepFailed(f"`{argv[0]} {argv[1] if len(argv) > 1 else ''}` took longer "
                             f"than {int(bound)}s") from exc
        except FileNotFoundError as exc:
            raise StepFailed(f"{argv[0]} is not installed") from exc
        out = (proc.stdout or "") + (proc.stderr or "")
        for secret in redact:
            if secret:
                out = out.replace(secret, "***")
        if out.strip() and not quiet:
            self.log(out.rstrip()[-4000:])
        if check and proc.returncode != 0:
            tail = out.strip().splitlines()[-6:]
            raise StepFailed(f"`{shown[:120]}` failed (exit {proc.returncode}): "
                             + " | ".join(tail))
        return proc

    def in_network(self, script: str, payload: dict, *, timeout: float = 30,
                   redact: tuple[str, ...] = (), image: str = PROBE_IMAGE) -> dict:
        """Run a python script on the hive's network and read its JSON answer.

        What a container can reach is not what the host can: `localhost`
        in an address the operator typed is the container itself, and a host
        firewall may pass the host and drop the bridge. So every reachability
        check runs where the services will run.

        The payload rides in on stdin with the script, never in argv: a
        password or a bearer on a `docker run` command line is readable by
        every user of the host in `ps`.
        """
        source = f"import json\nPAYLOAD = json.loads({json.dumps(json.dumps(payload))})\n{script}"
        proc = self.run(
            ["docker", "run", "--rm", "-i", "--network", NETWORK,
             "--entrypoint", "python", image, "-"],
            timeout=timeout, input=source, check=False, redact=redact, quiet=True,
        )
        lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
        try:
            return json.loads(lines[-1])
        except (IndexError, ValueError) as exc:
            tail = " | ".join(((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-4:])
            for secret in redact:
                tail = tail.replace(secret, "***") if secret else tail
            raise StepFailed(f"a check inside the hive network gave no answer: {tail}") from exc


#: An HTTP request made from inside the network.
HTTP_SCRIPT = r'''
import urllib.request, urllib.error
import os
method, url, headers, body, timeout = (PAYLOAD[k] for k in ("method", "url", "headers", "body", "timeout"))
# The web proxy the services themselves run with, NO_PROXY included: a check
# that bypassed it would pass for a service whose own traffic is sent to it.
os.environ.update(PAYLOAD.get("proxy_env") or {})
data = json.dumps(body).encode() if body is not None else None
req = urllib.request.Request(url, data=data, method=method, headers=headers)
opener = urllib.request.build_opener()
try:
    with opener.open(req, timeout=timeout) as r:
        print(json.dumps({"status": r.status, "body": r.read().decode("utf-8", "replace")}))
except urllib.error.HTTPError as e:
    print(json.dumps({"status": e.code, "body": e.read().decode("utf-8", "replace")}))
except Exception as e:
    print(json.dumps({"status": 0, "error": f"{type(e).__name__}: {e}"}))
'''


def http_in_network(sh: Shell, method: str, url: str, *, headers: dict | None = None,
                    body: dict | None = None, timeout: float = 15,
                    proxy_env: dict | None = None) -> dict:
    """{status, body|error} for one request made from inside the network."""
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    redact = tuple(v.split(" ", 1)[-1] for k, v in hdrs.items() if k.lower() == "authorization")
    return sh.in_network(
        HTTP_SCRIPT,
        {"method": method, "url": url, "headers": hdrs, "body": body, "timeout": timeout,
         "proxy_env": proxy_env or {}},
        timeout=timeout + 30, redact=redact,
    )
