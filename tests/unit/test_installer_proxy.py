"""The proxy step's calls to the proxy's own command."""

from __future__ import annotations

import json
import subprocess

import pytest

from installer import steps
from installer.adapters import proxy
from installer.shell import Shell, StepFailed


class _Recorded:
    """Where the process stops being ours: subprocess.run."""

    def __init__(self, stdout: dict, code: int = 0):
        self.calls: list[tuple[list[str], dict]] = []
        self.stdout, self.code = stdout, code

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("env") or {}))
        return subprocess.CompletedProcess(argv, self.code, json.dumps(self.stdout), "")


def test_the_provider_key_reaches_the_proxy_in_the_environment_never_in_argv(monkeypatch):
    run = _Recorded({"created": True, "updated": False})
    monkeypatch.setattr(subprocess, "run", run)

    proxy.add_provider(Shell(), ["docker", "compose"], name="anthropic", kind="anthropic",
                       api_key="invented-upstream-value", update=True)

    argv, env = run.calls[0]
    assert "invented-upstream-value" not in " ".join(argv)
    assert env[proxy.KEY_ENV] == "invented-upstream-value"
    assert argv[argv.index("-e") + 1] == proxy.KEY_ENV
    assert argv[argv.index("--api-key-env") + 1] == proxy.KEY_ENV
    assert "--update" in argv


def test_a_refused_test_completion_with_401_says_the_key_was_refused(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _Recorded(
        {"error": "invalid x-api-key", "status": 401}, code=2))

    with pytest.raises(StepFailed, match=r"refused the key.*401.*invalid x-api-key"):
        proxy.check_model(Shell(), ["docker", "compose"], model="claude-sonnet-5")


class _Outputs:
    def __init__(self, recorded):
        self.recorded = dict(recorded)

    def output(self, key, value=None):
        if value is None:
            return self.recorded.get(key)
        self.recorded[key] = value
        return value


@pytest.mark.parametrize("recorded, expect_update", [
    ({}, True),                                   # never confirmed: rewrite it
    ({"provider:x": {"value": "old"}}, True),     # changed since: rewrite it
    ({"provider:x": {"value": "now"}}, False),    # unchanged: leave it
])
def test_an_existing_row_is_rewritten_only_when_what_was_given_has_changed(
        recorded, expect_update):
    asked = []

    def add(update):
        asked.append(update)
        return {"created": False, "updated": update}

    assert steps._put_row(_Outputs(recorded), "provider:x", "now", add) is expect_update
    assert asked == ([False, True] if expect_update else [False])
