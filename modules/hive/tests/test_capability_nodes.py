"""What the Capability Nodes cards render from, for the hive-tools node."""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

import auth
import capability_install
import main as main_mod
import accounts
from hive_site.testing import run


def _render_installation_controls(node: dict) -> str:
    """Run the card's real branch, rather than grepping its source.

    Reading the template as text cannot see branch order, reachability or
    polarity: a branch moved below the function's final return, or a condition
    inverted, leaves every substring a grep looks for exactly where it was.
    Node is the only JS runtime this repo can reach, so the function is lifted
    out and executed against a stub of the `h` helper the page provides.
    """
    template = (
        Path(main_mod.BASE_DIR) / "templates" / "capability_nodes.html"
    ).read_text()
    body = template.split("function installationControls(node) {", 1)[1]
    body = body.split("\n    }", 1)[0]
    script = (
        "const h = {escapeHtml: function (value) {"
        "  return String(value === undefined || value === null ? '' : value)"
        "    .replace(/&/g, '&amp;').replace(/</g, '&lt;')"
        "    .replace(/>/g, '&gt;').replace(/\"/g, '&quot;');"
        "}};\n"
        "function installationControls(node) {" + body + "\n}\n"
        "process.stdout.write(installationControls(JSON.parse(process.argv[1])) || '');"
    )
    result = subprocess.run(
        ["node", "-e", script, json.dumps(node)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


async def _hive_tools_node() -> dict:
    return next(
        node for node in await main_mod._capability_node_specs() if node["id"] == "hive-tools"
    )


def _authed_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = run(auth.create_user("daniel", "secret-pass", is_admin=True, replace=True))
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, run(auth.create_session_token(user)))
    return client


async def test_the_hive_tools_card_is_bundled_and_offers_no_clone():
    node = await _hive_tools_node()
    assert node["bundled"] is True
    assert node["installing"] is False
    assert "hive-mind" in node["repository"]


# Requirement 3 — an installed hive-tools offers a link to its own site rather
# than an install action.
async def test_installed_hive_tools_node_carries_the_configure_link(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    destination.mkdir(parents=True)
    (destination / "server.py").write_text("")
    (destination / "Dockerfile").write_text("")
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))
    monkeypatch.setenv("EDGE_HIVE_TOOLS_URL", "http://192.168.4.64:9421")

    node = await _hive_tools_node()

    assert node["installed"] is True
    assert node["configure_url"] == "http://192.168.4.64:9421"
    assert node["configure_label"] == "Configure"


# Requirement 3, fresh install — the placeholder host .env.example ships is not
# a reachable site, so the card must not offer it as a link to open.
async def test_placeholder_hive_tools_url_is_not_offered_as_a_link(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    destination.mkdir(parents=True)
    (destination / "server.py").write_text("")
    (destination / "Dockerfile").write_text("")
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))
    monkeypatch.delenv("HIVE_TOOLS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("EDGE_HIVE_TOOLS_URL", "http://hive-host:9421")
    # The compose service name resolves on the container network and nowhere
    # the operator's browser can follow it, so it is not a fallback either.
    monkeypatch.setenv("HIVE_TOOLS_URL", "http://hive-tools:9421")

    node = await _hive_tools_node()

    assert node["configure_url"] == ""
    # And the card says how to get one, rather than substituting the git clone
    # url, which is not the node's own site.
    assert "EDGE_HIVE_TOOLS_URL" in node["configure_hint"]


# Requirement 6 — the card names the approval-gate placement boundary.
async def test_hive_tools_node_carries_the_placement_note():
    note = (await _hive_tools_node())["placement_note"]

    # Behaviour, not a comparison against the constant being tested: the note
    # has to actually name the boundary and who is on the wrong side of it.
    assert "non-operator" in note
    assert "Hive Tools" in note
    assert "approval" in note.lower()


# The card for a bundled node: where the source is read from, the placement
# note, and no install handle.
def test_the_bundled_card_names_its_source_and_offers_no_clone_button(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node is required to execute the card's rendering branch")
    node = {"installable": True, "bundled": True, "installed": True, "id": "hive-tools",
            "path": "../hive-tools", "repository": "modules/hive-tools in hive-mind",
            "placement_note": "kept outside the checkout"}
    card = _render_installation_controls(node)
    assert "modules/hive-tools in hive-mind" in card
    assert "kept outside the checkout" in card
    assert "data-capability-install=" not in card

    missing = _render_installation_controls({**node, "installed": False})
    assert "not visible to the console" in missing
    assert "kept outside the checkout" in missing
    assert "data-capability-install=" not in missing


async def test_the_inference_proxy_node_guards_its_clone_like_hive_tools(tmp_path, monkeypatch):
    """Requirement 14 — real-checkout truth, a refused second clone, in-flight to every tab."""
    destination = tmp_path / "inference-proxy"
    destination.mkdir()
    monkeypatch.setenv("INFERENCE_PROXY_REPO_DIR", str(destination))

    async def _node() -> dict:
        return next(
            node for node in await main_mod._capability_node_specs()
            if node["id"] == "inference-proxy"
        )

    # A bare .git is the wreckage of an interrupted clone, not an install.
    # Reading it as one is what made a broken node look permanently successful.
    (destination / ".git").mkdir()
    assert (await _node())["installed"] is False
    (destination / "README.md").write_text("real content\n")
    assert (await _node())["installed"] is True

    # A tab that did not start the clone reads the same process state as the
    # one that did, and a second install is refused rather than run twice over
    # the same directory.
    assert (await _node())["installing"] is False
    with capability_install._INFERENCE_PROXY_INSTALL_LOCK:
        assert (await _node())["installing"] is True
        with pytest.raises(capability_install.InstallInProgress):
            capability_install.clone_inference_proxy()
