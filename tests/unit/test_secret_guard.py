"""Unit tests for the pre-commit secret guard.

The guard's whole value is that it fires without anyone remembering it, so
the contract worth pinning is behavioural: a credential in an added line
blocks, a credential being *removed* does not, and no test in this file may
contain a real value — every fixture below is invented.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_PATH = REPO_ROOT / "tools" / "stateless" / "secret_guard" / "secret_guard.py"

# Token-shaped and not real, but deliberately *without* one of the
# FAKE_TOKENS markers — these fixtures have to reach the detector, and a
# marker would exempt them. The marker path gets its own test below.
FAKE_PAT = "ghp_0123456789abcdef0123456789abcdef0123"  # secret-guard: allow
FAKE_KEY = "sk-ant-api03-0123456789abcdef0123456789"  # secret-guard: allow


def _load_guard():
    spec = importlib.util.spec_from_file_location("secret_guard_cli", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["secret_guard_cli"] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo with an initial commit."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    # This host has the guard installed globally via core.hooksPath, and
    # several tests here commit a fixture credential on purpose. Point the
    # fixture repo at an empty hook directory so the suite tests the code
    # rather than the installation.
    empty_hooks = tmp_path / "no-hooks"
    empty_hooks.mkdir()
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "core.hooksPath", str(empty_hooks)], check=True
    )
    (tmp_path / "seed.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "seed"], check=True)
    return tmp_path


def _stage(repo: Path, name: str, body: str) -> None:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(body)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)


def test_a_staged_credential_is_found(repo):
    _stage(repo, "fixture.py", f'TOKEN = "{FAKE_PAT}"\n')
    findings = guard.scan_staged(cwd=str(repo))
    assert len(findings) == 1
    assert findings[0]["path"] == "fixture.py"
    assert findings[0]["line"] == 1


def test_the_finding_never_carries_the_value(repo):
    """A guard that leaks the secret to report it has solved nothing."""
    _stage(repo, "fixture.py", f'TOKEN = "{FAKE_PAT}"\n')
    serialized = json.dumps(guard.scan_staged(cwd=str(repo)))
    assert FAKE_PAT not in serialized
    assert "ghp_" in serialized


def test_ordinary_code_passes(repo):
    _stage(
        repo,
        "login.py",
        "def login(user: str, password: str) -> bool:\n"
        "    token = os.environ['GITHUB_TOKEN']\n"
        "    return bool(token)\n",
    )
    assert guard.scan_staged(cwd=str(repo)) == []


def test_removing_a_credential_is_not_blocked(repo):
    """Otherwise the fix for a leak is the one commit you cannot make."""
    _stage(repo, "leak.py", f'TOKEN = "{FAKE_PAT}"\n')
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "leak"], check=True)

    _stage(repo, "leak.py", "TOKEN = os.environ['GITHUB_TOKEN']\n")
    assert guard.scan_staged(cwd=str(repo)) == []


def test_a_value_that_announces_itself_as_fake_is_exempt(repo):
    """The exemption is a property of the value, never of the path.

    A detector needs token-shaped fixtures, but exempting `tests/` would
    reopen the exact hole this guard was built to close.
    """
    _stage(repo, "fixture.py", 'TOKEN = "ghp_INVENTEDfixture00000000000000000"\n')
    assert guard.scan_staged(cwd=str(repo)) == []


def test_a_real_looking_credential_in_a_test_file_still_blocks(repo):
    _stage(repo, "tests/test_thing.py", f'TOKEN = "{FAKE_PAT}"\n')
    assert len(guard.scan_staged(cwd=str(repo))) == 1


def test_an_allow_marker_exempts_a_line(repo):
    _stage(repo, "fixture.py", f'TOKEN = "{FAKE_PAT}"  # {guard.ALLOW_MARKER}\n')
    assert guard.scan_staged(cwd=str(repo)) == []


def test_line_numbers_survive_multiple_hunks(repo):
    _stage(repo, "big.py", "\n".join(f"line {n}" for n in range(1, 41)) + "\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "big"], check=True)

    body = [f"line {n}" for n in range(1, 41)]
    body[2] = f'early = "{FAKE_PAT}"'
    body[36] = f'late = "{FAKE_KEY}"'
    _stage(repo, "big.py", "\n".join(body) + "\n")

    lines = sorted(hit["line"] for hit in guard.scan_staged(cwd=str(repo)))
    assert lines == [3, 37]


def test_every_staged_file_is_scanned(repo):
    _stage(repo, "clean.py", "x = 1\n")
    _stage(repo, "dirty.py", f'TOKEN = "{FAKE_KEY}"\n')
    paths = {hit["path"] for hit in guard.scan_staged(cwd=str(repo))}
    assert paths == {"dirty.py"}


def test_scan_path_reads_the_working_tree(tmp_path):
    (tmp_path / "a.py").write_text(f'TOKEN = "{FAKE_PAT}"\n')
    (tmp_path / "b.py").write_text("x = 1\n")
    findings = guard.scan_paths([str(tmp_path)])
    assert len(findings) == 1
    assert findings[0]["path"].endswith("a.py")


def test_scan_path_skips_trees_nothing_is_committed_from(tmp_path):
    """An audit that walks a virtualenv never finishes."""
    (tmp_path / "src.py").write_text("x = 1\n")
    vendored = tmp_path / ".venv" / "lib"
    vendored.mkdir(parents=True)
    (vendored / "sample.py").write_text(f'TOKEN = "{FAKE_PAT}"\n')

    assert guard.scan_paths([str(tmp_path)]) == []


def test_binary_files_are_skipped(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
    assert guard.scan_paths([str(tmp_path)]) == []


def test_the_cli_exits_non_zero_on_a_finding(repo, monkeypatch, capsys):
    _stage(repo, "fixture.py", f'TOKEN = "{FAKE_PAT}"\n')
    monkeypatch.chdir(repo)
    assert guard.main(["scan-staged"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["count"] == 1


def test_the_cli_exits_zero_on_a_clean_diff(repo, monkeypatch, capsys):
    _stage(repo, "clean.py", "x = 1\n")
    monkeypatch.chdir(repo)
    assert guard.main(["scan-staged"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_quiet_mode_reports_to_stderr_without_the_value(repo, monkeypatch, capsys):
    """Hook output goes to the developer's terminal, so it must stay safe."""
    _stage(repo, "fixture.py", f'TOKEN = "{FAKE_PAT}"\n')
    monkeypatch.chdir(repo)
    assert guard.main(["scan-staged", "--quiet"]) == 1
    err = capsys.readouterr().err
    assert "fixture.py:1" in err
    assert FAKE_PAT not in err


def test_install_writes_hooks_and_points_git_at_them(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    hooks = home / ".git-hooks"
    report = guard.install(hooks)

    pre_commit = hooks / "pre-commit"
    assert pre_commit.exists()
    assert pre_commit.stat().st_mode & 0o111
    assert str(pre_commit) in report["written"]

    configured = subprocess.run(
        ["git", "config", "--global", "core.hooksPath"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert configured == str(hooks)


def test_install_chains_the_hooks_it_would_otherwise_shadow(tmp_path, monkeypatch):
    """core.hooksPath replaces .git/hooks, so a repo's own pre-push would die."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    hooks = home / ".git-hooks"
    guard.install(hooks)

    for name in guard.CHAINED_HOOKS:
        body = (hooks / name).read_text()
        assert f"hooks/{name}" in body
        assert "exec" in body


def test_the_installed_hook_blocks_a_real_commit(repo, tmp_path, monkeypatch):
    """End to end: the thing that should have stopped 2026-07-30."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    hooks = home / ".git-hooks"
    guard.install(hooks)

    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True
    )
    _stage(repo, "fixture.py", f'TOKEN = "{FAKE_PAT}"\n')

    blocked = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "oops"],
        capture_output=True,
        text=True,
    )
    assert blocked.returncode != 0
    assert FAKE_PAT not in blocked.stderr

    _stage(repo, "fixture.py", "TOKEN = os.environ['GITHUB_TOKEN']\n")
    allowed = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "fixed"],
        capture_output=True,
        text=True,
    )
    assert allowed.returncode == 0
