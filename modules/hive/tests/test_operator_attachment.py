"""A mind made in the console reaches the install's generated skills, and the
one that holds the operator role can write them (story 858).

Every test drives the real fragment a creation writes, and the real reconcile
and role route, against files under tmp_path. What is stood in for is the far
side of the socket (Hive Tools, which holds the docker socket) and the
registry's answer to who a mind is: nothing else.
"""

import os
import sys
from unittest.mock import AsyncMock

import pytest
import yaml
from hive_site import testing
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import accounts
import auth
import configuration_data
import main as main_mod
import mind_mounts
import operator_attachment
import store

FOLDER = "/usr/src/app/install/workflow-skills"
HOST = "${HOST_PROJECT_DIR:-.}/install/workflow-skills"
CREDENTIAL = "ROLES_PUSH_TOKEN=${ROLES_PUSH_TOKEN}"
CONSOLE = "HIVE_SITE_URL=http://hive-site:8710"

#: A fragment as the console wrote one before this change: no skills folder.
OLDER = """\
services:
  atlas:
    build:
      context: https://example.invalid/hive-mind.git#master
      dockerfile: Dockerfile
      target: locked-mind
    container_name: hive-mind-atlas
    environment:
      - MIND_NAME=atlas
      # Its own key, never another mind's.
      - COMMS_BEARER_TOKEN=${COMMS_TOKEN_ATLAS}
      - HIVE_SITE_URL=http://elsewhere:1
    volumes:
      # Its own folder, and nothing else of the host.
      - ${HOST_PROJECT_DIR:-.}/minds/atlas:/usr/src/app/minds/atlas:rw
      - ${HOST_PROJECT_DIR:-.}/minds/atlas/container:/usr/src/app/minds/atlas/container:ro
    restart: unless-stopped
    networks:
      - hivemind
"""


def _volumes(path, name="atlas"):
    return yaml.safe_load(path.read_text())["services"][name]["volumes"]


def _environment(path, name="atlas"):
    return yaml.safe_load(path.read_text())["services"][name]["environment"]


@pytest.fixture()
def minds(tmp_path, monkeypatch):
    folder = tmp_path / "minds"
    folder.mkdir()
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", folder)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", folder)
    monkeypatch.setenv("HIVE_MIND_PROJECT_PATH", "/host/hive_mind")
    monkeypatch.delenv("MIND_COMPOSE_TARGETS", raising=False)
    monkeypatch.delenv("HIVE_MIND_BUILD_ARGS", raising=False)
    return folder


def _fragment(minds, text=OLDER, name="atlas"):
    path = minds / name / "container" / "compose.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def _made(minds, name="atlas"):
    """The fragment the console writes for a new mind, as it writes it."""
    made = configuration_data.scaffold_internal_mind(
        name, mind_id="id-" + name, harness="claude_cli", provider="anthropic",
        default_model="claude-sonnet-5", owner="daniel")
    return minds / name / "container" / "compose.yaml"


def test_the_compose_a_created_mind_gets_attaches_the_installs_skills_folder_read_only(minds):
    path = _made(minds)

    assert f"{HOST}:{FOLDER}:ro" in _volumes(path)
    # Nothing else of the host's checkout comes with it.
    assert not any(v.startswith("${HOST_PROJECT_DIR:-.}:") for v in _volumes(path))


def test_the_operator_role_makes_the_folder_writable_and_hands_over_the_credential_and_taking_it_away_undoes_it(minds):
    path = _made(minds)
    before = path.read_text()

    given = operator_attachment.reconcile("atlas", True)

    assert given["applicable"] and given["changed"]
    assert f"{HOST}:{FOLDER}:rw" in _volumes(path)
    assert f"{HOST}:{FOLDER}:ro" not in _volumes(path)
    environment = _environment(path)
    assert CREDENTIAL in environment and CONSOLE in environment
    # The credential is a reference to the stack's variable, never a value.
    assert [e for e in environment if e.startswith("ROLES_PUSH_TOKEN=")] == [CREDENTIAL]

    taken = operator_attachment.reconcile("atlas", False)

    assert taken["changed"] is True
    assert path.read_text() == before




def test_a_fragment_made_before_the_folder_existed_gains_it_and_keeps_every_other_line(minds):
    path = _fragment(minds)

    answer = operator_attachment.reconcile("atlas", False)

    assert answer["changed"] is True
    assert f"{HOST}:{FOLDER}:ro" in _volumes(path)
    kept = path.read_text()
    for line in OLDER.splitlines():
        assert line in kept.splitlines()
    # The one line the operator hand-set for the console's address is not
    # taken away from a mind that does not hold the role: it is not ours.
    assert "HIVE_SITE_URL=http://elsewhere:1" in _environment(path)








def test_the_mounts_page_keeps_the_skills_folder_line_as_infrastructure_when_it_saves(minds):
    path = _made(minds)
    operator_attachment.reconcile("atlas", True)
    revision = mind_mounts.read_mounts("atlas")["revision"]

    state = mind_mounts.save_mounts("atlas", [], revision)

    assert f"{HOST}:{FOLDER}:rw" in _volumes(path)
    assert any(row["spec"] == f"{HOST}:{FOLDER}:rw" for row in state["infrastructure"])


# The roles route re-makes the mind.

GATEWAY = "http://atlas.local:8420"


@pytest.fixture()
def console(minds, monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")
    testing.run(roles_data_seed())

    async def registry():
        return [{"name": "atlas", "id": "id-atlas", "owner": "daniel", "gateway_url": GATEWAY}]

    monkeypatch.setattr(main_mod, "_broker_minds_safe", registry)
    monkeypatch.setattr(main_mod, "_hive_tools_request",
                        AsyncMock(return_value={"status": "success", "return_code": 0}))
    user = accounts.make_user("daniel", "secret-pass", is_admin=True, replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    client.fragment = _made(minds)
    return client


def roles_data_seed():
    import roles_data
    return roles_data.seed()


def _role_id(slug):
    return next(r["id"] for r in testing.run(store.roles_list()) if r["slug"] == slug)


def test_giving_a_console_made_mind_the_operator_role_re_makes_it_with_the_folder_writable(console):
    response = console.put("/api/minds/atlas/roles", json={"role_ids": [_role_id("operator")]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert f"{HOST}:{FOLDER}:rw" in _volumes(console.fragment)
    assert CREDENTIAL in _environment(console.fragment)
    assert body["attachments"]["changed"] is True and body["attachments"]["recreated"] is True
    assert "being re-made" in body["attachments"]["detail"]
    method, path = main_mod._hive_tools_request.await_args.args[:2]
    assert (method, path) == ("POST", "/docker/up")
    assert main_mod._hive_tools_request.await_args.kwargs["params"] == {
        "path": "/host/hive_mind", "service": "atlas"}


def test_taking_the_operator_role_away_re_makes_the_mind_with_the_folder_read_only_again(console):
    console.put("/api/minds/atlas/roles", json={"role_ids": [_role_id("operator")]})
    main_mod._hive_tools_request.reset_mock()

    response = console.put("/api/minds/atlas/roles", json={"role_ids": [_role_id("developer")]})

    assert response.status_code == 200, response.text
    assert f"{HOST}:{FOLDER}:ro" in _volumes(console.fragment)
    assert not any(e.startswith(("ROLES_PUSH_TOKEN=", "HIVE_SITE_URL=")) for e in _environment(console.fragment))
    assert main_mod._hive_tools_request.await_count == 1




def test_a_mind_made_before_the_folder_existed_is_re_made_once_when_given_any_role(console):
    console.fragment.write_text(OLDER)

    response = console.put("/api/minds/atlas/roles", json={"role_ids": [_role_id("developer")]})

    assert response.status_code == 200, response.text
    assert f"{HOST}:{FOLDER}:ro" in _volumes(console.fragment)
    assert response.json()["attachments"]["recreated"] is True
    assert main_mod._hive_tools_request.await_count == 1



