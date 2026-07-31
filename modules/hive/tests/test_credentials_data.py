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
    manual_keys = {r["key"] for r in rows if not r["rotatable"]}

    assert "hive_tools_token" in manual_keys
    assert "github_pat" in manual_keys
    assert all(r["manual_reason"] for r in rows if not r["rotatable"])


def test_rotate_credential_writes_new_value_to_every_location(tmp_path, monkeypatch):
    nervous_system = tmp_path / "nervous_system.env"
    hive_mind = tmp_path / "hive_mind.env"
    skippy = tmp_path / "skippy.env"
    _write_env(nervous_system, COMMS_BEARER_TOKEN="old", OTHER_KEY="untouched")
    _write_env(hive_mind, COMMS_BEARER_TOKEN="old")
    _write_env(skippy, COMMS_BEARER_TOKEN="old", HIVEMIND_BROKER_TOKEN="old")
    _patch_env_files(
        monkeypatch, tmp_path,
        nervous_system=nervous_system, hive_mind=hive_mind, skippy=skippy,
    )

    result = credentials_data.rotate_credential("comms_bearer_token")

    new_comms = credentials_data._read_env_value(nervous_system, "COMMS_BEARER_TOKEN")
    assert new_comms != "old"
    assert credentials_data._read_env_value(hive_mind, "COMMS_BEARER_TOKEN") == new_comms
    assert credentials_data._read_env_value(skippy, "COMMS_BEARER_TOKEN") == new_comms
    assert credentials_data._read_env_value(skippy, "HIVEMIND_BROKER_TOKEN") == new_comms
    assert credentials_data._read_env_value(nervous_system, "OTHER_KEY") == "untouched"
    assert len(result["written"]) == 4
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


def test_write_env_value_preserves_file_permissions(tmp_path):
    path = tmp_path / "secret.env"
    _write_env(path, TOKEN="old")
    path.chmod(0o600)

    assert credentials_data._write_env_value(path, "TOKEN", "new") is True
    assert credentials_data._read_env_value(path, "TOKEN") == "new"
    assert oct(path.stat().st_mode)[-3:] == "600"
