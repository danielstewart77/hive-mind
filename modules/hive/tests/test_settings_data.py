"""Unit tests for settings_data.py's local mind-config discovery."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import settings_data


def test_find_local_config_reads_runtime_yaml(tmp_path, monkeypatch):
    mind_dir = tmp_path / "hive_mind" / "bob"
    mind_dir.mkdir(parents=True)
    (mind_dir / "runtime.yaml").write_text(
        "name: bob\nharness: claude_cli\ndefault_model: qwen3\n"
    )
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(settings_data, "EDGE_MINDS_DIR", tmp_path / "edge")

    result = settings_data._find_local_config("bob")

    assert result["source"] == "runtime.yaml"
    assert result["config"]["harness"] == "claude_cli"
    assert result["config"]["default_model"] == "qwen3"


def test_find_local_config_reads_edge_mind_runtime_yaml(tmp_path, monkeypatch):
    """An edge Mind installed on this host is local, not remote.

    Its runtime.yaml lives in its own checkout, mounted at
    /minds_edge/<name>/ — which is what makes the console's default-model
    editor reachable for it at all.
    """
    mind_dir = tmp_path / "edge" / "skippy"
    mind_dir.mkdir(parents=True)
    (mind_dir / "runtime.yaml").write_text(
        "name: skippy\nharness: claude_cli\nprovider: anthropic\n"
        "deployment: systemd\ndefault_model: opus\n"
    )
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(settings_data, "EDGE_MINDS_DIR", tmp_path / "edge")

    result = settings_data._find_local_config("skippy")

    assert result["source"] == "runtime.yaml"
    assert result["config"]["default_model"] == "opus"
    assert result["config"]["deployment"] == "systemd"


def test_find_local_config_returns_none_for_remote_mind(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(settings_data, "EDGE_MINDS_DIR", tmp_path / "edge")

    assert settings_data._find_local_config("arnold") is None


def test_build_mind_registry_merges_broker_and_local(tmp_path, monkeypatch):
    mind_dir = tmp_path / "hive_mind" / "bob"
    mind_dir.mkdir(parents=True)
    (mind_dir / "runtime.yaml").write_text("harness: claude_cli\n")
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(settings_data, "EDGE_MINDS_DIR", tmp_path / "edge")

    broker_minds = [
        {"name": "bob", "id": "b9f2", "gateway_url": "http://bob:8420", "model": "qwen3"},
        {"name": "arnold", "id": "5437", "gateway_url": "http://192.168.5.64:8433"},
    ]
    rows = settings_data.build_mind_registry(broker_minds)

    by_name = {r["name"]: r for r in rows}
    assert by_name["bob"]["local_config"]["config"]["harness"] == "claude_cli"
    assert by_name["arnold"]["local_config"] is None
    assert rows == sorted(rows, key=lambda r: r["name"])


def test_build_mind_registry_skips_malformed_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(settings_data, "EDGE_MINDS_DIR", tmp_path / "edge")

    rows = settings_data.build_mind_registry([{"name": "ok"}, "not-a-dict", None])

    assert len(rows) == 1
    assert rows[0]["name"] == "ok"


def test_build_mind_registry_redacts_runtime_secrets(tmp_path, monkeypatch):
    mind_dir = tmp_path / "hive_mind" / "bob"
    mind_dir.mkdir(parents=True)
    (mind_dir / "runtime.yaml").write_text(
        "provider: ollama\n"
        "env:\n"
        "  OPENAI_API_KEY: super-secret\n"
        "  ANTHROPIC_BASE_URL: http://proxy:8899\n"
        "nested:\n"
        "  bearer_token: another-secret\n"
    )
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(settings_data, "EDGE_MINDS_DIR", tmp_path / "edge")

    rows = settings_data.build_mind_registry([{"name": "bob", "id": "b9f2"}])
    config = rows[0]["local_config"]["config"]

    assert config["env"]["OPENAI_API_KEY"] == "[redacted]"
    assert config["env"]["ANTHROPIC_BASE_URL"] == "http://proxy:8899"
    assert config["nested"]["bearer_token"] == "[redacted]"
    assert "super-secret" not in str(rows)


def test_hive_tools_status_discovers_supported_capabilities(tmp_path, monkeypatch):
    repo = tmp_path / "hive-tools"
    tools_dir = repo / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "gmail.py").write_text("router = object()")
    (tools_dir / "calendar.py").write_text("router = object()")
    (tools_dir / "__init__.py").write_text("")
    monkeypatch.setattr(settings_data, "HIVE_TOOLS_REPO_DIR", repo)

    status = settings_data.hive_tools_status()

    assert [item["id"] for item in status["capabilities"]] == ["calendar", "gmail"]
    assert status["capabilities"][1]["name"] == "Gmail"


# ── hive-tools status ─────────────────────────────────────────────────


def test_hive_tools_status_present_when_dir_has_entries(tmp_path, monkeypatch):
    repo = tmp_path / "hive-tools"
    repo.mkdir()
    (repo / "README.md").write_text("hi")
    monkeypatch.setattr(settings_data, "HIVE_TOOLS_REPO_DIR", repo)

    status = settings_data.hive_tools_status()

    assert status["present"] is True
    assert status["entry_count"] == 1


def test_hive_tools_status_absent_when_dir_empty(tmp_path, monkeypatch):
    repo = tmp_path / "hive-tools"
    repo.mkdir()
    monkeypatch.setattr(settings_data, "HIVE_TOOLS_REPO_DIR", repo)

    assert settings_data.hive_tools_status()["present"] is False


def test_hive_tools_status_absent_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_TOOLS_REPO_DIR", tmp_path / "nope")

    assert settings_data.hive_tools_status()["present"] is False


# ── voice ref ──────────────────────────────────────────────────────────

_MINIMAL_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 16 + b"data\x00\x00\x00\x00"


def test_voice_ref_status_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path)
    assert settings_data.voice_ref_status("bob") is None


def test_voice_ref_status_reports_size_when_present(tmp_path, monkeypatch):
    mind_dir = tmp_path / "bob"
    mind_dir.mkdir()
    (mind_dir / "voice_ref.wav").write_bytes(_MINIMAL_WAV)
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path)

    status = settings_data.voice_ref_status("bob")

    assert status["size_bytes"] == len(_MINIMAL_WAV)


def test_save_voice_ref_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    target = settings_data.save_voice_ref("bob", _MINIMAL_WAV)

    assert target.read_bytes() == _MINIMAL_WAV
    assert target == tmp_path / "bob" / "voice_ref.wav"


def test_save_voice_ref_rejects_non_wav(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    with pytest.raises(ValueError, match="WAV"):
        settings_data.save_voice_ref("bob", b"not a wav file at all")


def test_save_voice_ref_rejects_unsafe_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    with pytest.raises(ValueError, match="invalid mind name"):
        settings_data.save_voice_ref("../etc", _MINIMAL_WAV)


def test_save_voice_ref_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)
    monkeypatch.setattr(settings_data, "_WAV_MAX_BYTES", 10)

    with pytest.raises(ValueError, match="too large"):
        settings_data.save_voice_ref("bob", _MINIMAL_WAV)


# ── downloads ──────────────────────────────────────────────────────────


def test_list_downloads_only_lists_existing_allowed_files(tmp_path, monkeypatch):
    (tmp_path / "hive-init.py").write_text("print('hi')")
    monkeypatch.setattr(settings_data, "HIVE_INIT_DIR", tmp_path)

    downloads = settings_data.list_downloads()

    assert len(downloads) == 1
    assert downloads[0]["filename"] == "hive-init.py"


def test_resolve_download_path_rejects_unknown_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_INIT_DIR", tmp_path)
    assert settings_data.resolve_download_path("../../etc/passwd") is None
    assert settings_data.resolve_download_path("not-allowed.txt") is None


def test_resolve_download_path_returns_path_for_existing_allowed_file(tmp_path, monkeypatch):
    (tmp_path / "hive-init.sh").write_text("#!/bin/sh")
    monkeypatch.setattr(settings_data, "HIVE_INIT_DIR", tmp_path)

    resolved = settings_data.resolve_download_path("hive-init.sh")

    assert resolved == (tmp_path / "hive-init.sh").resolve()
