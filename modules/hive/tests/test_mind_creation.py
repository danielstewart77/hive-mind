"""Creating an internal Mind from the console.

Before this existed the console could only check whether a name was free —
every one of the four harness/provider pairs running in this Hive today
(ada, bob, nagatha, bilby) had to be scaffolded by hand.
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import auth
import configuration_data
import credentials_data
import holder_credentials
import main as main_mod
import mind_creation
import accounts

# A container mind starts on a model the inference proxy serves, since that
# is what it calls (story 2.1.9).
#: The one key the person pastes; the hive makes none.
PASTED_KEY = "pasted-mind-key-for-tests-0123456789"

_PAIRS = [
    ("claude_cli", "anthropic", "claude-sonnet-5"),
    ("claude_cli", "ollama", "gpt-oss:20b-32k"),
    ("codex_cli", "openai", "gpt-5.6-terra"),
    ("codex_cli", "ollama", "gpt-oss:20b-32k"),
]


class _Recorder:
    """A stand-in for one service, on a real port.

    A real server rather than a patched helper: what these tests assert is
    the request that went out — its method, its query, its bearer — and a
    stub of our own transport wrapper can only tell us it was called.
    """

    def __init__(self):
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        self.received: list[dict] = []
        self.hitl: str | None = None
        self.refuse = False
        # (method, path) pairs this service refuses, the rest answered.
        self.refusing: set[tuple[str, str]] = set()
        self.listing = False
        # As the real gateway and memory store behave together: the minds
        # registered here are listed, and a mind's node is refused its edge
        # until the gateway holds that mind.
        self.as_a_hive = False
        self.registered: list[dict] = []
        app = FastAPI()

        @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def anything(path: str, request: Request):
            try:
                payload = await request.json()
            except Exception:
                payload = None
            self.received.append({
                "method": request.method,
                "path": "/" + path,
                "query": dict(request.query_params.multi_items() and
                              {k: request.query_params.getlist(k)
                               for k in request.query_params}),
                "json": payload,
                "bearer": (request.headers.get("authorization") or ""
                           ).removeprefix("Bearer "),
            })
            if self.refuse or (request.method, "/" + path) in self.refusing:
                return JSONResponse({"error": "refused"}, status_code=403)
            if self.as_a_hive:
                held = [m["id"] for m in self.registered]
                if (request.method, path) == ("POST", "broker/minds"):
                    self.registered.append(
                        {"id": payload["mind_id"], "name": payload["name"]})
                elif (request.method, path) == ("GET", "broker/minds"):
                    return JSONResponse(self.registered)
                elif request.method == "DELETE" and path.startswith("broker/minds/"):
                    self.registered = [
                        m for m in self.registered if m["name"] != path.split("/")[-1]]
                elif (request.method, path) == ("POST", "graph/upsert") and \
                        payload["mind_id"] not in held:
                    return JSONResponse({
                        "ok": False, "upserted": True, "code": "missing_reference",
                        "detail": "the node landed and the CREATED_BY edge did not",
                    }, status_code=409)
            if self.listing and request.method == "GET":
                return JSONResponse([])
            if self.hitl:
                return JSONResponse({"hitl_request_id": self.hitl})
            return JSONResponse({"ok": True})

        self.app = app


@pytest.fixture()
def hive_tools(monkeypatch):
    from multiuser_support import ThreadedServer

    recorder = _Recorder()
    with ThreadedServer(recorder.app) as server:
        monkeypatch.setenv("HIVE_TOOLS_URL", server.url)
        monkeypatch.setenv("HIVE_TOOLS_TOKEN", "tools-token")
        yield recorder


@pytest.fixture()
def lucent(monkeypatch):
    from multiuser_support import ThreadedServer

    recorder = _Recorder()
    with ThreadedServer(recorder.app) as server:
        monkeypatch.setenv("LUCENT_API_URL", server.url)
        yield recorder


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A console with a whole stand-in hive behind it, all of it in tmp_path:
    the minds folder, the env file a mind's keys go into, the stack file, and
    one server on a real port answering as the gateway, the memory store, the
    inference proxy and Hive Tools. A creation here can finish, and everything
    it writes or sends lands in something this fixture made."""
    from multiuser_support import ThreadedServer

    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    minds_dir = tmp_path / "minds"
    minds_dir.mkdir()
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_DIR", minds_dir)
    monkeypatch.setattr(configuration_data, "HIVE_MIND_MINDS_CONFIG_DIR", minds_dir)
    monkeypatch.setattr(main_mod, "_broker_minds_safe", AsyncMock(return_value=[]))
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"HOST_PROJECT_DIR={tmp_path}\nPROXY_CREDENTIAL_ADMIN_KEY=stand-in-proxy-admin\n")
    files = {"hive_mind": env_file}
    monkeypatch.setattr(credentials_data, "ENV_FILES", files)
    monkeypatch.setattr(holder_credentials, "ENV_FILES", files)
    # Read once at import, from whatever environment the suite was started in.
    monkeypatch.setattr(holder_credentials, "MIND_ENV_LABEL", "hive_mind")
    monkeypatch.setattr(credentials_data, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(_stack(tmp_path, "ada")))
    monkeypatch.setenv("HIVE_MIND_PROJECT_PATH", str(tmp_path))
    for variable in ("HIVE_MIND_STACK_MINDS_PREFIX", "HIVE_MIND_PROXY_URL",
                     "HIVE_MIND_RESERVED_NAMES", "HIVE_MIND_BUILD_ARGS"):
        monkeypatch.delenv(variable, raising=False)
    services = _Recorder()
    services.listing = True
    services.as_a_hive = True
    with ThreadedServer(services.app) as server:
        for variable in ("GATEWAY_API_URL", "LUCENT_API_URL", "INFERENCE_PROXY_URL",
                         "HIVE_TOOLS_URL"):
            monkeypatch.setenv(variable, server.url)
        monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "stand-in-gateway-admin")
        monkeypatch.setenv("LUCENT_ADMIN_BEARER_TOKEN", "stand-in-memory-admin")
        # The proxy's is not in the environment: it is read from the env file.
        monkeypatch.delenv("PROXY_CREDENTIAL_ADMIN_KEY", raising=False)
        monkeypatch.setenv("HIVE_TOOLS_TOKEN", "stand-in-tools-token")
        user = accounts.make_user("daniel", "secret-pass", is_admin=True, replace=True)
        test_client = TestClient(main_mod.app)
        test_client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
        test_client.minds_dir = minds_dir
        test_client.env_file = env_file
        test_client.services = services
        yield test_client


def _create(client, **overrides):
    body = {"name": "atlas", "harness": "claude_cli", "provider": "anthropic",
            "proxy_key": PASTED_KEY}
    body.update(overrides)
    return client.post("/api/minds", json=body)


@pytest.mark.parametrize("harness,provider,model", _PAIRS)
def test_every_pair_scaffolds_a_runnable_mind(client, harness, provider, model):
    response = _create(client, harness=harness, provider=provider)

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["registration"]["model"] == model

    runtime = yaml.safe_load((client.minds_dir / "atlas" / "runtime.yaml").read_text())
    assert runtime["harness"] == harness
    assert runtime["provider"] == provider
    assert runtime["default_model"] == model
    assert runtime["mind_id"] == body["mind_id"]
    assert runtime["remote"] is False

    compose = yaml.safe_load(
        (client.minds_dir / "atlas" / "container" / "compose.yaml").read_text()
    )
    service = compose["services"]["atlas"]
    assert service["container_name"] == "hive-mind-atlas"
    assert service["command"][-2:] == ["-m", "minds.mind_server"]
    assert "hivemind" in service["networks"]


@pytest.mark.parametrize("harness,expected_var", [
    ("claude_cli", "CLAUDE_CONFIG_DIR"),
    ("codex_cli", "CODEX_HOME"),
])
def test_each_harness_gets_the_config_dir_variable_its_cli_reads(
    client, harness, expected_var
):
    provider = "anthropic" if harness == "claude_cli" else "openai"
    assert _create(client, harness=harness, provider=provider).status_code == 200

    compose = yaml.safe_load(
        (client.minds_dir / "atlas" / "container" / "compose.yaml").read_text()
    )
    environment = compose["services"]["atlas"]["environment"]
    assert any(item.startswith(f"{expected_var}=") for item in environment)

    runtime = yaml.safe_load((client.minds_dir / "atlas" / "runtime.yaml").read_text())
    suffix = ".claude" if harness == "claude_cli" else ".codex"
    assert runtime["runtime_config_dir"].endswith(suffix)


def test_a_first_party_mind_gets_no_env_block(client):
    assert _create(client).status_code == 200
    runtime = yaml.safe_load((client.minds_dir / "atlas" / "runtime.yaml").read_text())
    assert "env" not in runtime


def test_an_ollama_mind_calls_the_proxy_even_where_the_hive_names_a_bare_daemon(client, monkeypatch):
    """Story 2.1.9: a mind talks to the proxy with its own key. Pointed at a
    bare Ollama daemon instead, its calls go unmetered and its proxy key is
    sent to a host that is not the proxy."""
    monkeypatch.setenv("OLLAMA_URL", "http://192.168.4.64:11434")
    with patch.object(main_mod, "get_capability_endpoint",
                      new=AsyncMock(return_value="http://192.168.4.64:11434")):
        assert _create(client, provider="ollama").status_code == 200

    runtime = yaml.safe_load((client.minds_dir / "atlas" / "runtime.yaml").read_text())
    # The proxy as this console is told to reach it, never the daemon.
    assert runtime["env"]["ANTHROPIC_BASE_URL"] == os.environ["INFERENCE_PROXY_URL"]


def test_rejects_pairs_the_cli_cannot_serve(client):
    for harness, provider in [("claude_cli", "openai"), ("codex_cli", "anthropic")]:
        response = _create(client, harness=harness, provider=provider)
        assert response.status_code == 400
        assert provider in response.json()["detail"]
    assert not (client.minds_dir / "atlas").exists()


def test_rejects_a_bad_name_before_writing_anything(client):
    for name in ["../bad", "A", "Atlas!", ""]:
        assert _create(client, name=name).status_code == 400
    assert list(client.minds_dir.iterdir()) == []


def test_refuses_to_overwrite_a_name_the_broker_knows(client):
    with patch.object(
        main_mod, "_broker_minds_safe", new=AsyncMock(return_value=[{"name": "atlas"}])
    ):
        response = _create(client)
    assert response.status_code == 409
    assert not (client.minds_dir / "atlas").exists()


def test_refuses_to_overwrite_an_existing_scaffold(client):
    """A mind scaffolded but never registered is invisible to the broker —
    checking only the broker would clobber it."""
    assert _create(client).status_code == 200
    assert _create(client).status_code == 409


def test_check_name_sees_unregistered_scaffolds(client):
    assert client.get("/api/settings/new-mind/check-name?name=atlas").json()["available"]
    assert _create(client).status_code == 200
    assert not client.get(
        "/api/settings/new-mind/check-name?name=atlas"
    ).json()["available"]


def test_register_sends_the_scaffolded_identity_to_the_broker(client):
    assert _create(client, harness="codex_cli", provider="openai").status_code == 200
    mind_id = yaml.safe_load(
        (client.minds_dir / "atlas" / "runtime.yaml").read_text()
    )["mind_id"]

    gateway = AsyncMock(return_value={"status": "registered"})
    with patch.object(main_mod, "_gateway_request", new=gateway):
        response = client.post("/api/minds/atlas/register", json={})

    assert response.status_code == 200
    assert response.json()["registered"] is True
    payload = gateway.await_args.kwargs["json_body"]
    assert payload == {
        "mind_id": mind_id,
        "name": "atlas",
        "gateway_url": "http://atlas:8420",
        "model": "gpt-5.6-terra",
        # Already the broker's vocabulary — no translation step.
        "harness": "codex_cli",
        # A new mind is the registering admin's unless another owner is named.
        "owner": "daniel",
        # The credential the gateway presents to it, minted with the scaffold.
        "session_token": (client.minds_dir / "atlas" / "session_token").read_text().strip(),
    }


@pytest.fixture()
def comms(fake_comms, monkeypatch):
    monkeypatch.setenv("GATEWAY_API_URL", fake_comms.url)
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "service-token")
    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")
    fake_comms.received.clear()
    return fake_comms


def test_register_rejects_a_mind_that_was_never_scaffolded(client):
    response = client.post("/api/minds/ghost/register", json={})
    assert response.status_code == 400


def test_the_form_offers_only_pairs_the_api_accepts(client):
    page = client.get("/minds/new").text
    assert 'value="claude_cli"' in page
    assert 'value="codex_cli"' in page
    # Identity is deliberately not scaffolded here.
    assert "soul" in page.lower()


@pytest.mark.parametrize("harness,provider,model", _PAIRS)
def test_every_harness_logs_in_to_the_proxy_with_the_key_minted_for_this_mind(
    client, harness, provider, model
):
    """Story 2.1.9: the key the mind is given at creation is how it talks to
    the proxy, whatever its harness - read from the variable the mint writes,
    and not stamped over by its runtime env at spawn."""
    import holder_credentials

    assert _create(client, harness=harness, provider=provider).status_code == 200

    own = holder_credentials.variable_name(holder_credentials.SERVICES["proxy"], "atlas")
    compose = yaml.safe_load(
        (client.minds_dir / "atlas" / "container" / "compose.yaml").read_text())
    environment = dict(
        item.split("=", 1) for item in compose["services"]["atlas"]["environment"])
    for login in ("ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "DSH_API_KEY"):
        assert environment[login] == "${" + own + "}"
    assert environment["ANTHROPIC_BASE_URL"] == os.environ["INFERENCE_PROXY_URL"]
    runtime_env = yaml.safe_load(
        (client.minds_dir / "atlas" / "runtime.yaml").read_text()).get("env") or {}
    assert not {"ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "DSH_API_KEY"} & set(runtime_env)


@pytest.mark.parametrize("harness,provider,model", _PAIRS)
def test_a_new_minds_container_sees_only_its_own_folder_and_the_published_code(
    client, harness, provider, model
):
    """DECIDED 2026-11-15 by Daniel: a mind not owned by him never reaches his
    credentials. Its container is built from the published hive-mind tree
    and mounts its own folder alone - not the checkout, its .env, the shared
    keyring or another mind - and keeps its keyring in that folder. The
    folder is its own to write; its compose folder and session credential are
    read-only inside it."""
    assert _create(client, harness=harness, provider=provider).status_code == 200

    service = yaml.safe_load(
        (client.minds_dir / "atlas" / "container" / "compose.yaml").read_text()
    )["services"]["atlas"]
    home = ".claude" if harness == "claude_cli" else ".codex"
    host, own = "${HOST_PROJECT_DIR:-.}/minds/atlas", "/usr/src/app/minds/atlas"
    assert service["volumes"] == [
        f"{host}:{own}:rw",
        f"{host}/container:{own}/container:ro",
        f"{host}/session_token:{own}/session_token:ro",
        # The install's generated skills, read-only: the one folder of the
        # checkout a role's skill may come from.
        "${HOST_PROJECT_DIR:-.}/install/workflow-skills:/usr/src/app/install/workflow-skills:ro",
    ]
    assert (client.minds_dir / "atlas" / home).is_dir()
    # The published tree, at the branch it is published on, and the stage
    # whose code is root's.
    assert service["build"]["context"] == "https://github.com/danielstewart77/hive-mind.git#master"
    assert service["build"]["target"] == "locked-mind"
    environment = dict(item.split("=", 1) for item in service["environment"])
    assert environment["KEY_RING"] == f"{own}/data/keyring"


def test_a_mind_made_holding_the_operator_role_is_made_with_the_folder_writable_and_the_credential(
    client
):
    from hive_site import testing
    import roles_data
    import store

    testing.run(roles_data.seed())
    operator = next(r["id"] for r in testing.run(store.roles_list()) if r["slug"] == "operator")

    assert _create(client, role_ids=[operator]).status_code == 200

    service = yaml.safe_load(
        (client.minds_dir / "atlas" / "container" / "compose.yaml").read_text()
    )["services"]["atlas"]
    folder = "${HOST_PROJECT_DIR:-.}/install/workflow-skills:/usr/src/app/install/workflow-skills"
    assert f"{folder}:rw" in service["volumes"]
    assert f"{folder}:ro" not in service["volumes"]
    assert "ROLES_PUSH_TOKEN=${ROLES_PUSH_TOKEN}" in service["environment"]
    assert "HIVE_SITE_URL=http://hive-site:8710" in service["environment"]


def test_a_mind_made_from_the_console_is_not_locked_and_its_settings_card_reaches_it_with_its_own_key(
    client, monkeypatch,
):
    """Daniel, 2026-11-17: "No such thing as lockdown." And 2026-11-18: "A
    mind has a key and that is it." Nothing in a new mind's runtime file
    says locked, its folder is its own to write so a setting it is sent is
    one it can save, and the console - which has already let the person in
    by their account - sends that mind the one key it was issued, not the
    hive's admin credential, which the mind is never given."""
    import asyncio
    import route_access
    import mind_runtime

    assert _create(client).status_code == 200

    runtime = yaml.safe_load((client.minds_dir / "atlas" / "runtime.yaml").read_text())
    assert "locked_down" not in runtime
    service = yaml.safe_load(
        (client.minds_dir / "atlas" / "container" / "compose.yaml").read_text()
    )["services"]["atlas"]
    environment = dict(item.split("=", 1) for item in service["environment"])
    assert "COMMS_ADMIN_BEARER_TOKEN" not in environment
    issued = credentials_data._read_env_value(
        client.env_file,
        holder_credentials.variable_name(holder_credentials.SERVICES["comms"], "atlas"))
    assert issued and issued != os.environ["COMMS_ADMIN_BEARER_TOKEN"]

    registry = [
        {"id": runtime["mind_id"], "name": "atlas", "gateway_url": runtime["gateway_url"]},
        {"id": "bob-id", "name": "bob", "gateway_url": "http://bob:8420"},
    ]
    monkeypatch.setattr(route_access, "registered_minds", AsyncMock(return_value=registry))
    assert asyncio.run(mind_runtime.admin_headers(runtime["gateway_url"])) == {
        "Authorization": f"Bearer {issued}"}


def test_the_console_never_sends_its_admin_credential_to_a_mind_it_made(client, monkeypatch):
    """However that mind's address is written: it can re-register itself with
    the gateway at any address - a server of its own, or another mind's,
    which the console would then act on for it. So the admin credential is
    withheld from every address its registry row claims, and from wherever
    its container answers."""
    import asyncio
    import route_access
    import mind_runtime

    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")
    assert _create(client).status_code == 200
    mind_id = yaml.safe_load((client.minds_dir / "atlas" / "runtime.yaml").read_text())["mind_id"]
    registry = [
        {"id": mind_id, "name": "atlas", "gateway_url": "http://ada:8420"},
        {"id": "ada-id", "name": "ada", "gateway_url": "http://ada:8420"},
        {"id": "bob-id", "name": "bob", "gateway_url": "http://bob:8420"},
    ]
    monkeypatch.setattr(route_access, "registered_minds", AsyncMock(return_value=registry))
    where = {"atlas": {"10.9.0.7"}, "10.9.0.7": {"10.9.0.7"}, "ada": {"10.9.0.3"},
             "bob": {"10.9.0.2"}}
    monkeypatch.setattr(configuration_data, "_addresses", lambda host: where.get(host, set()))

    for url in ("http://ada:8420", "http://atlas:8420", "http://10.9.0.7:7777"):
        with pytest.raises(main_mod.HTTPException) as refused:
            asyncio.run(mind_runtime.admin_headers(url))
        assert refused.value.status_code == 503, url
    assert asyncio.run(mind_runtime.admin_headers("http://bob:8420")) == {
        "Authorization": "Bearer admin-token"}


def test_the_console_withholds_its_admin_credential_from_minds_the_gateway_withholds_it_from(
    client, monkeypatch,
):
    """A kid's box elsewhere on the network, created nowhere near this
    console, is not to be sent it either; the gateway marks which minds are."""
    import asyncio
    import route_access
    import mind_runtime

    monkeypatch.setenv("COMMS_ADMIN_BEARER_TOKEN", "admin-token")
    registry = [
        {"id": "arnold-id", "name": "arnold", "owner": "sloan",
         "gateway_url": "http://192.168.5.64:8433", "admin_credential_allowed": False},
        {"id": "bob-id", "name": "bob", "owner": "daniel",
         "gateway_url": "http://bob:8420", "admin_credential_allowed": True},
    ]
    monkeypatch.setattr(route_access, "registered_minds", AsyncMock(return_value=registry))

    with pytest.raises(main_mod.HTTPException) as refused:
        asyncio.run(mind_runtime.admin_headers("http://192.168.5.64:8433"))
    assert refused.value.status_code == 503
    assert asyncio.run(mind_runtime.admin_headers("http://bob:8420")) == {
        "Authorization": "Bearer admin-token"}


def test_a_mind_cannot_be_named_after_an_address_on_the_hives_network(client):
    """Its name is its address there: a mind called hive-comms would answer
    some of the calls meant for the gateway."""
    assert _create(client, name="hive-comms").status_code == 409
    assert not (client.minds_dir / "hive-comms").exists()


# ── wave B: the mind is reachable, started, and has a node ───────────────
#
# Before this, creating a mind wrote a compose fragment no `docker compose`
# invocation could see and handed the operator a shell line to run, and the
# mind had no node in the graph at all.


def _stack(tmp_path, *names):
    """A stack file shaped like the real one: an include list, then services."""
    entries = "".join(
        f"  - path: minds/{n}/container/compose.yaml\n" for n in names)
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "include:\n" + entries + "\nservices:\n  hive-postgres:\n"
        "    image: postgres:17  # keep this comment\n")
    return path


def test_a_new_minds_fragment_is_added_to_the_stacks_include_list(tmp_path, monkeypatch):
    """Without the entry the scaffold is a file nothing can start."""
    path = _stack(tmp_path, "ada", "bob")
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(path))

    assert configuration_data.include_mind_in_stack("atlas") is True

    lines = path.read_text().splitlines()
    assert lines[1:4] == [
        "  - path: minds/ada/container/compose.yaml",
        "  - path: minds/bob/container/compose.yaml",
        "  - path: minds/atlas/container/compose.yaml",
    ]
    # The rest of the running stack's definition is left exactly as it was.
    assert "    image: postgres:17  # keep this comment" in path.read_text()


def test_a_stack_file_that_would_stop_parsing_is_left_alone(tmp_path, monkeypatch):
    """The whole file is what `docker compose` refuses, for every service in
    it — and it is generated and gitignored, so there is no diff to notice
    the breakage by and no checkout to recover with."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "include:\n  - path: minds/ada/container/compose.yaml\n"
        "services:\n  hive-postgres:\n    image: postgres:17\n")
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(path))
    before = path.read_text()
    # A list item at the mapping's own indentation is the shape that breaks.
    monkeypatch.setattr(
        configuration_data.yaml, "safe_load",
        lambda _text: (_ for _ in ()).throw(configuration_data.yaml.YAMLError("bad")))

    with pytest.raises(ValueError):
        configuration_data.include_mind_in_stack("atlas")

    assert path.read_text() == before
    assert not list(tmp_path.glob("*.console-new"))


def test_a_mind_already_in_the_include_list_is_not_added_twice(tmp_path, monkeypatch):
    """A retried create must not leave the stack naming one mind twice."""
    path = _stack(tmp_path, "ada", "atlas")
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(path))
    before = path.read_text()

    assert configuration_data.include_mind_in_stack("atlas") is False
    assert path.read_text() == before


def test_the_new_entry_copies_the_indentation_of_the_list_it_joins(tmp_path, monkeypatch):
    """A two-space line in a four-space list is a mixed-indent sequence, and
    `docker compose` then refuses every subcommand for every service in the
    file — one new mind taking the whole running stack down."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "include:\n"
        "    - path: minds/ada/container/compose.yaml\n"
        "\nservices:\n  hive-postgres:\n    image: postgres:17\n")
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(path))

    assert configuration_data.include_mind_in_stack("atlas") is True

    assert yaml.safe_load(path.read_text())["include"] == [
        {"path": "minds/ada/container/compose.yaml"},
        {"path": "minds/atlas/container/compose.yaml"},
    ]


def test_an_entry_keeps_the_keys_that_belong_to_it(tmp_path, monkeypatch):
    """Inserted one line too early, the previous mind's own keys are
    reattached to the new one."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "include:\n"
        "  - path: minds/bob/container/compose.yaml\n"
        "    env_file: minds/bob/.env\n"
        "\nservices:\n  hive-postgres:\n    image: postgres:17\n")
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(path))

    configuration_data.include_mind_in_stack("atlas")

    assert yaml.safe_load(path.read_text())["include"] == [
        {"path": "minds/bob/container/compose.yaml", "env_file": "minds/bob/.env"},
        {"path": "minds/atlas/container/compose.yaml"},
    ]


def test_a_mind_listed_in_another_form_is_still_only_listed_once(tmp_path, monkeypatch):
    """Already-present is the path, not the spelling: a quoted entry or one
    with a trailing comment is the same mind."""
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        "include:\n"
        '  - path: "minds/atlas/container/compose.yaml"  # the new one\n'
        "\nservices:\n  hive-postgres:\n    image: postgres:17\n")
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(path))

    assert configuration_data.include_mind_in_stack("atlas") is False


def test_the_stack_file_keeps_its_owner_and_mode(tmp_path, monkeypatch):
    """This console runs as root over the operator's own checkout. A new
    inode owned by root is a stack file he can no longer edit."""
    path = _stack(tmp_path, "ada")
    path.chmod(0o640)
    before = path.stat()
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(path))

    configuration_data.include_mind_in_stack("atlas")

    after = path.stat()
    assert after.st_mode & 0o7777 == before.st_mode & 0o7777
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_creating_a_mind_lists_it_in_the_stack_and_starts_it_through_hive_tools(
    client, tmp_path, monkeypatch, hive_tools
):
    """Story 2.1.9: the container is started through Hive Tools, whose
    approval is the admin's click, not a shell line pasted into a terminal."""
    path = _stack(tmp_path, "ada")
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(path))
    monkeypatch.setenv("HIVE_MIND_PROJECT_PATH", "/host/Dev/hive_mind")

    body = _create(client).json()

    assert "  - path: minds/atlas/container/compose.yaml" in path.read_text()
    sent = [r for r in hive_tools.received if r["path"] == "/docker/up"]
    assert len(sent) == 1
    assert sent[0]["method"] == "POST"
    assert sent[0]["query"]["service"] == ["atlas"]
    assert sent[0]["query"]["path"] == ["/host/Dev/hive_mind"]
    assert body["started"] is True
    assert body["start_error"] is None


def test_a_start_waiting_on_approval_is_reported_as_waiting_rather_than_started(
    client, tmp_path, monkeypatch, hive_tools
):
    """A container nobody has approved yet is not running, and saying it is
    sends the operator to a mind that is not there."""
    path = _stack(tmp_path, "ada")
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(path))
    monkeypatch.setenv("HIVE_MIND_PROJECT_PATH", "/host/Dev/hive_mind")
    hive_tools.hitl = "op-77"

    body = _create(client).json()

    assert body["started"] is False
    assert body["start_operation"] == "op-77"
    assert "approval" in body["next_step"]


def test_a_start_that_hive_tools_refuses_is_not_reported_as_started(
    client, tmp_path, monkeypatch, hive_tools
):
    """The same lie as calling an unapproved start started: the operator goes
    looking for a mind that is not running."""
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(_stack(tmp_path, "ada")))
    monkeypatch.setenv("HIVE_MIND_PROJECT_PATH", "/host/Dev/hive_mind")
    hive_tools.refuse = True

    body = _create(client).json()

    assert body["started"] is False
    assert body["start_error"]
    assert "is not running" in body["next_step"]


def _everything_a_creation_writes(client, tmp_path):
    """The stand-in hive's files, as text: what a failed creation must leave
    exactly as it found."""
    return {
        "minds": sorted(p.name for p in client.minds_dir.iterdir()),
        "env": client.env_file.read_text(),
        "stack": (tmp_path / "docker-compose.yml").read_text(),
        "records": sorted(p.name for p in mind_creation.CREATION_DIR.glob("*"))
        if mind_creation.CREATION_DIR.exists() else [],
    }


@pytest.mark.parametrize("refused,step", [
    (("PUT", "/credentials/atlas"), "Issuing its credentials"),
    (("POST", "/graph/upsert"), "Its node in the graph"),
    (("POST", "/broker/minds"), "Registering it with the gateway"),
])
def test_a_creation_that_fails_at_a_step_says_not_made_names_the_step_and_leaves_nothing(
    client, tmp_path, refused, step,
):
    """Daniel, 2026-11-17: the page showed "Started" beside three failures,
    for a mind that existed nowhere it could run."""
    before = _everything_a_creation_writes(client, tmp_path)
    client.services.refusing.add(refused)

    response = _create(client)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "atlas was not made" in detail
    assert step in detail
    assert _everything_a_creation_writes(client, tmp_path) == before
    sent = client.services.received
    if refused != ("PUT", "/credentials/atlas"):
        # The rows it made are removed, not merely revoked, on the gateway and
        # the memory store; the proxy is sent nothing, its key is the person's.
        removed = [r for r in sent
                   if (r["method"], r["path"]) == ("DELETE", "/credentials/atlas")]
        assert len(removed) == 2
        assert all(r["query"] == {"remove": ["true"]} for r in removed)
    if refused == ("POST", "/graph/upsert"):
        # The gateway had the mind by then, and no longer does.
        assert client.services.registered == []
        assert [r["path"] for r in sent
                if r["method"] == "DELETE" and r["path"].startswith("/broker/")
                ] == ["/broker/minds/atlas"]
        # A refused node may have landed all the same: it is deleted as the
        # mind that asked for it, before that mind's key is revoked.
        wrote = [r for r in sent if (r["method"], r["path"]) == ("POST", "/graph/upsert")]
        deleted = [r for r in sent if (r["method"], r["path"]) == ("DELETE", "/graph/nodes")]
        assert len(wrote) == 1 and len(deleted) == 1
        assert deleted[0]["bearer"] == wrote[0]["bearer"] != ""
        assert deleted[0]["json"]["name"] == wrote[0]["json"]["name"]
        order = [(r["method"], r["path"]) for r in sent]
        assert order.index(("DELETE", "/graph/nodes")) < order.index(
            ("DELETE", "/credentials/atlas"))


def test_a_creation_that_cannot_be_listed_in_the_stack_says_not_made_and_leaves_nothing(
    client, tmp_path, monkeypatch,
):
    stack = tmp_path / "docker-compose.yml"
    stack.write_text("services:\n  hive-postgres:\n    image: postgres:17\n")
    before = _everything_a_creation_writes(client, tmp_path)

    response = _create(client)

    assert response.status_code == 400
    assert "atlas was not made" in response.json()["detail"]
    assert "Listing it in the stack" in response.json()["detail"]
    assert _everything_a_creation_writes(client, tmp_path) == before


def _lay_out_as_the_installer_does(client, monkeypatch):
    """One env file under the installer's label, and a stack list of its own
    among the minds, empty. Returns that list."""
    import dataclasses

    files = {"nervous_system": client.env_file}
    monkeypatch.setattr(credentials_data, "ENV_FILES", files)
    monkeypatch.setattr(holder_credentials, "ENV_FILES", files)
    monkeypatch.setattr(holder_credentials, "MIND_ENV_LABEL", "nervous_system")
    monkeypatch.setitem(holder_credentials.SERVICES, "proxy", dataclasses.replace(
        holder_credentials.SERVICES["proxy"], admin_file="nervous_system"))
    stack = client.minds_dir / "stack.yaml"
    stack.write_text("include: []\n")
    monkeypatch.setenv("HIVE_MIND_STACK_COMPOSE", str(stack))
    monkeypatch.setenv("HIVE_MIND_STACK_MINDS_PREFIX", ".")
    return stack


def test_a_creation_that_fails_on_a_hive_laid_out_as_the_installer_lays_it_out_leaves_nothing(
    client, monkeypatch,
):
    stack = _lay_out_as_the_installer_does(client, monkeypatch)
    env_before = client.env_file.read_text()
    client.services.refusing.add(("POST", "/broker/minds"))

    response = _create(client, harness="dsh", provider="anthropic")

    assert response.status_code == 502
    assert "atlas was not made" in response.json()["detail"]
    assert client.env_file.read_text() == env_before
    # Still a list compose can read: an `include:` with nothing under it is
    # refused, and the whole stack with it.
    assert stack.read_text() == "include: []\n"
    assert sorted(p.name for p in client.minds_dir.iterdir()) == ["stack.yaml"]


def test_on_a_hive_laid_out_as_the_installer_lays_it_out_a_new_mind_gets_its_keys_its_listing_and_its_registration(
    client, tmp_path, monkeypatch,
):
    """Daniel, 2026-11-17, on an installed hive: "no hive_mind env file is
    configured", "Listed in the stack failed", "Registered with the gateway
    failed". There the keys go into the hive's one env file under another
    label, and the stack list is a file of its own, beside the minds, that
    starts empty."""
    stack = _lay_out_as_the_installer_does(client, monkeypatch)

    response = _create(client, harness="dsh", provider="anthropic")

    assert response.status_code == 200, response.text
    for service in holder_credentials.SERVICES.values():
        key = holder_credentials.variable_name(service, "atlas")
        assert credentials_data._read_env_value(client.env_file, key), key
    assert yaml.safe_load(stack.read_text()) == {
        "include": [{"path": "atlas/container/compose.yaml"}]}
    registered = [r["json"] for r in client.services.received
                  if (r["method"], r["path"]) == ("POST", "/broker/minds")]
    assert [r["name"] for r in registered] == ["atlas"]
    assert registered[0]["harness"] == "dsh"

