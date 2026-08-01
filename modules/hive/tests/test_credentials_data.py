"""Unit tests for credentials_data.py's env-file credential fan-out."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import credentials_data


def _write_env(path, **kv):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{k}={v}" for k, v in kv.items()) + "\n")


def _patch_env_files(monkeypatch, tmp_path, **files):
    """files: label -> Path. Overrides only the given labels; others point nowhere."""
    env_files = {label: tmp_path / f"missing-{label}.env" for label in credentials_data.ENV_FILES}
    env_files.update(files)
    monkeypatch.setattr(credentials_data, "ENV_FILES", env_files)
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")


def test_list_credentials_reports_in_sync_shared_secret(tmp_path, monkeypatch):
    nervous_system = tmp_path / "nervous_system.env"
    hive_mind = tmp_path / "hive_mind.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="same-value", COMMS_ADMIN_BEARER_TOKEN="x")
    _write_env(hive_mind, COMMS_BEARER_TOKEN="same-value")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system, hive_mind=hive_mind)

    rows = credentials_data.list_credentials()
    comms = next(r for r in rows if r["key"] == "comms_bearer_token")

    present = [loc for loc in comms["locations"] if loc["present"]]
    assert len(present) == 2
    assert comms["in_sync"] is True
    assert all("same-value" not in str(loc["preview"]) for loc in present)


def test_list_credentials_flags_out_of_sync(tmp_path, monkeypatch):
    nervous_system = tmp_path / "nervous_system.env"
    hive_mind = tmp_path / "hive_mind.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="value-a")
    _write_env(hive_mind, COMMS_BEARER_TOKEN="value-b")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system, hive_mind=hive_mind)

    rows = credentials_data.list_credentials()
    comms = next(r for r in rows if r["key"] == "comms_bearer_token")

    assert comms["in_sync"] is False


def test_list_credentials_marks_manual_only_entries_non_rotatable():
    rows = credentials_data.list_credentials()
    non_rotatable = {r["key"] for r in rows if not r["rotatable"]}

    assert "hive_tools_token" in non_rotatable
    assert "github_pat" in non_rotatable
    # Manual-only entries explain themselves; settable ones offer a set flow instead.
    assert all(
        r["manual_reason"] or r["settable"] for r in rows if not r["rotatable"]
    )


def test_rotate_credential_writes_new_value_to_every_location(tmp_path, monkeypatch):
    nervous_system = tmp_path / "nervous_system.env"
    hive_mind = tmp_path / "hive_mind.env"
    skippy = tmp_path / "skippy.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old", OTHER_KEY="untouched")
    _write_env(hive_mind, COMMS_BEARER_TOKEN="old")
    _write_env(skippy, COMMS_BEARER_TOKEN="old")
    _patch_env_files(
        monkeypatch, tmp_path,
        nervous_system=nervous_system, hive_mind=hive_mind, skippy=skippy,
    )

    result = credentials_data.rotate_credential("comms_bearer_token")

    new_comms = credentials_data._read_env_value(nervous_system, "COMMS_BEARER_TOKEN")
    assert new_comms != "old"
    assert credentials_data._read_env_value(hive_mind, "COMMS_BEARER_TOKEN") == new_comms
    assert credentials_data._read_env_value(skippy, "COMMS_BEARER_TOKEN") == new_comms
    assert credentials_data._read_env_value(nervous_system, "OTHER_KEY") == "untouched"
    assert len(result["written"]) == 3
    assert result["restart_targets"]


def test_rotate_credential_skips_files_missing_the_key(tmp_path, monkeypatch):
    nervous_system = tmp_path / "nervous_system.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system)

    result = credentials_data.rotate_credential("comms_bearer_token")

    assert result["written"] == [{"file": "nervous_system", "env_key": "COMMS_BEARER_TOKEN"}]


def test_rotate_credential_rejects_manual_only_key():
    with pytest.raises(ValueError, match="sqlite"):
        credentials_data.rotate_credential("hive_tools_token")


def test_rotate_credential_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown credential"):
        credentials_data.rotate_credential("not_a_real_key")


def test_rotate_credential_raises_when_key_absent_everywhere(tmp_path, monkeypatch):
    _patch_env_files(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="not found"):
        credentials_data.rotate_credential("comms_bearer_token")


def test_write_env_value_preserves_inode_and_permissions(tmp_path, monkeypatch):
    """The .env files are single-file bind mounts: replacing the inode breaks the mount."""
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")
    path = tmp_path / "secret.env"
    _write_env(path, TOKEN="old")
    path.chmod(0o600)
    inode_before = path.stat().st_ino

    assert credentials_data._write_env_value(path, "TOKEN", "new") is True
    assert credentials_data._read_env_value(path, "TOKEN") == "new"
    assert path.stat().st_ino == inode_before
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_write_env_value_backs_up_the_original_before_rewriting(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", backups)
    path = tmp_path / "secret.env"
    _write_env(path, TOKEN="old", OTHER="keep")

    credentials_data._write_env_value(path, "TOKEN", "new", label="skippy")

    written = list(backups.iterdir())
    assert len(written) == 1
    assert written[0].name.startswith("skippy-TOKEN-")
    assert "TOKEN=old" in written[0].read_text()
    assert "OTHER=keep" in written[0].read_text()


def _patch_github_files(monkeypatch, tmp_path, git_credentials=None, gh_hosts=None):
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")
    creds = tmp_path / ".git-credentials"
    hosts = tmp_path / "hosts.yml"
    if git_credentials is not None:
        creds.write_text(git_credentials)
    if gh_hosts is not None:
        hosts.write_text(gh_hosts)
    monkeypatch.setattr(credentials_data, "GIT_CREDENTIALS_PATH", creds)
    monkeypatch.setattr(credentials_data, "GH_HOSTS_PATH", hosts)
    return creds, hosts


_GIT_CREDENTIALS = "https://daniel:ghp_oldtoken@github.com\nhttps://x:y@huggingface.co\n"  # secret-guard: allow — invented fixture
_GH_HOSTS = (
    "github.com:\n"
    "    git_protocol: https\n"
    "    users:\n"
    "        daniel:\n"
    "            oauth_token: ghp_oldtoken\n"  # secret-guard: allow — invented fixture
    "    user: daniel\n"
    "    oauth_token: ghp_oldtoken\n"  # secret-guard: allow — invented fixture
)


def test_github_pat_status_reads_both_files(tmp_path, monkeypatch):
    _patch_github_files(monkeypatch, tmp_path, _GIT_CREDENTIALS, _GH_HOSTS)

    rows = credentials_data.list_credentials()
    pat = next(r for r in rows if r["key"] == "github_pat")

    assert pat["settable"] is True
    assert pat["rotatable"] is False
    assert pat["in_sync"] is True
    by_file = {loc["file"]: loc for loc in pat["locations"]}
    assert by_file["git_credentials"]["present"] is True
    assert by_file["git_credentials"]["preview"] == "...oken"
    assert by_file["gh_hosts"]["present"] is True
    assert by_file["gh_hosts"]["env_key"] == "oauth_token x2"


def test_github_pat_status_flags_out_of_sync(tmp_path, monkeypatch):
    _patch_github_files(
        monkeypatch, tmp_path,
        "https://daniel:ghp_different@github.com\n", _GH_HOSTS,  # secret-guard: allow — invented fixture
    )

    pat = next(r for r in credentials_data.list_credentials() if r["key"] == "github_pat")

    assert pat["in_sync"] is False


def test_set_github_pat_verifies_then_writes_both_files(tmp_path, monkeypatch):
    creds, hosts = _patch_github_files(monkeypatch, tmp_path, _GIT_CREDENTIALS, _GH_HOSTS)
    monkeypatch.setattr(credentials_data, "_verify_github_pat", lambda token: "danielstewart77")  # secret-guard: allow — invented fixture

    result = credentials_data.set_github_pat("ghp_newtoken")

    assert result["login"] == "danielstewart77"
    assert "https://daniel:ghp_newtoken@github.com" in creds.read_text()  # secret-guard: allow — invented fixture
    assert "https://x:y@huggingface.co" in creds.read_text()  # secret-guard: allow — invented fixture
    assert creds.read_text().count("ghp_newtoken") == 1
    assert hosts.read_text().count("oauth_token: ghp_newtoken") == 2
    assert "ghp_oldtoken" not in hosts.read_text()
    assert {w["file"] for w in result["written"]} == {"git_credentials", "gh_hosts"}
    backups = sorted(f.name for f in (tmp_path / "backups").iterdir())
    assert any(name.startswith("git_credentials-") for name in backups)
    assert any(name.startswith("gh_hosts-") for name in backups)


def test_set_github_pat_rejects_bad_token_before_writing(tmp_path, monkeypatch):
    creds, hosts = _patch_github_files(monkeypatch, tmp_path, _GIT_CREDENTIALS, _GH_HOSTS)

    def _reject(token):
        raise ValueError("GitHub rejected that token (401) — nothing was written")

    monkeypatch.setattr(credentials_data, "_verify_github_pat", _reject)

    with pytest.raises(ValueError, match="401"):
        credentials_data.set_github_pat("ghp_bogus")
    assert "ghp_oldtoken" in creds.read_text()
    assert "ghp_oldtoken" in hosts.read_text()


def test_set_github_pat_rejects_garbage_without_calling_github(tmp_path, monkeypatch):
    _patch_github_files(monkeypatch, tmp_path, _GIT_CREDENTIALS, _GH_HOSTS)

    def _boom(token):
        raise AssertionError("should not verify garbage")

    monkeypatch.setattr(credentials_data, "_verify_github_pat", _boom)

    with pytest.raises(ValueError, match="look like a token"):
        credentials_data.set_github_pat("  ")
    with pytest.raises(ValueError, match="look like a token"):
        credentials_data.set_github_pat("ghp_has space")


def test_set_github_pat_errors_when_no_file_has_an_entry(tmp_path, monkeypatch):
    _patch_github_files(monkeypatch, tmp_path)  # neither file exists
    monkeypatch.setattr(credentials_data, "_verify_github_pat", lambda token: "someone")

    with pytest.raises(ValueError, match="neither credential file"):
        credentials_data.set_github_pat("ghp_new")


def test_comms_bearer_note_distinguishes_it_from_the_admin_token():
    comms = next(
        r for r in credentials_data.list_credentials() if r["key"] == "comms_bearer_token"
    )
    assert "separate credential" in comms["note"]


def test_write_env_value_leaves_file_untouched_when_key_absent(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", backups)
    path = tmp_path / "secret.env"
    _write_env(path, OTHER="keep")

    assert credentials_data._write_env_value(path, "TOKEN", "new") is False
    assert path.read_text() == "OTHER=keep\n"
    assert not backups.exists()
