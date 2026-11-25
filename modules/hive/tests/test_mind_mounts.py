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
from fastapi import HTTPException
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


def _revision(client, name="atlas"):
    return client.get("/api/minds/" + name + "/mounts").json()["revision"]


def _save(client, mounts, name="atlas", revision=None):
    return client.put(
        "/api/minds/" + name + "/mounts",
        json={
            "mounts": mounts,
            "revision": revision if revision is not None else _revision(client, name),
        },
    )


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

    response = _save(client, [
                {"root": "dev", "path": "model-testing", "writable": True},
                {"root": "dev", "path": "hive_mind/minds", "writable": True},
            ])

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
    response = _save(client, [])

    assert response.status_code == 200, response.json()
    assert _volumes(client) == [
        "${HOST_PROJECT_DIR:-.}:/usr/src/app:ro",
        "${HOST_PROJECT_DIR:-.}/minds/atlas:/usr/src/app/minds/atlas:rw",
    ]
    assert response.json()["mounts"] == []


@pytest.mark.parametrize(
    "path,reason",
    [
        # A dot-dot is refused as a name before it is resolved at all.
        ("../outside", "not a name this control can write"),
        ("model-testing/../../outside", "not a name this control can write"),
        # A symlink pointing out of the root, and a real folder reached
        # through it: both exist on disk and carry ordinary names, so the only
        # thing that can refuse them is where they resolve to.
        ("escape", "outside Dev"),
        ("escape/anything", "outside Dev"),
        # An absolute path whose tail does exist under the root: refused as
        # absolute rather than quietly answered with that folder.
        ("/model-testing", "relative to its root"),
    ],
)
def test_a_folder_outside_the_declared_roots_is_refused(client, path, reason):
    response = _save(client, [{"root": "dev", "path": path, "writable": True}])

    assert response.status_code == 400, response.json()
    assert reason in response.json()["detail"]
    # Refused before anything was written: the fragment still holds what it did.
    assert _volumes(client)[-1] == "${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:rw"


def test_browsing_names_a_folder_pointing_out_of_the_root_rather_than_dropping_it(client):
    listing = client.get("/api/minds/atlas/mounts/folders?root=dev").json()

    offered = {folder["name"]: folder for folder in listing["folders"]}
    # Present, and said to be unmountable: a browser that hid it could not be
    # trusted about absence either.
    assert offered["escape"]["mountable"] is False
    assert offered["model-testing"]["mountable"] is True
    assert offered["model-testing"]["path"] == "model-testing"


def test_a_volumes_block_this_writer_cannot_rewrite_refuses_rather_than_mangling_it(client):
    # A flow-style list is valid compose and is not what the line rewriter
    # understands, so the save refuses whole and the file is left alone.
    client.fragment.write_text(
        FRAGMENT.replace(
            """    volumes:
      # The hive's own checkout, read-only.
      - ${HOST_PROJECT_DIR:-.}:/usr/src/app:ro
      # The one directory this Mind writes.
      - ${HOST_PROJECT_DIR:-.}/minds/atlas:/usr/src/app/minds/atlas:rw
      # The project folders the operator granted.
      - ${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:rw
""",
            '    volumes: ["${HOST_PROJECT_DIR:-.}:/usr/src/app:ro"]\n',
        )
    )
    original = client.fragment.read_text()

    response = _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}])

    assert response.status_code == 400, response.json()
    assert client.fragment.read_text() == original


def test_the_fragments_own_permissions_survive_the_save(client):
    os.chmod(client.fragment, 0o640)

    _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}])

    assert client.fragment.stat().st_mode & 0o777 == 0o640


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

    response = _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}])

    assert response.status_code == 400, response.json()
    assert client.fragment.read_text() == original


def test_two_folders_that_would_mount_at_one_path_are_refused(client):
    response = _save(client, [
                {"root": "dev", "path": "hive_mind/minds", "writable": True},
                {"root": "dev", "path": "model-testing", "writable": True},
                {"root": "dev", "path": "hive_mind/minds", "writable": False},
            ])

    assert response.status_code == 400, response.json()
    assert "/mnt/dev/minds" in response.json()["detail"]


def test_a_successful_write_asks_hive_tools_to_bring_up_that_one_service(client):
    response = _save(client, [{"root": "dev", "path": "model-testing", "writable": True}])

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

    response = _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}])

    assert response.status_code == 502, response.json()
    assert "no such service" in response.json()["detail"]
    # The fragment still took the edit — the recreate is what did not happen.
    assert "${HOST_DEV_DIR}/hive_mind:/mnt/dev/hive_mind:rw" in _volumes(client)


def test_a_recreate_waiting_on_approval_is_not_reported_as_applied(client):
    main_mod._hive_tools_request.return_value = {
        "hitl_request_id": "abc-123",
        "status": "pending",
    }

    body = _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}]).json()

    assert body["saved"] is True
    assert body["recreated"] is False
    assert body["operation_id"] == "abc-123"


def test_a_read_only_row_is_written_read_only_and_a_writable_row_writable(client):
    _save(client, [
                {"root": "dev", "path": "model-testing", "writable": False},
                {"root": "dev", "path": "hive_mind", "writable": True},
            ])

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

    refused = _save(
        client, [{"root": "dev", "path": "hive_mind", "writable": True}], name="edgy",
        revision="whatever",
    )
    assert refused.status_code == 400
    assert main_mod._hive_tools_request.await_count == 0


def test_a_save_carrying_a_stale_revision_is_refused_and_changes_nothing(client):
    stale = _revision(client)
    _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}])
    written = client.fragment.read_text()

    response = _save(
        client,
        [{"root": "dev", "path": "model-testing", "writable": False}],
        revision=stale,
    )

    assert response.status_code == 409, response.json()
    assert client.fragment.read_text() == written


def test_a_mount_carrying_a_mode_option_is_listed_as_one_this_control_may_not_edit(client):
    # `ro,z` reads as writable under a naive check, and a save that rewrites
    # three fields from a boolean would drop the label and widen the mount.
    client.fragment.write_text(
        FRAGMENT.replace(
            "- ${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:rw",
            "- ${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:ro,z",
        )
    )

    body = client.get("/api/minds/atlas/mounts").json()

    assert body["mounts"] == []
    assert "${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:ro,z" in [
        row["spec"] for row in body["infrastructure"]
    ]
    # And it survives a save of something else, untouched.
    _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}])
    assert "${HOST_DEV_DIR}/model-testing:/mnt/dev/model-testing:ro,z" in _volumes(client)


def test_a_fragment_whose_last_line_has_no_newline_still_takes_a_new_folder(client):
    client.fragment.write_text(
        """services:
  atlas:
    container_name: hive-mind-atlas
    volumes:
      - ${HOST_PROJECT_DIR:-.}:/usr/src/app:ro"""
    )

    response = _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}])

    assert response.status_code == 200, response.json()
    assert _volumes(client) == [
        "${HOST_PROJECT_DIR:-.}:/usr/src/app:ro",
        "${HOST_DEV_DIR}/hive_mind:/mnt/dev/hive_mind:rw",
    ]


def test_a_row_that_does_not_say_whether_it_is_writable_is_refused(client):
    response = _save(client, [{"root": "dev", "path": "hive_mind"}])

    assert response.status_code == 400, response.json()
    assert "writable" in response.json()["detail"]


def test_creating_a_folder_that_is_already_there_is_refused_rather_than_reported_as_made(client):
    response = client.post(
        "/api/minds/atlas/mounts/folders",
        json={"root": "dev", "parent": "", "name": "model-testing"},
    )

    assert response.status_code == 400, response.json()
    assert "already exists" in response.json()["detail"]


def test_a_folder_whose_name_compose_would_reinterpret_is_not_offered(client):
    (client.dev / "dollar$name").mkdir()

    listing = client.get("/api/minds/atlas/mounts/folders?root=dev").json()
    offered = {folder["name"]: folder for folder in listing["folders"]}
    assert offered["dollar$name"]["mountable"] is False

    refused = _save(client, [{"root": "dev", "path": "dollar$name", "writable": True}])
    assert refused.status_code == 400, refused.json()


def test_a_project_folder_deleted_on_the_host_is_reported_as_missing(client):
    (client.dev / "model-testing").rmdir()

    rows = client.get("/api/minds/atlas/mounts").json()["mounts"]

    assert [(row["name"], row["missing"]) for row in rows] == [("model-testing", True)]


def test_a_root_whose_compose_variable_the_project_does_not_define_is_refused(
    client, tmp_path, monkeypatch
):
    # An undefined variable expands to nothing, so the bind source becomes an
    # absolute path at the filesystem root: a folder Docker creates empty and
    # root-owned while the page shows the one that was picked.
    monkeypatch.setenv(
        "MIND_MOUNT_ROOTS",
        json.dumps(
            [
                {
                    "key": "dev",
                    "label": "Dev",
                    "compose_var": "HOST_NOTES_DIR",
                    "console_path": str(client.dev),
                    "container_root": "/mnt/notes",
                }
            ]
        ),
    )

    response = _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}])

    assert response.status_code == 400, response.json()
    assert "HOST_NOTES_DIR" in response.json()["detail"]


def test_a_recreate_this_console_could_not_even_ask_for_still_says_the_file_was_written(client):
    main_mod._hive_tools_request.side_effect = HTTPException(
        status_code=503, detail="Hive Tools authorization is not configured"
    )

    response = _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}])

    assert response.status_code == 502, response.json()
    assert "were written" in response.json()["detail"]
    assert "${HOST_DEV_DIR}/hive_mind:/mnt/dev/hive_mind:rw" in _volumes(client)


def test_a_fragment_reached_through_a_symlink_is_written_through_it(client, tmp_path):
    real = tmp_path / "real-compose.yaml"
    real.write_text(client.fragment.read_text())
    client.fragment.unlink()
    client.fragment.symlink_to(real)

    response = _save(client, [{"root": "dev", "path": "hive_mind", "writable": True}])

    assert response.status_code == 200, response.json()
    assert client.fragment.is_symlink()
    assert "${HOST_DEV_DIR}/hive_mind:/mnt/dev/hive_mind:rw" in real.read_text()


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
