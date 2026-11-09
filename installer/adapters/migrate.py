"""Deliverable A (shipped, hive-data PR #12): the schema check and apply.

As built, run in an image with hive_data installed (the comms image), with a
DSN reachable inside the hive network; `--dsn` defaults to HIVE_DATABASE_URL,
which is how the DSN is handed over here (never in an argv). One JSON object
on stdout:

    check  (read-only)  exit 0 ok, exit 2 problems:
        {"ok", "problems": [str], "recorded", "adopt", "pending": [{version, name}]}
    apply  exit 0 {"ok": true, "adopted", "applied", "version"}
           exit 2 refused, nothing written {"ok": false, "problems"}
           exit 1 a step failed {"ok": false, "failed": {version, name, error}, "applied"}

An unreachable database is exit 2 with the problem "cannot reach the
database: ...". One apply per database at a time (advisory lock).
"""

from __future__ import annotations

from installer.shell import Shell, StepFailed

MODULE = "hive_data.migrate"
REFUSED_EXIT = 2
FAILED_EXIT = 1

_SCRIPT = r'''
import os, subprocess, sys
env = dict(os.environ, HIVE_DATABASE_URL=PAYLOAD["dsn"])
try:
    p = subprocess.run([sys.executable, "-m", PAYLOAD["module"], PAYLOAD["verb"]], env=env,
                       capture_output=True, text=True, timeout=PAYLOAD["timeout"])
    print(json.dumps({"code": p.returncode, "stdout": p.stdout, "stderr": p.stderr[-4000:]}))
except subprocess.TimeoutExpired:
    print(json.dumps({"code": -1, "stdout": "", "stderr": "timed out"}))
'''


def _run(sh: Shell, verb: str, dsn: str, timeout: float) -> tuple[int, dict, str]:
    out = sh.in_network(_SCRIPT, {"module": MODULE, "verb": verb, "dsn": dsn, "timeout": timeout},
                        timeout=timeout + 60, redact=(dsn,))
    report: dict = {}
    for line in reversed((out.get("stdout") or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                report = json_loads(line)
                break
            except ValueError:
                continue
    stderr = (out.get("stderr") or "").replace(dsn, "***")
    return int(out.get("code", -1)), report, stderr


def json_loads(text: str) -> dict:
    import json
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def check(sh: Shell, dsn: str, timeout: float = 120) -> dict:
    """The check's report. Raises with every problem when the database cannot
    take the schema; nothing has been written either way."""
    code, report, stderr = _run(sh, "check", dsn, timeout)
    if code == 0:
        return report
    if code == REFUSED_EXIT:
        problems = report.get("problems") or ["refused"]
        raise StepFailed("the database cannot take the hive's schema: " + "; ".join(problems))
    raise StepFailed("the schema check could not run: " + " | ".join(stderr.strip().splitlines()[-4:]))


def apply(sh: Shell, dsn: str, timeout: float = 900) -> dict:
    code, report, stderr = _run(sh, "apply", dsn, timeout)
    if code == 0:
        return report
    if code == REFUSED_EXIT:
        raise StepFailed("the schema was refused, nothing written: "
                         + "; ".join(report.get("problems") or ["refused"]))
    if code == FAILED_EXIT and report.get("failed"):
        f = report["failed"]
        raise StepFailed(f"schema step {f.get('version')} {f.get('name')} failed: {f.get('error')}")
    raise StepFailed("applying the schema failed: " + " | ".join(stderr.strip().splitlines()[-6:]))
