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


def test_inference_proxy_endpoint_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))

    saved = configuration_data.set_capability_endpoint(
        "inference-proxy", "http://inference-proxy:8888"
    )

    assert configuration_data.get_capability_endpoint("inference-proxy") == saved


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


def test_update_local_runtime_model_writes_edge_mind_file(tmp_path, monkeypatch):
    """An edge Mind's runtime.yaml is read and written at the same path.

    Containerized minds get a read-only display mount and a separate
    read-write one; an edge checkout is mounted once, read-write.
    """
    hive_mind_dir = tmp_path / "hive_mind"
    edge_dir = tmp_path / "edge"
    (edge_dir / "skippy").mkdir(parents=True)
    runtime = edge_dir / "skippy" / "runtime.yaml"
    runtime.write_text(
        "name: skippy\nprovider: anthropic\ndeployment: systemd\n"
        "default_model: sonnet\nmind_server_port: 8421\n"
    )
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", hive_mind_dir)
    monkeypatch.setattr(
        configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", tmp_path / "write"
    )
    monkeypatch.setattr(configuration_data, "EDGE_MINDS_DIR", edge_dir)

    updated = configuration_data.update_local_runtime_model("skippy", "opus")

    assert updated["default_model"] == "opus"
    assert updated["mind_server_port"] == 8421
    assert 'default_model: "opus"' in runtime.read_text()


def test_runtime_paths_prefer_the_containerized_mount(tmp_path, monkeypatch):
    hive_mind_dir = tmp_path / "hive_mind"
    (hive_mind_dir / "ada").mkdir(parents=True)
    (hive_mind_dir / "ada" / "runtime.yaml").write_text("name: ada\n")
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", hive_mind_dir)
    monkeypatch.setattr(
        configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", tmp_path / "write"
    )
    monkeypatch.setattr(configuration_data, "EDGE_MINDS_DIR", tmp_path / "edge")

    source, target = configuration_data._runtime_paths("ada")

    assert source == hive_mind_dir / "ada" / "runtime.yaml"
    assert target == tmp_path / "write" / "ada" / "runtime.yaml"
