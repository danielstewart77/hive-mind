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


def _hive_tools_node() -> dict:
    return next(
        node for node in main_mod._capability_node_specs() if node["id"] == "hive-tools"
    )


def _authed_client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    return client


INSTALL_ROUTE = "/api/capability-nodes/hive-tools/install"


# Requirement 1 — the button exists to be clicked, and clicking it is what
# runs the install. Without this the whole route could be deleted, or replaced
# with a hardcoded success, and every other test would still pass.
def test_clicking_clone_runs_the_install_and_reports_the_result(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)
    with patch.object(
        main_mod,
        "clone_hive_tools",
        return_value={"installed": True, "path": "/srv/t", "repository": "r"},
    ) as clone:
        response = client.post(INSTALL_ROUTE)

    clone.assert_called_once_with()
    assert response.status_code == 200
    assert response.json()["installed"] is True


# Requirement 1 — cloning to disk is an operator action, so an anonymous
# caller cannot trigger it.
def test_the_install_route_refuses_an_unauthenticated_caller(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    client = TestClient(main_mod.app)

    with patch.object(main_mod, "clone_hive_tools") as clone:
        assert client.post(INSTALL_ROUTE).status_code == 401
    clone.assert_not_called()


# Requirement 1 — the card offers the action at all. `installable` is what the
# template keys the button off; false and the button silently never renders.
def test_the_hive_tools_card_offers_the_install_action():
    assert _hive_tools_node()["installable"] is True


# Requirement 1 — a failure reaches the operator as the reason it failed, and
# a second concurrent click is distinguishable from a blocked destination.
def test_install_failures_reach_the_card_with_their_own_status(tmp_path, monkeypatch):
    client = _authed_client(tmp_path, monkeypatch)

    with patch.object(
        main_mod, "clone_hive_tools", side_effect=RuntimeError("could not resolve host")
    ):
        failed = client.post(INSTALL_ROUTE)
    assert failed.status_code == 502
    assert "could not resolve host" in failed.json()["detail"]

    with patch.object(
        main_mod, "clone_hive_tools", side_effect=ValueError("destination is not empty")
    ):
        assert client.post(INSTALL_ROUTE).status_code == 409

    with patch.object(
        main_mod,
        "clone_hive_tools",
        side_effect=capability_install.InstallInProgress("already running"),
    ):
        assert client.post(INSTALL_ROUTE).status_code == 503


# Requirement 3 — an installed hive-tools offers a link to its own site rather
# than an install action.
def test_installed_hive_tools_node_carries_the_configure_link(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    (destination / ".git").mkdir(parents=True)
    (destination / "README.md").write_text("# hive-tools\n")
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))
    monkeypatch.setenv("EDGE_HIVE_TOOLS_URL", "http://192.168.4.64:9421")

    node = _hive_tools_node()

    assert node["installed"] is True
    assert node["configure_url"] == "http://192.168.4.64:9421"
    assert node["configure_label"] == "Configure"


# Requirement 3, fresh install — the placeholder host .env.example ships is not
# a reachable site, so the card must not offer it as a link to open.
def test_placeholder_hive_tools_url_is_not_offered_as_a_link(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    (destination / ".git").mkdir(parents=True)
    (destination / "README.md").write_text("# hive-tools\n")
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))
    monkeypatch.delenv("HIVE_TOOLS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("EDGE_HIVE_TOOLS_URL", "http://hive-host:9421")
    # The compose service name resolves on the container network and nowhere
    # the operator's browser can follow it, so it is not a fallback either.
    monkeypatch.setenv("HIVE_TOOLS_URL", "http://hive-tools:9421")

    node = _hive_tools_node()

    assert node["configure_url"] == ""
    # And the card says how to get one, rather than substituting the git clone
    # url, which is not the node's own site.
    assert "EDGE_HIVE_TOOLS_URL" in node["configure_hint"]


# Requirement 2, wording — the card tells the operator to recreate the
# container, because a restart re-reads neither the mount nor the env.
def test_hive_tools_node_path_hint_names_the_setting_and_the_recreate_command():
    hint = _hive_tools_node()["path_hint"]

    assert "HIVE_TOOLS_DIR" in hint
    assert "docker compose up -d" in hint


# Requirement 6 — the card names the approval-gate placement boundary.
def test_hive_tools_node_carries_the_placement_note():
    note = _hive_tools_node()["placement_note"]

    # Behaviour, not a comparison against the constant being tested: the note
    # has to actually name the boundary and who is on the wrong side of it.
    assert "non-operator" in note
    assert "Hive Tools" in note
    assert "approval" in note.lower()


# Requirement 6 — the note reaches the card in every state, not only the one
# the operator happens to be in. The card is client-side JS with no test
# runner in this repo, so this pins the source rather than the rendering:
# each branch of installationControls must emit the placement note.
def test_every_install_card_branch_emits_the_placement_note():
    template = (
        Path(main_mod.BASE_DIR) / "templates" / "capability_nodes.html"
    ).read_text()
    body = template.split("function installationControls(node) {", 1)[1]
    body = body.split("\n    }", 1)[0]

    returns = [line for line in body.splitlines() if "return " in line]
    # The bare `if (!node.installable) return ""` guard is not a card.
    cards = [line for line in returns if "capability-install" in line]

    assert len(cards) == 3, "expected installed, installing and not-installed cards"
    assert all("placement" in line for line in cards)

    # …and `placement` must actually be the node's note, not a constant that
    # happens to be spliced into both branches.
    definition = next(
        line for line in body.splitlines() if "const placement" in line
    )
    assert "node.placement_note ?" in definition


# Requirement 6 — a clone takes minutes and the grid repaints every 30 seconds.
# Until the in-flight state was server truth, that repaint handed every tab a
# fresh enabled Clone button over a running install: the tab that started it
# lost its own progress line, and a second operator's click earned a 503.
def test_an_install_in_flight_is_visible_to_every_tab(tmp_path, monkeypatch):
    destination = tmp_path / "hive-tools"
    destination.mkdir()
    monkeypatch.setenv("HIVE_TOOLS_REPO_DIR", str(destination))

    assert _hive_tools_node()["installing"] is False

    # A tab that did not start the install reads the same process state the
    # one that did is holding.
    with capability_install._HIVE_TOOLS_INSTALL_LOCK:
        during = _hive_tools_node()

    assert during["installing"] is True
    assert during["installed"] is False

    if shutil.which("node") is None:
        pytest.skip("node is required to execute the card's rendering branch")

    base = {"installable": True, "id": "hive-tools", "path": str(destination)}
    mid_clone = _render_installation_controls({**base, **during})
    idle = _render_installation_controls({**base, "installing": False, "installed": False})

    # Mid-clone the button is shut and says so, and carries no install handle —
    # a card that still offered the click could only earn a 503.
    assert "disabled" in mid_clone
    assert "Cloning source" in mid_clone
    assert "data-capability-install=" not in mid_clone

    # And the same function offers a live button when no clone is running, so
    # a branch that is dead, mis-ordered or inverted cannot pass both halves.
    assert "data-capability-install=" in idle
    assert "disabled" not in idle


def test_the_inference_proxy_node_guards_its_clone_like_hive_tools(tmp_path, monkeypatch):
    """Requirement 14 — real-checkout truth, a refused second clone, in-flight to every tab."""
    destination = tmp_path / "inference-proxy"
    destination.mkdir()
    monkeypatch.setenv("INFERENCE_PROXY_REPO_DIR", str(destination))

    def _node() -> dict:
        return next(
            node for node in main_mod._capability_node_specs()
            if node["id"] == "inference-proxy"
        )

    # A bare .git is the wreckage of an interrupted clone, not an install.
    # Reading it as one is what made a broken node look permanently successful.
    (destination / ".git").mkdir()
    assert _node()["installed"] is False
    (destination / "README.md").write_text("real content\n")
    assert _node()["installed"] is True

    # A tab that did not start the clone reads the same process state as the
    # one that did, and a second install is refused rather than run twice over
    # the same directory.
    assert _node()["installing"] is False
    with capability_install._INFERENCE_PROXY_INSTALL_LOCK:
        assert _node()["installing"] is True
        with pytest.raises(capability_install.InstallInProgress):
            capability_install.clone_inference_proxy()
