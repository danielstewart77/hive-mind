"""Capability repository installation is fixed-source and non-destructive."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
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
        # A finished clone leaves checked-out content beside .git; a bare .git
        # is the wreckage of an interrupted one and is not an install.
        (destination / "README.md").write_text("# inference-proxy\n")

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
    (destination / "README.md").write_text("# inference-proxy\n")
    monkeypatch.setenv("INFERENCE_PROXY_REPO_DIR", str(destination))

    with patch.object(capability_install.subprocess, "run") as clone:
        result = capability_install.clone_inference_proxy()

    clone.assert_not_called()
    assert result["installed"] is True


# ── hive-tools ────────────────────────────────────────────────────────
#
# The tools source is hive-mind's own modules/hive-tools, read through a
# read-only mount. Nothing is fetched, so there is no clone to test: the
# console only reports whether that source is readable and where it is.


def test_the_reported_destination_comes_from_hive_tools_dir_or_the_checkout_default(monkeypatch):
    monkeypatch.setenv("HIVE_TOOLS_DIR", "/srv/private/hive-tools")
    assert capability_install.hive_tools_install_status()["path"] == "/srv/private/hive-tools"

    monkeypatch.delenv("HIVE_TOOLS_DIR")
    assert capability_install.hive_tools_install_status()["path"] == "../hive-tools"


def test_the_tools_source_reads_as_present_from_its_files_not_from_a_git_folder(tmp_path, monkeypatch):
    destination = tmp_path / "modules" / "hive-tools"
    destination.mkdir(parents=True)
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))
    assert capability_install.hive_tools_install_status()["installed"] is False

    (destination / "server.py").write_text("")
    (destination / "Dockerfile").write_text("")
    status = capability_install.hive_tools_install_status()
    assert status["installed"] is True
    assert status["installing"] is False
