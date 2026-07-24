"""Unit tests for settings_data.py's local mind-config discovery."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import settings_data


def test_find_local_config_reads_runtime_yaml(tmp_path, monkeypatch):
    mind_dir = tmp_path / "hive_mind" / "bob"
    mind_dir.mkdir(parents=True)
    (mind_dir / "runtime.yaml").write_text(
        "name: bob\nharness: claude_cli\ndefault_model: qwen3\n"
    )
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(settings_data, "SKIPPY_MINDS_DIR", tmp_path / "skippy")

    result = settings_data._find_local_config("bob")

    assert result["source"] == "runtime.yaml"
    assert result["config"]["harness"] == "claude_cli"
    assert result["config"]["default_model"] == "qwen3"


def test_find_local_config_reads_mind_md_frontmatter(tmp_path, monkeypatch):
    mind_dir = tmp_path / "skippy" / "skippy"
    mind_dir.mkdir(parents=True)
    (mind_dir / "MIND.md").write_text(
        "---\nname: skippy\nmodel: opus\nharness: claude_cli_claude\n---\n"
        "Skippy the Magnificent — elder mind.\n"
    )
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(settings_data, "SKIPPY_MINDS_DIR", tmp_path / "skippy")

    result = settings_data._find_local_config("skippy")

    assert result["source"] == "MIND.md"
    assert result["config"]["model"] == "opus"
    assert "Skippy the Magnificent" in result["body"]


def test_find_local_config_returns_none_for_remote_mind(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(settings_data, "SKIPPY_MINDS_DIR", tmp_path / "skippy")

    assert settings_data._find_local_config("arnold") is None


def test_build_mind_registry_merges_broker_and_local(tmp_path, monkeypatch):
    mind_dir = tmp_path / "hive_mind" / "bob"
    mind_dir.mkdir(parents=True)
    (mind_dir / "runtime.yaml").write_text("harness: claude_cli\n")
    monkeypatch.setattr(settings_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(settings_data, "SKIPPY_MINDS_DIR", tmp_path / "skippy")

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
    monkeypatch.setattr(settings_data, "SKIPPY_MINDS_DIR", tmp_path / "skippy")

    rows = settings_data.build_mind_registry([{"name": "ok"}, "not-a-dict", None])

    assert len(rows) == 1
    assert rows[0]["name"] == "ok"
