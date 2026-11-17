"""What the Python hooks here share: where this mind lives and where it logs.

The installer copies every hook into ``<harness home>/role-hooks/`` and each
one runs from there, so the harness home is one level above this file and the
mind's folder two. Nothing here names a host, a mind or a path.

Standard library only.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path

#: The harness home these hooks were installed into.
HOME = Path(__file__).resolve().parents[1]

#: The mind's folder: the harness home sits directly inside it.
MIND_DIR = Path(__file__).resolve().parents[2]


def state_dir() -> Path:
    """Where this mind's hooks log: its own directory when it names one."""
    explicit = os.environ.get("AUTO_REMEMBER_LOG_DIR")
    return Path(explicit) if explicit else MIND_DIR / "data" / "auto-remember"


def log(file_name: str, message: str) -> None:
    """Append one stamped line to a log in the state directory, best effort."""
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(directory / file_name, "a") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def load_env() -> None:
    """Fill unset variables from the mind folder's ``.env``, if it has one."""
    try:
        lines = (MIND_DIR / ".env").read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value and value[0] not in ('"', "'") and "#" in value:
            value = value[: value.find("#")].rstrip()
        os.environ.setdefault(key.strip(), value.strip('"').strip("'"))


def transcript(event: dict, harness: str) -> str:
    """The conversation a Stop payload names, as a path this hook can read.

    dsh keeps a compressed log its own reader has to unpack; Claude and Codex
    name a file that is read as it is.
    """
    if harness == "dsh":
        import dsh_transcript
        return str(dsh_transcript.resolve(event) or "")
    return str((event or {}).get("transcript_path") or "")


def wait_for_quiescence(path: Path, *, interval: float = 1.0,
                        stable_checks: int = 3, max_wait: float = 120.0) -> None:
    """Block until the file's size and mtime hold steady, capped at max_wait."""
    import time
    try:
        st = path.stat()
        last = (st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        return
    stable = 0
    waited = 0.0
    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        try:
            st = path.stat()
            cur = (st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            return
        if cur == last:
            stable += 1
            if stable >= stable_checks:
                return
        else:
            stable = 0
            last = cur


def detach() -> bool:
    """Fork into a session of our own; True in the child, False in the parent.

    When the fork is refused the caller does the work in the hook itself.
    """
    try:
        pid = os.fork()
    except OSError:
        return True
    if pid > 0:
        return False
    os.setsid()
    return True
