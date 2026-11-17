"""Unit tests for credentials_data.py's env-file credential fan-out."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import credentials_data
import holder_credentials


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
    skippy = tmp_path / "skippy.env"
    _write_env(nervous_system, COMMS_ADMIN_BEARER_TOKEN="same-value")
    _write_env(skippy, COMMS_ADMIN_BEARER_TOKEN="same-value")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system, skippy=skippy)

    rows = credentials_data.list_credentials()
    admin = next(r for r in rows if r["key"] == "comms_admin_bearer_token")

    present = [loc for loc in admin["locations"] if loc["present"]]
    assert len(present) == 2
    assert admin["in_sync"] is True
    assert all("same-value" not in str(loc["preview"]) for loc in present)


def test_list_credentials_flags_out_of_sync(tmp_path, monkeypatch):
    nervous_system = tmp_path / "nervous_system.env"
    skippy = tmp_path / "skippy.env"
    _write_env(nervous_system, COMMS_ADMIN_BEARER_TOKEN="value-a")
    _write_env(skippy, COMMS_ADMIN_BEARER_TOKEN="value-b")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system, skippy=skippy)

    rows = credentials_data.list_credentials()
    admin = next(r for r in rows if r["key"] == "comms_admin_bearer_token")

    assert admin["in_sync"] is False


def test_list_credentials_marks_manual_only_entries_non_rotatable():
    rows = credentials_data.list_credentials()
    non_rotatable = {r["key"] for r in rows if not r["rotatable"]}

    assert "hive_tools_token" in non_rotatable
    assert "github_pat" in non_rotatable
    # Manual-only entries explain themselves; settable ones offer a set flow instead.
    assert all(
        r["manual_reason"] or r["settable"] for r in rows if not r["rotatable"]
    )


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


# --- Reading the credentials ------------------------------------------
#
# The tests of writing a credential were deleted: that code writes the hive's
# live credential files (see the code skill's test-standard.md, "Code whose
# job is to write into production"). Rotation and the GitHub token are
# unprotected until Daniel has agreed how they can be tested safely.


def test_one_unreadable_file_does_not_hide_the_other_credentials(tmp_path, monkeypatch):
    """Requirement 12 — the listing survives a file it cannot read."""
    nervous_system = tmp_path / "nervous_system.env"
    skippy = tmp_path / "skippy.env"
    _write_env(nervous_system, COMMS_ADMIN_BEARER_TOKEN="live-value")
    skippy.write_bytes(b"COMMS_ADMIN_BEARER_TOKEN=caf\xe9\n")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system, skippy=skippy)

    rows = credentials_data.list_credentials()

    assert len(rows) == len(credentials_data.CREDENTIAL_GROUPS)
    comms = next(row for row in rows if row["key"] == "comms_admin_bearer_token")
    by_file = {loc["file"]: loc for loc in comms["locations"]}
    assert by_file["skippy"]["unreadable"] is True
    assert by_file["skippy"]["present"] is False
    # The file it *can* read is still reported, rather than lost with the tab.
    assert by_file["nervous_system"]["present"] is True


def test_an_empty_value_is_not_reported_as_a_configured_credential(tmp_path, monkeypatch):
    """Requirement 13 — KEY= is not set, and a hive of blanks is not in sync."""
    nervous_system = tmp_path / "nervous_system.env"
    skippy = tmp_path / "skippy.env"
    _write_env(nervous_system, COMMS_ADMIN_BEARER_TOKEN="")
    _write_env(skippy, COMMS_ADMIN_BEARER_TOKEN="")
    _patch_env_files(monkeypatch, tmp_path, nervous_system=nervous_system, skippy=skippy)

    comms = next(
        row for row in credentials_data.list_credentials() if row["key"] == "comms_admin_bearer_token"
    )

    by_file = {loc["file"]: loc for loc in comms["locations"]}
    assert by_file["nervous_system"]["present"] is False
    assert not by_file["nervous_system"]["preview"]
    # Every file agreeing on nothing is not agreement.
    assert comms["in_sync"] is False


def _env_files_in_a_fresh_console(env_value):
    """ENV_FILES as a console process started with this HIVE_ENV_FILES has it."""
    import json as _json
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != "HIVE_ENV_FILES"}
    if env_value is not None:
        env["HIVE_ENV_FILES"] = env_value
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    done = subprocess.run(
        [sys.executable, "-c",
         "import json, credentials_data; "
         "print(json.dumps({k: str(v) for k, v in credentials_data.ENV_FILES.items()}))"],
        cwd=src, env=env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return _json.loads(done.stdout)


def test_hive_env_files_names_the_files_the_console_reads():
    files = _env_files_in_a_fresh_console('{"nervous_system": "/env_hive/.env"}')

    assert files == {"nervous_system": "/env_hive/.env"}


def test_unset_hive_env_files_keeps_the_workstation_files():
    files = _env_files_in_a_fresh_console(None)

    assert files == {k: str(v) for k, v in credentials_data.DEFAULT_ENV_FILES.items()}


def test_malformed_hive_env_files_is_refused_not_read_as_unset():
    with pytest.raises(ValueError, match="HIVE_ENV_FILES"):
        credentials_data.env_files_from('{"nervous_system": "relative/.env"}')


def test_credentials_tab_lists_only_the_files_it_was_given(tmp_path, monkeypatch):
    hive_env = tmp_path / "hive.env"
    _write_env(hive_env, COMMS_ADMIN_BEARER_TOKEN="c", LUCENT_ADMIN_BEARER_TOKEN="l")
    monkeypatch.setattr(credentials_data, "ENV_FILES", {"nervous_system": hive_env})

    groups = {g["key"]: g for g in credentials_data.list_credentials()}

    for key in ("comms_admin_bearer_token", "lucent_admin_bearer_token"):
        assert [loc["file"] for loc in groups[key]["locations"]] == ["nervous_system"]
        assert groups[key]["locations"][0]["present"] is True
    assert groups["hive_tools_token"]["locations"] == []


def test_holder_whose_own_file_is_not_configured_is_found_in_the_shared_file(
    tmp_path, monkeypatch
):
    hive_env = tmp_path / "hive.env"
    _write_env(hive_env, LUCENT_TOKEN_SKIPPY="v")
    env_files = {"nervous_system": hive_env}
    monkeypatch.setattr(credentials_data, "ENV_FILES", env_files)
    monkeypatch.setattr(holder_credentials, "ENV_FILES", env_files)

    location = holder_credentials.locate(holder_credentials.SERVICES["lucent"], "skippy")

    assert (location.file, location.env_key) == ("nervous_system", "LUCENT_TOKEN_SKIPPY")
