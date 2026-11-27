"""A mind made in the console reaches the install's generated skills, and the
one that holds the operator role can write them (story 858).

Every test drives the real fragment a creation writes, and the real reconcile
and role route, against files under tmp_path. What is stood in for is the far
side of the socket (Hive Tools, which holds the docker socket) and the
registry's answer to who a mind is: nothing else.
"""

import json
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


#: The same fragment without the console address somebody set by hand.
CLEAN = OLDER.replace("      - HIVE_SITE_URL=http://elsewhere:1\n", "")


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


def _no_env_block(text):
    return text.replace(
        "    environment:\n      - MIND_NAME=atlas\n      # Its own key, never another mind's.\n"
        "      - COMMS_BEARER_TOKEN=${COMMS_TOKEN_ATLAS}\n", "")


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
    assert CONSOLE in environment and not any(e.startswith("ROLES_PUSH_TOKEN") for e in environment)

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


def _written(path):
    with open(path, encoding="utf-8", newline="") as stream:
        return stream.read()


# What reconcile refuses or leaves alone: nothing is written.

REFUSED = {
    "an anchored list": CLEAN.replace("    volumes:\n", "    volumes: &mounts\n"),
    "a flow list": CLEAN.replace(
        "    environment:\n      - MIND_NAME=atlas\n      # Its own key, never another mind's.\n"
        "      - COMMS_BEARER_TOKEN=${COMMS_TOKEN_ATLAS}\n", "    environment: [MIND_NAME=atlas]\n"),
    "a mapping of variables": CLEAN.replace(
        "      - MIND_NAME=atlas\n      # Its own key, never another mind's.\n"
        "      - COMMS_BEARER_TOKEN=${COMMS_TOKEN_ATLAS}\n", "      MIND_NAME: atlas\n"),
    "the folder mounted some other way": CLEAN.replace(
        "    restart:", "      - /elsewhere:/usr/src/app/install/workflow-skills:ro,z\n    restart:"),
    "an anchored line that is the folder's own": CLEAN.replace(
        "      - ${HOST_PROJECT_DIR:-.}/minds/atlas/container:/usr/src/app/minds/atlas/container:ro\n",
        "      - ${HOST_PROJECT_DIR:-.}/minds/atlas/container:/usr/src/app/minds/atlas/container:ro\n"
        "      - &skills ${HOST_PROJECT_DIR:-.}/install/workflow-skills:"
        "/usr/src/app/install/workflow-skills:ro\n"),
}


@pytest.mark.parametrize("shape", sorted(REFUSED))
def test_a_fragment_in_a_shape_it_cannot_edit_with_certainty_is_refused_and_written_not_at_all(
        minds, shape):
    path = _fragment(minds, REFUSED[shape])
    os.utime(path, (5, 5))

    with pytest.raises(operator_attachment.AttachmentRefused):
        operator_attachment.reconcile("atlas", True)

    assert _written(path) == REFUSED[shape]
    assert path.stat().st_mtime == 5


def test_a_mind_this_console_did_not_make_is_left_alone_and_the_answer_says_so(minds):
    text = OLDER.replace("target: locked-mind", "target: something-else")
    path = _fragment(minds, text)

    answer = operator_attachment.reconcile("atlas", True)

    assert answer["applicable"] is False and answer["changed"] is False
    assert "not made by this console" in answer["detail"]
    assert _written(path) == text


def test_a_fragment_that_is_a_link_is_not_one_this_console_wrote_and_is_left_alone(minds, tmp_path):
    real = tmp_path / "elsewhere.yaml"
    real.write_text(OLDER)
    link = minds / "atlas" / "container" / "compose.yaml"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    answer = operator_attachment.reconcile("atlas", True)

    assert answer["applicable"] is False and answer["changed"] is False
    assert real.read_text() == OLDER and link.is_symlink()


def test_a_container_defined_by_a_different_compose_file_than_the_one_this_console_wrote_is_not_touched(
        minds, tmp_path, monkeypatch):
    mine = _fragment(minds)
    other = tmp_path / "their-project" / "compose.yaml"
    other.parent.mkdir()
    other.write_text(OLDER)
    monkeypatch.setenv("MIND_COMPOSE_TARGETS", json.dumps(
        {"atlas": {"fragment": str(other), "project_path": "/theirs", "service": "atlas"}}))

    answer = operator_attachment.reconcile("atlas", True)

    assert answer["applicable"] is False and answer["changed"] is False
    assert "did not write" in answer["detail"]
    assert _written(mine) == OLDER and _written(other) == OLDER


def test_the_fragments_mode_survives_the_replacement(minds):
    path = _fragment(minds, CLEAN)
    os.chmod(path, 0o640)
    inode = path.stat().st_ino

    assert operator_attachment.reconcile("atlas", True)["changed"] is True

    assert path.stat().st_mode & 0o7777 == 0o640
    assert path.stat().st_ino != inode  # it was replaced, not rewritten in place


COMMENTED = (CLEAN
             .replace("      - COMMS_BEARER_TOKEN=${COMMS_TOKEN_ATLAS}\n",
                      "      - COMMS_BEARER_TOKEN=${COMMS_TOKEN_ATLAS}\n"
                      "      - ROLES_PUSH_TOKEN=${ROLES_PUSH_TOKEN}  # the operator's own\n"
                      "      - HIVE_SITE_URL=http://hive-site:8710 # where the console is\n")
             .replace("/usr/src/app/minds/atlas/container:ro\n",
                      "/usr/src/app/minds/atlas/container:ro\n"
                      "      - ${HOST_PROJECT_DIR:-.}/install/workflow-skills:"
                      "/usr/src/app/install/workflow-skills:rw  # the skills\n"))


def test_an_operators_lines_that_carry_a_trailing_comment_are_still_taken_away_with_the_role(minds):
    path = _fragment(minds, COMMENTED)

    answer = operator_attachment.reconcile("atlas", False)

    assert answer["changed"] is True
    assert f"{HOST}:{FOLDER}:ro" in _volumes(path)
    assert not any(e.startswith(("ROLES_PUSH_TOKEN=", "HIVE_SITE_URL=")) for e in _environment(path))
    # The comment on the line that stays is the operator's still.
    assert "# the skills" in _written(path)


HALF_HANDED = CLEAN.replace(
    "      - COMMS_BEARER_TOKEN=${COMMS_TOKEN_ATLAS}\n",
    "      - COMMS_BEARER_TOKEN=${COMMS_TOKEN_ATLAS}\n"
    "      - HIVE_SITE_URL=http://hive-site:8710  # where the console is\n")


def test_giving_the_operator_role_keeps_a_console_address_line_that_already_carries_a_comment(minds):
    path = _fragment(minds, HALF_HANDED)

    answer = operator_attachment.reconcile("atlas", True)

    assert answer["changed"] is True
    environment = _environment(path)
    assert environment.count(CONSOLE) == 1 and not any(e.startswith("ROLES_PUSH_TOKEN") for e in environment)
    assert "  # where the console is" in _written(path)


NO_LISTS = [
    ("no environment block", _no_env_block(CLEAN)),
    ("an empty environment list", _no_env_block(CLEAN).replace(
        "    volumes:", "    environment: []\n    volumes:")),
    ("no volumes block", CLEAN.replace(
        "    volumes:\n      # Its own folder, and nothing else of the host.\n"
        "      - ${HOST_PROJECT_DIR:-.}/minds/atlas:/usr/src/app/minds/atlas:rw\n"
        "      - ${HOST_PROJECT_DIR:-.}/minds/atlas/container:/usr/src/app/minds/atlas/container:ro\n", "")),
]


@pytest.mark.parametrize("shape,text", NO_LISTS, ids=[n for n, _ in NO_LISTS])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_a_fragment_with_no_such_list_is_given_one_for_the_operator_and_loses_it_again(
        minds, shape, text, newline):
    text = text.replace("\n", newline)
    path = _fragment(minds, text)
    path.write_bytes(text.encode())

    promoted = operator_attachment.reconcile("atlas", True)

    assert promoted["changed"] is True
    assert f"{HOST}:{FOLDER}:rw" in _volumes(path)
    assert CONSOLE in _environment(path)
    written = path.read_bytes().decode()
    # Every line this edit added ends as the file's own lines do.
    assert written.count("\r\n") == written.count("\n") * (1 if newline == "\r\n" else 0)
    assert yaml.safe_load(written)["services"]["atlas"]["restart"] == "unless-stopped"

    demoted = operator_attachment.reconcile("atlas", False)

    assert demoted["changed"] is True
    assert f"{HOST}:{FOLDER}:ro" in _volumes(path)
    assert not any(e.startswith(("ROLES_PUSH_TOKEN=", "HIVE_SITE_URL=")) for e in _environment(path))


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
    assert CONSOLE in _environment(console.fragment)
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





def _hive_tools_calls():
    return main_mod._hive_tools_request.await_count


def test_a_re_make_waiting_on_approval_is_reported_as_waiting_and_the_roles_stay_saved(
        console, monkeypatch):
    main_mod._hive_tools_request.return_value = {"hitl_request_id": "op-1", "status": "pending"}
    recorded = []

    async def record(result, user):
        recorded.append(result["hitl_request_id"])

    monkeypatch.setattr(main_mod.console_access, "record_operation_from", record)

    response = console.put("/api/minds/atlas/roles", json={"role_ids": [_role_id("operator")]})

    assert response.status_code == 200, response.text
    attachments = response.json()["attachments"]
    assert attachments["recreated"] is False and attachments["operation_id"] == "op-1"
    assert "waiting on your approval" in attachments["detail"]
    assert recorded == ["op-1"]
    held = console.get("/api/minds/atlas/roles").json()["roles"]
    assert "operator" in [r["slug"] for r in held]


@pytest.mark.parametrize("how", ["an error answer", "a raised failure"])
def test_a_re_make_that_did_not_happen_says_so_and_the_roles_stay_saved(console, how):
    from fastapi import HTTPException
    if how == "an error answer":
        main_mod._hive_tools_request.return_value = {"status": "error", "error": "boom"}
    else:
        main_mod._hive_tools_request.side_effect = HTTPException(status_code=502, detail="tools down")

    response = console.put("/api/minds/atlas/roles", json={"role_ids": [_role_id("operator")]})

    assert response.status_code == 200, response.text
    attachments = response.json()["attachments"]
    assert attachments["changed"] is True and attachments["recreated"] is False
    assert ("boom" if how == "an error answer" else "tools down") in attachments["detail"]
    assert "did not happen" in attachments["detail"]
    held = console.get("/api/minds/atlas/roles").json()["roles"]
    assert "operator" in [r["slug"] for r in held]


def test_saving_the_same_roles_a_second_time_does_not_re_make_the_mind_again(console):
    body = {"role_ids": [_role_id("operator")]}
    console.put("/api/minds/atlas/roles", json=body)
    assert _hive_tools_calls() == 1

    second = console.put("/api/minds/atlas/roles", json=body)

    assert second.status_code == 200
    assert second.json()["attachments"]["changed"] is False
    assert _hive_tools_calls() == 1


def test_a_fragment_that_cannot_be_edited_still_saves_the_roles_says_so_and_re_makes_nothing(console):
    console.fragment.write_text(REFUSED["an anchored list"])

    response = console.put("/api/minds/atlas/roles", json={"role_ids": [_role_id("operator")]})

    assert response.status_code == 200, response.text
    attachments = response.json()["attachments"]
    assert attachments["changed"] is False and attachments["recreated"] is False
    assert "roles are saved" in attachments["detail"] and "not changed" in attachments["detail"]
    assert _hive_tools_calls() == 0
    assert console.fragment.read_text() == REFUSED["an anchored list"]
    held = console.get("/api/minds/atlas/roles").json()["roles"]
    assert "operator" in [r["slug"] for r in held]


def test_deleting_the_operator_role_takes_the_folder_back_to_read_only_and_re_makes_each_holder(console):
    operator = _role_id("operator")
    console.put("/api/minds/atlas/roles", json={"role_ids": [operator]})
    assert f"{HOST}:{FOLDER}:rw" in _volumes(console.fragment)
    main_mod._hive_tools_request.reset_mock()

    response = console.delete(f"/api/roles/{operator}")

    assert response.status_code == 200, response.text
    assert f"{HOST}:{FOLDER}:ro" in _volumes(console.fragment)
    assert not any(e.startswith(("ROLES_PUSH_TOKEN=", "HIVE_SITE_URL="))
                   for e in _environment(console.fragment))
    assert response.json()["attachments"]["atlas"]["recreated"] is True
    assert _hive_tools_calls() == 1
    assert main_mod._hive_tools_request.await_args.kwargs["params"]["service"] == "atlas"
