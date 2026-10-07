"""The project folders a containerised Mind can reach.

Every test here drives the real fragment reader and writer against a real
file on disk: what is stubbed is the far side of the socket (Hive Tools,
which holds the docker socket) and nothing else.
"""

import json
import os
import sys
from unittest.mock import AsyncMock

import pytest
import yaml
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import configuration_data
import main as main_mod

FRAGMENT = """\
services:
  atlas:
    build:
      context: ../../..
    container_name: hive-mind-atlas
    environment:
      - MIND_NAME=atlas
    volumes:
      # The hive's own checkout, read-only.
      - ${HOST_PROJECT_DIR:-.}:/usr/src/app:ro
      # The one directory this Mind writes.
      - ${HOST_PROJECT_DIR:-.}/minds/atlas:/usr/src/app/minds/atlas:rw
      # The project folders the operator granted.
      - ${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:rw
    restart: unless-stopped
    networks:
      - hivemind
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    minds_dir = tmp_path / "minds"
    (minds_dir / "atlas" / "container").mkdir(parents=True)
    (minds_dir / "atlas" / "runtime.yaml").write_text(
        "name: atlas\nharness: claude_cli\ndeployment: container\nremote: false\n"
    )
    (minds_dir / "atlas" / "container" / "compose.yaml").write_text(FRAGMENT)
    (minds_dir / "edgy").mkdir()
    (minds_dir / "edgy" / "runtime.yaml").write_text(
        "name: edgy\nharness: claude_cli\ndeployment: systemd\nremote: false\n"
    )

    dev = tmp_path / "Dev"
    (dev / "model-testing").mkdir(parents=True)
    (dev / "hive_mind" / "minds").mkdir(parents=True)
    (dev / "secrets-elsewhere").mkdir()
    (tmp_path / "outside").mkdir()
    (dev / "escape").symlink_to(tmp_path / "outside")

    monkeypatch.setenv("HIVE_DB_PATH", str(tmp_path / "hive.db"))
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("HIVE_MIND_PROJECT_PATH", "/host/hive_mind")
    monkeypatch.setenv(
        "MIND_MOUNT_ROOTS",
        json.dumps(
            [
                {
                    "key": "dev",
                    "label": "Dev",
                    "compose_var": "HOST_DEV_DIR",
                    "console_path": str(dev),
                    "container_root": "/mnt/dev",
                }
            ]
        ),
    )
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", minds_dir)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", minds_dir)
    monkeypatch.setattr(
        main_mod,
        "_hive_tools_request",
        AsyncMock(return_value={"status": "success", "return_code": 0}),
    )
    user = auth.create_user("daniel", "secret-pass", is_admin=True, replace=True)
    test_client = TestClient(main_mod.app)
    test_client.cookies.set(auth.SESSION_COOKIE_NAME, auth.create_session_token(user))
    test_client.fragment = minds_dir / "atlas" / "container" / "compose.yaml"
    test_client.dev = dev
    return test_client


def _volumes(client):
    document = yaml.safe_load(client.fragment.read_text())
    return document["services"]["atlas"]["volumes"]


def test_reading_reports_the_project_folders_apart_from_the_infrastructure_mounts(client):
    body = client.get("/api/minds/atlas/mounts").json()

    assert body["editable"] is True
    assert [(row["path"], row["container_path"], row["writable"]) for row in body["mounts"]] == [
        ("model-testing", "/mnt/dev/model-testing", True)
    ]
    assert [row["container_path"] for row in body["infrastructure"]] == [
        "/usr/src/app",
        "/usr/src/app/minds/atlas",
    ]


def test_saving_writes_the_chosen_folders_and_leaves_every_other_line_intact(client):
    before = client.fragment.read_text().splitlines()

    response = client.put(
        "/api/minds/atlas/mounts",
        json={
            "mounts": [
                {"root": "dev", "path": "model-testing", "writable": True},
                {"root": "dev", "path": "hive_mind/minds", "writable": True},
            ]
        },
    )

    assert response.status_code == 200, response.json()
    assert _volumes(client) == [
        "${HOST_PROJECT_DIR:-.}:/usr/src/app:ro",
        "${HOST_PROJECT_DIR:-.}/minds/atlas:/usr/src/app/minds/atlas:rw",
        "${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:rw",
        "${HOST_DEV_DIR}/hive_mind/minds:/mnt/dev/minds:rw",
    ]
    after = client.fragment.read_text().splitlines()
    # Every line that was not a project mount survives, comments included.
    assert [line for line in before if "HOST_DEV_DIR" not in line] == [
        line for line in after if "HOST_DEV_DIR" not in line
    ]
    assert "      # The project folders the operator granted." in after


def test_a_removed_folder_leaves_the_fragment_without_it(client):
    response = client.put("/api/minds/atlas/mounts", json={"mounts": []})

    assert response.status_code == 200, response.json()
    assert _volumes(client) == [
        "${HOST_PROJECT_DIR:-.}:/usr/src/app:ro",
        "${HOST_PROJECT_DIR:-.}/minds/atlas:/usr/src/app/minds/atlas:rw",
    ]
    assert response.json()["mounts"] == []


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "model-testing/../../outside",
        "escape",
        "escape/anything",
        "/etc",
    ],
)
def test_a_folder_outside_the_declared_roots_is_refused(client, path):
    response = client.put(
        "/api/minds/atlas/mounts",
        json={"mounts": [{"root": "dev", "path": path, "writable": True}]},
    )

    assert response.status_code == 400, response.json()
    # Refused before anything was written: the fragment still holds what it did.
    assert _volumes(client)[-1] == "${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:rw"


def test_creating_a_folder_lands_under_a_declared_root_and_nowhere_else(client):
    made = client.post(
        "/api/minds/atlas/mounts/folders",
        json={"root": "dev", "parent": "model-testing", "name": "fresh-run"},
    )

    assert made.status_code == 200, made.json()
    assert made.json()["path"] == "model-testing/fresh-run"
    assert (client.dev / "model-testing" / "fresh-run").is_dir()

    escaped = client.post(
        "/api/minds/atlas/mounts/folders",
        json={"root": "dev", "parent": "escape", "name": "planted"},
    )
    assert escaped.status_code == 400, escaped.json()
    assert not (client.dev.parent / "outside" / "planted").exists()

    traversed = client.post(
        "/api/minds/atlas/mounts/folders",
        json={"root": "dev", "parent": "", "name": "../planted"},
    )
    assert traversed.status_code == 400, traversed.json()
    assert not (client.dev.parent / "planted").exists()


def test_a_save_that_does_not_parse_back_as_what_was_chosen_refuses_whole(client):
    # A fragment whose service carries the key twice: a YAML reader takes the
    # last block, so a rewrite of the first one parses cleanly while declaring
    # none of the folders that were asked for.
    client.fragment.write_text(
        FRAGMENT.replace(
            "    restart: unless-stopped\n",
            "    volumes:\n      - ${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:rw\n"
            "    restart: unless-stopped\n",
        )
    )
    original = client.fragment.read_text()

    response = client.put(
        "/api/minds/atlas/mounts",
        json={"mounts": [{"root": "dev", "path": "hive_mind", "writable": True}]},
    )

    assert response.status_code == 400, response.json()
    assert client.fragment.read_text() == original


def test_two_folders_that_would_mount_at_one_path_are_refused(client):
    response = client.put(
        "/api/minds/atlas/mounts",
        json={
            "mounts": [
                {"root": "dev", "path": "hive_mind/minds", "writable": True},
                {"root": "dev", "path": "model-testing", "writable": True},
                {"root": "dev", "path": "hive_mind/minds", "writable": False},
            ]
        },
    )

    assert response.status_code == 400, response.json()
    assert "/mnt/dev/minds" in response.json()["detail"]


def test_a_successful_write_asks_hive_tools_to_bring_up_that_one_service(client):
    response = client.put(
        "/api/minds/atlas/mounts",
        json={"mounts": [{"root": "dev", "path": "model-testing", "writable": True}]},
    )

    assert response.status_code == 200, response.json()
    assert response.json()["recreated"] is True
    method, path = main_mod._hive_tools_request.await_args.args[:2]
    params = main_mod._hive_tools_request.await_args.kwargs["params"]
    assert (method, path) == ("POST", "/docker/up")
    assert params == {"path": "/host/hive_mind", "service": "atlas"}


def test_a_recreate_that_fails_is_reported_as_a_failure_not_as_a_save_that_worked(client):
    main_mod._hive_tools_request.return_value = {
        "status": "error",
        "return_code": 1,
        "stderr": "no such service",
    }

    response = client.put(
        "/api/minds/atlas/mounts",
        json={"mounts": [{"root": "dev", "path": "hive_mind", "writable": True}]},
    )

    assert response.status_code == 502, response.json()
    assert "no such service" in response.json()["detail"]
    # The fragment still took the edit — the recreate is what did not happen.
    assert "${HOST_DEV_DIR}/hive_mind:/mnt/dev/hive_mind:rw" in _volumes(client)


def test_a_recreate_waiting_on_approval_is_not_reported_as_applied(client):
    main_mod._hive_tools_request.return_value = {
        "hitl_request_id": "abc-123",
        "status": "pending",
    }

    body = client.put(
        "/api/minds/atlas/mounts",
        json={"mounts": [{"root": "dev", "path": "hive_mind", "writable": True}]},
    ).json()

    assert body["saved"] is True
    assert body["recreated"] is False
    assert body["operation_id"] == "abc-123"


def test_a_read_only_row_is_written_read_only_and_a_writable_row_writable(client):
    client.put(
        "/api/minds/atlas/mounts",
        json={
            "mounts": [
                {"root": "dev", "path": "model-testing", "writable": False},
                {"root": "dev", "path": "hive_mind", "writable": True},
            ]
        },
    )

    assert _volumes(client)[-2:] == [
        "${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:ro",
        "${HOST_DEV_DIR}/hive_mind:/mnt/dev/hive_mind:rw",
    ]
    rows = client.get("/api/minds/atlas/mounts").json()["mounts"]
    assert [(row["name"], row["writable"]) for row in rows] == [
        ("model-testing", False),
        ("hive_mind", True),
    ]


def test_a_mind_that_is_not_a_container_reports_its_mounts_as_not_editable(client):
    body = client.get("/api/minds/edgy/mounts").json()

    assert body["editable"] is False
    assert body["deployment"] == "systemd"
    assert body["mounts"] == []

    refused = client.put(
        "/api/minds/edgy/mounts",
        json={"mounts": [{"root": "dev", "path": "hive_mind", "writable": True}]},
    )
    assert refused.status_code == 400
    assert main_mod._hive_tools_request.await_count == 0
