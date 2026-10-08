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


def test_hive_tools_status_present_when_dir_holds_a_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "hive-tools"
    (repo / ".git").mkdir(parents=True)
    (repo / "README.md").write_text("hi")
    monkeypatch.setattr(settings_data, "HIVE_TOOLS_REPO_DIR", repo)

    assert settings_data.hive_tools_status()["present"] is True


def test_hive_tools_status_absent_when_dir_holds_only_the_env_placeholder(
    tmp_path, monkeypatch
):
    repo = tmp_path / "hive-tools"
    (repo / ".env").mkdir(parents=True)
    monkeypatch.setattr(settings_data, "HIVE_TOOLS_REPO_DIR", repo)

    assert settings_data.hive_tools_status()["present"] is False


def test_hive_tools_status_absent_when_dir_empty(tmp_path, monkeypatch):
    repo = tmp_path / "hive-tools"
    repo.mkdir()
    monkeypatch.setattr(settings_data, "HIVE_TOOLS_REPO_DIR", repo)

    assert settings_data.hive_tools_status()["present"] is False


def test_hive_tools_status_absent_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_TOOLS_REPO_DIR", tmp_path / "nope")

    assert settings_data.hive_tools_status()["present"] is False


# ── voice clips ────────────────────────────────────────────────────────

_MINIMAL_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + b"\x00" * 16 + b"data\x00\x00\x00\x00"


def test_list_voice_clips_is_empty_for_a_mind_with_no_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path)
    assert settings_data.list_voice_clips("bob") == []


def test_list_voice_clips_reports_every_recording_in_the_directory(
    tmp_path, monkeypatch
):
    """A mind's directory accumulates candidates; all of them are selectable."""
    mind_dir = tmp_path / "skippy"
    mind_dir.mkdir()
    for name in (
        "voice_ref.wav",
        "_dramitac_mono_voice_ref.wav",
        "dramatic_sterio_voice_ref.wav",
    ):
        (mind_dir / name).write_bytes(_MINIMAL_WAV)
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path)

    clips = settings_data.list_voice_clips("skippy")

    assert [clip["name"] for clip in clips] == [
        "_dramitac_mono_voice_ref.wav",
        "dramatic_sterio_voice_ref.wav",
        "voice_ref.wav",
    ]
    assert clips[0]["size_bytes"] == len(_MINIMAL_WAV)


def test_list_voice_clips_ignores_what_is_not_a_recording(tmp_path, monkeypatch):
    """A mind's directory also holds its runtime.yaml and its soul."""
    mind_dir = tmp_path / "skippy"
    mind_dir.mkdir()
    (mind_dir / "voice_ref.wav").write_bytes(_MINIMAL_WAV)
    (mind_dir / "runtime.yaml").write_text("name: skippy\n")
    (mind_dir / "notes.txt").write_text("not audio")
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path)

    assert [c["name"] for c in settings_data.list_voice_clips("skippy")] == [
        "voice_ref.wav"
    ]


def test_list_voice_clips_refuses_a_mind_name_that_is_a_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path)
    assert settings_data.list_voice_clips("../etc") == []


def test_save_voice_clip_writes_under_its_own_name(tmp_path, monkeypatch):
    """An upload adds a candidate rather than replacing the active clip."""
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    target = settings_data.save_voice_clip("bob", "calm_take_two.wav", _MINIMAL_WAV)

    assert target == tmp_path / "bob" / "calm_take_two.wav"
    assert target.read_bytes() == _MINIMAL_WAV


def test_save_voice_clip_leaves_the_existing_clip_alone(tmp_path, monkeypatch):
    """The clip currently being spoken must survive an upload beside it."""
    mind_dir = tmp_path / "bob"
    mind_dir.mkdir()
    existing = mind_dir / "voice_ref.wav"
    existing.write_bytes(_MINIMAL_WAV + b"original")
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    settings_data.save_voice_clip("bob", "second_take.wav", _MINIMAL_WAV)

    assert existing.read_bytes() == _MINIMAL_WAV + b"original"


def test_save_voice_clip_keeps_an_escaping_filename_inside_the_directory(
    tmp_path, monkeypatch
):
    """The name comes off a browser upload and is joined onto a directory."""
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    target = settings_data.save_voice_clip(
        "bob", "../../../../etc/evil.wav", _MINIMAL_WAV
    )

    assert target == tmp_path / "bob" / "evil.wav"
    assert target.read_bytes() == _MINIMAL_WAV


def test_save_voice_clip_keeps_a_windows_path_inside_the_directory(
    tmp_path, monkeypatch
):
    """A Windows browser sends the whole local path as the filename."""
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    target = settings_data.save_voice_clip(
        "bob", r"C:\Users\daniel\Desktop\take.wav", _MINIMAL_WAV
    )

    assert target == tmp_path / "bob" / "take.wav"


def test_save_voice_clip_refuses_a_name_that_is_not_a_wav(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    with pytest.raises(ValueError, match="wav"):
        settings_data.save_voice_clip("bob", "take.mp3", _MINIMAL_WAV)


def test_save_voice_clip_refuses_a_name_the_mind_could_not_select(
    tmp_path, monkeypatch
):
    """A filename this accepted and runtime.yaml refused is an unusable clip."""
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    with pytest.raises(ValueError, match="clip filename"):
        settings_data.save_voice_clip("bob", "my take (final).wav", _MINIMAL_WAV)


def test_save_voice_clip_rejects_non_wav_content(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    with pytest.raises(ValueError, match="WAV"):
        settings_data.save_voice_clip("bob", "take.wav", b"not a wav file at all")


def test_save_voice_clip_rejects_unsafe_mind_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)

    with pytest.raises(ValueError, match="invalid mind name"):
        settings_data.save_voice_clip("../etc", "take.wav", _MINIMAL_WAV)


def test_save_voice_clip_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_VOICE_DIR", tmp_path)
    monkeypatch.setattr(settings_data, "_WAV_MAX_BYTES", 10)

    with pytest.raises(ValueError, match="too large"):
        settings_data.save_voice_clip("bob", "take.wav", _MINIMAL_WAV)


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
