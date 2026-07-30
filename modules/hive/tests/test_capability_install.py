"""Capability repository installation is fixed-source and non-destructive."""

from unittest.mock import patch

import pytest

import capability_install


def test_clone_inference_proxy_uses_fixed_public_repository(tmp_path, monkeypatch):
    destination = tmp_path / "inference-proxy"
    monkeypatch.setenv("INFERENCE_PROXY_REPO_DIR", str(destination))

    def clone(command, **kwargs):
        assert command == [
            "git",
            "clone",
            "--depth",
            "1",
            capability_install.INFERENCE_PROXY_REPOSITORY,
            str(destination),
        ]
        assert kwargs["timeout"] == 120
        (destination / ".git").mkdir(parents=True)

    with patch.object(capability_install.subprocess, "run", side_effect=clone):
        result = capability_install.clone_inference_proxy()

    assert result["installed"] is True
    assert result["repository"].startswith("https://github.com/")


def test_clone_inference_proxy_refuses_nonempty_destination(tmp_path, monkeypatch):
    destination = tmp_path / "inference-proxy"
    destination.mkdir()
    (destination / "keep.txt").write_text("mine")
    monkeypatch.setenv("INFERENCE_PROXY_REPO_DIR", str(destination))

    with pytest.raises(ValueError, match="not empty"):
        capability_install.clone_inference_proxy()

    assert (destination / "keep.txt").read_text() == "mine"


def test_clone_inference_proxy_is_idempotent(tmp_path, monkeypatch):
    destination = tmp_path / "inference-proxy"
    (destination / ".git").mkdir(parents=True)
    monkeypatch.setenv("INFERENCE_PROXY_REPO_DIR", str(destination))

    with patch.object(capability_install.subprocess, "run") as clone:
        result = capability_install.clone_inference_proxy()

    clone.assert_not_called()
    assert result["installed"] is True
