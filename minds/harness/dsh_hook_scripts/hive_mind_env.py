"""What every hook here shares: where this mind lives and how to reach the hive.

Each hook is copied into ``minds/<name>/.dsh/hooks/`` and runs from there, so
the mind's folder is two levels above this file and nothing names a host
path. The services are reached through the mind's own environment — the
harness hands its hooks the whole spawn environment, credentials included —
with the mind folder's ``.env`` filling only what that environment lacks.

Standard library only: a hook runs under whatever ``python3`` the container
has, never the mind server's venv.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

#: ``minds/<name>/`` — this file sits in ``minds/<name>/.dsh/hooks/``.
MIND_DIR = Path(__file__).resolve().parents[2]

#: The outcome dsh records for a turn refused for size. The gateway's
#: overflow path rotates that turn and re-sends its message.
CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"


def load_env() -> None:
    """Fill unset variables from the mind folder's ``.env``, if it has one."""
    path = MIND_DIR / ".env"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def state_dir() -> Path:
    """Where the rotation hooks keep per-conversation state.

    The directory ``minds.rotation_marker_api.state_dir`` resolves, so a
    marker the gateway asks the mind to drop is the marker staged here.
    """
    explicit = os.environ.get("AUTO_REMEMBER_LOG_DIR")
    return Path(explicit) if explicit else MIND_DIR / "data" / "auto-remember"


def log(name: str, message: str) -> None:
    """Append one line to this mind's hook log; a failed write is dropped."""
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(directory / "hooks.log", "a") as handle:
            handle.write(f"[{stamp}] {name}: {message}\n")
    except OSError:
        pass


def comms() -> tuple[str, str]:
    """The gateway's address and this mind's bearer for it."""
    return (os.environ.get("COMMS_URL", "").rstrip("/"),
            os.environ.get("COMMS_BEARER_TOKEN", ""))


def lucent() -> tuple[str, str]:
    """Memory's address and this mind's bearer for it."""
    url = os.environ.get("LUCENT_URL_SELF") or os.environ.get("LUCENT_URL") or ""
    return url.rstrip("/"), os.environ.get("LUCENT_BEARER_TOKEN", "")


def proxy() -> tuple[str, str]:
    """The inference proxy's root and this mind's key for it: the one key.

    ``INFERENCE_PROXY_URL`` / ``MIND_PROXY_KEY`` as the installer sets them,
    else the dsh variables naming the same proxy and key.
    """
    url = os.environ.get("INFERENCE_PROXY_URL") or os.environ.get("DSH_PROXY_BASE_URL") or ""
    url = url.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    key = os.environ.get("MIND_PROXY_KEY") or os.environ.get("DSH_API_KEY") or ""
    return url, key.strip()


def request(method: str, url: str, bearer: str, body: dict | None = None,
            timeout: float = 10) -> dict:
    """One JSON request; the decoded reply. Raises on transport or status."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode()
    parsed = json.loads(raw) if raw.strip() else {}
    return parsed if isinstance(parsed, dict) else {}


def detach() -> bool:
    """Fork into a session of our own; True in the child, False in the parent.

    The harness ends a hook's process group with its turn, so work that must
    outlive the turn runs in a new session with its stdio on /dev/null.
    A failed fork answers True: the work then runs inline rather than not at all.
    """
    try:
        pid = os.fork()
    except OSError:
        return True
    if pid > 0:
        return False
    try:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
    except OSError:
        pass
    return True


# ---------------------------------------------------------------------------
# The staged-rotation marker a terminal pane's fire hook reads
# ---------------------------------------------------------------------------

STATE_READY = "ready"
STATE_FIRING = "firing"

#: A staged rotation older than this belongs to a conversation nobody typed
#: into for a day; firing it then would surprise whoever comes back.
MARKER_TTL_SECONDS = 24 * 3600

#: How long a fire may hold its claim before another prompt may retry.
FIRE_LEASE_SECONDS = 120


def marker_path(conversation_id: str) -> Path:
    return state_dir() / f"rotation-staged-{conversation_id}.json"


def write_marker(conversation_id: str, **fields: object) -> None:
    path = marker_path(conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_text(json.dumps({"conversation_id": conversation_id,
                                "updated_at": time.time(), **fields}))
    os.replace(temp, path)


def read_marker(conversation_id: str) -> dict | None:
    try:
        marker = json.loads(marker_path(conversation_id).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict):
        return None
    if time.time() - float(marker.get("staged_at") or 0) > MARKER_TTL_SECONDS:
        clear_marker(conversation_id)
        return None
    return marker


def clear_marker(conversation_id: str) -> None:
    try:
        marker_path(conversation_id).unlink()
    except FileNotFoundError:
        pass
