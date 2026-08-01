"""What the Capability Nodes cards render from, for the hive-tools node."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

import auth
import capability_install
import main as main_mod


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

    assert len(cards) == 2, "expected an installed and a not-installed card"
    assert all("placement" in line for line in cards)

    # …and `placement` must actually be the node's note, not a constant that
    # happens to be spliced into both branches.
    definition = next(
        line for line in body.splitlines() if "const placement" in line
    )
    assert "node.placement_note ?" in definition
