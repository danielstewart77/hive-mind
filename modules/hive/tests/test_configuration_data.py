"""Tests for durable, non-secret configuration owned by the Hive site."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import configuration_data


def test_capability_endpoint_round_trip_normalizes_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))

    saved = configuration_data.set_capability_endpoint(
        "ollama", " http://192.168.4.64:11434/ "
    )

    assert saved == "http://192.168.4.64:11434"
    assert configuration_data.get_capability_endpoint("ollama") == saved


@pytest.mark.parametrize(
    "node_id,endpoint",
    [
        ("unknown", "http://service"),
        ("ollama", "file:///etc/passwd"),
        ("ollama", "http://user:password@service"),
        ("ollama", "http://service/path?secret=value"),
    ],
)
def test_capability_endpoint_rejects_unsafe_values(
    tmp_path, monkeypatch, node_id, endpoint
):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))

    with pytest.raises(ValueError):
        configuration_data.set_capability_endpoint(node_id, endpoint)


def test_edge_mind_metadata_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))

    configuration_data.set_edge_mind_metadata(
        "sentinel-one",
        profile="sentinel",
        role="operator",
        deployment="container",
    )

    assert configuration_data.get_edge_mind_metadata("sentinel-one") == {
        "profile": "sentinel",
        "role": "operator",
        "deployment": "container",
    }


def test_update_local_runtime_model_preserves_other_configuration(tmp_path, monkeypatch):
    read_dir = tmp_path / "read"
    write_dir = tmp_path / "write"
    (read_dir / "ada").mkdir(parents=True)
    (write_dir / "ada").mkdir(parents=True)
    source = """name: ada
remote: false
provider: anthropic
default_model: sonnet
env:
  EXISTING_SETTING: preserved
"""
    (read_dir / "ada" / "runtime.yaml").write_text(source)
    (write_dir / "ada" / "runtime.yaml").write_text(source)
    (write_dir / "ada" / "runtime.yaml").chmod(0o644)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", read_dir)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", write_dir)

    updated = configuration_data.update_local_runtime_model("ada", "opus")

    assert updated["default_model"] == "opus"
    assert updated["env"]["EXISTING_SETTING"] == "preserved"
    assert 'default_model: "opus"' in (write_dir / "ada" / "runtime.yaml").read_text()
    assert (write_dir / "ada" / "runtime.yaml").stat().st_mode & 0o777 == 0o644


def test_update_local_runtime_model_rejects_remote_mind(tmp_path, monkeypatch):
    read_dir = tmp_path / "read"
    write_dir = tmp_path / "write"
    (read_dir / "hex").mkdir(parents=True)
    (write_dir / "hex").mkdir(parents=True)
    source = "name: hex\nremote: true\ndefault_model: gpt-5.5\n"
    (read_dir / "hex" / "runtime.yaml").write_text(source)
    (write_dir / "hex" / "runtime.yaml").write_text(source)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", read_dir)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", write_dir)

    with pytest.raises(ValueError, match="remote"):
        configuration_data.update_local_runtime_model("hex", "gpt-5.4")
