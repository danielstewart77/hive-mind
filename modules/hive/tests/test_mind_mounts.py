"""The project folders a containerised Mind can reach.

Every test here drives the real fragment reader against a real file on
disk: what is stubbed is the far side of the socket (Hive Tools, which holds
the docker socket) and nothing else.

No test here calls the code that saves mounts or creates a folder: that code's job is to write into the
running hive, and a test that wraps it performs the write (see "Code whose
job is to write into production" in the code skill's test-standard.md;
Daniel, 2026-11-17). Those tests were deleted, not redirected, and the
behaviour is unprotected until he has agreed how it can be tested safely.
"""

import json
import os
import sys
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import configuration_data
import main as main_mod
import accounts

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
    (minds_dir / "edgy" / "container").mkdir(parents=True)
    (minds_dir / "edgy" / "container" / "compose.yaml").write_text(
        FRAGMENT.replace("atlas", "edgy")
    )
    (minds_dir / "edgy" / "runtime.yaml").write_text(
        "name: edgy\nharness: claude_cli\ndeployment: systemd\nremote: false\n"
    )

    dev = tmp_path / "Dev"
    (dev / "model-testing").mkdir(parents=True)
    (dev / "hive_mind" / "minds").mkdir(parents=True)
    (dev / "secrets-elsewhere").mkdir()
    (tmp_path / "outside" / "anything").mkdir(parents=True)
    (dev / "escape").symlink_to(tmp_path / "outside")

    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("HIVE_MIND_PROJECT_PATH", "/host/hive_mind")
    env_file = tmp_path / "hive_mind.env"
    env_file.write_text("HOST_DEV_DIR=/host/Dev\nLUCENT_BEARER_TOKEN=x\n")
    monkeypatch.setenv(
        "MIND_COMPOSE_TARGETS",
        json.dumps(
            {
                "atlas": {
                    "fragment": str(minds_dir / "atlas" / "container" / "compose.yaml"),
                    "project_path": "/host/hive_mind",
                    "service": "atlas",
                    "env_file": str(env_file),
                }
            }
        ),
    )
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
    user = accounts.make_user("daniel", "secret-pass", is_admin=True, replace=True)
    test_client = TestClient(main_mod.app)
    test_client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    test_client.fragment = minds_dir / "atlas" / "container" / "compose.yaml"
    test_client.dev = dev
    return test_client


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


def test_browsing_names_a_folder_pointing_out_of_the_root_rather_than_dropping_it(client):
    listing = client.get("/api/minds/atlas/mounts/folders?root=dev").json()

    offered = {folder["name"]: folder for folder in listing["folders"]}
    # Present, and said to be unmountable: a browser that hid it could not be
    # trusted about absence either.
    assert offered["escape"]["mountable"] is False
    assert offered["model-testing"]["mountable"] is True
    assert offered["model-testing"]["path"] == "model-testing"


def test_a_project_folder_deleted_on_the_host_is_reported_as_missing(client):
    (client.dev / "model-testing").rmdir()

    rows = client.get("/api/minds/atlas/mounts").json()["mounts"]

    assert [(row["name"], row["missing"]) for row in rows] == [("model-testing", True)]


def test_a_malformed_mount_root_configuration_is_reported_rather_than_raised(
    client, monkeypatch
):
    monkeypatch.setenv("MIND_MOUNT_ROOTS", json.dumps([{"key": "dev", "label": "Dev"}]))

    response = client.get("/api/minds/atlas/mounts")

    assert response.status_code == 400, response.json()
    assert "compose_var" in response.json()["detail"]


def test_a_container_whose_compose_file_this_console_cannot_reach_shows_no_control(client):
    # Mordecai is a container, and his mounts live in his own checkout's
    # compose project. That is a control that does not apply, not an error.
    client.fragment.unlink()

    body = client.get("/api/minds/atlas/mounts").json()

    assert body["editable"] is False
    assert body["deployment"] == "container"
    assert "not defined by a compose file" in body["detail"]
