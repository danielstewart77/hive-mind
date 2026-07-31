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


def test_runtime_path_prefers_the_containerized_mount(tmp_path, monkeypatch):
    hive_mind_dir = tmp_path / "hive_mind"
    (hive_mind_dir / "ada").mkdir(parents=True)
    (hive_mind_dir / "ada" / "runtime.yaml").write_text("name: ada\n")
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", hive_mind_dir)
    monkeypatch.setattr(configuration_data, "EDGE_MINDS_DIR", tmp_path / "edge")

    assert (
        configuration_data._runtime_path("ada")
        == hive_mind_dir / "ada" / "runtime.yaml"
    )


def test_runtime_path_falls_back_to_the_edge_mount(tmp_path, monkeypatch):
    edge_dir = tmp_path / "edge"
    (edge_dir / "skippy").mkdir(parents=True)
    (edge_dir / "skippy" / "runtime.yaml").write_text("name: skippy\n")
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", tmp_path / "hive_mind")
    monkeypatch.setattr(configuration_data, "EDGE_MINDS_DIR", edge_dir)

    assert (
        configuration_data._runtime_path("skippy")
        == edge_dir / "skippy" / "runtime.yaml"
    )


