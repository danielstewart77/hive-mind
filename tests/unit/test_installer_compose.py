"""The configuration written for a choice of pieces, as Docker Compose reads it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from installer import compose
from installer.shell import Shell
from installer.steps import Ctx

from tests.unit.installer_helpers import (a_host, all_pieces, answers, installer_at, needs_compose,
                                          render)

pytestmark = needs_compose


def _waits_on(config: dict) -> dict[str, set[str]]:
    return {name: set(svc.get("depends_on") or {}) for name, svc in config["services"].items()}


def test_an_existing_database_starts_no_postgres_and_its_address_reaches_every_user(tmp_path):
    inst = installer_at(tmp_path, answers(
        pieces=["console", "terminal", "tools"], modes={"postgres": "existing"},
        postgres_host="db.corp.example", postgres_port="6543", postgres_db="hivedb",
        postgres_user="hiveuser", postgres_password="p@ss"))
    config = render(inst)

    assert compose.POSTGRES not in config["services"]
    assert all(compose.POSTGRES not in deps for deps in _waits_on(config).values())
    users = {compose.COMMS: "HIVE_DATABASE_URL", compose.LUCENT: "HIVE_DATABASE_URL",
             compose.CONSOLE: "HIVE_DATABASE_URL", compose.TERMINAL: "HIVE_DATABASE_URL",
             "hive-tools": "HIVE_DATABASE_URL", "inference-proxy": "DATABASE_URL"}
    for service, var in users.items():
        assert config["services"][service]["environment"][var] == \
            "postgresql://hiveuser:p%40ss@db.corp.example:6543/hivedb", service


def test_no_voice_starts_no_voice_server_and_no_bot_waits_on_one(tmp_path):
    inst = installer_at(tmp_path, answers(pieces=["telegram", "discord"],
                                          telegram_token="t", discord_token="d"))
    config = render(inst)

    assert compose.VOICE not in config["services"]
    for bot in (compose.TELEGRAM, compose.DISCORD):
        assert compose.VOICE not in _waits_on(config)[bot]
        assert "VOICE_SERVER_URL" not in config["services"][bot]["environment"]


def test_existing_services_and_bot_tokens_land_in_each_service_that_uses_them(tmp_path):
    inst = installer_at(tmp_path, answers(
        pieces=["telegram", "console", "terminal", "tools"],
        modes={"proxy": "existing", "embeddings": "existing", "voice": "existing"},
        proxy_url="http://proxy.corp.example:8888", proxy_key="client-key-1",
        embeddings_url="http://gpu.corp.example:11434", voice_url="http://voice.corp.example:8422",
        telegram_token="123:bot"))
    config = render(inst)
    services = config["services"]

    assert "inference-proxy" not in services and compose.OLLAMA not in services
    assert compose.VOICE not in services
    bot = services[compose.TELEGRAM]["environment"]
    assert bot["VOICE_SERVER_URL"] == "http://voice.corp.example:8422"
    assert bot["TELEGRAM_BOT_TOKEN"] == "123:bot"
    console = services[compose.CONSOLE]["environment"]
    assert console["KOKORO_VOICE_URL"] == "http://voice.corp.example:8422"
    assert services[compose.TERMINAL]["environment"]["VOICE_API_URL"] == \
        "http://voice.corp.example:8422"
    tools_env = services["hive-tools"]["environment"]
    assert tools_env["INFERENCE_PROXY_URL"] == "http://proxy.corp.example:8888"
    assert tools_env["INFERENCE_PROXY_KEY"] == "client-key-1"


def test_the_tools_service_calls_models_through_the_proxy_with_the_hives_classifier(tmp_path):
    inst = installer_at(tmp_path, answers(
        pieces=["tools"], modes={"proxy": "existing"},
        proxy_url="http://proxy.corp.example:8888", proxy_key="client-key-1"))
    Ctx(inst, Shell(inst.home.log)).set_env(**{compose.CLASSIFIER_MODEL_VAR: "classifier-of-record"})

    tools_env = render(inst)["services"]["hive-tools"]["environment"]

    assert tools_env["INFERENCE_PROXY_URL"] == "http://proxy.corp.example:8888"
    assert tools_env["INFERENCE_PROXY_KEY"] == "client-key-1"
    assert tools_env["HIVE_TOOLS_DEFAULT_MODEL"] == "classifier-of-record"


def test_nothing_waits_on_a_service_that_is_not_rendered(tmp_path):
    for modes in ({}, {"postgres": "existing", "proxy": "existing", "embeddings": "existing"}):
        inst = installer_at(tmp_path / str(len(modes)), answers(
            pieces=all_pieces(), modes={"voice": "here", **modes},
            postgres_host="db", postgres_user="u", postgres_password="p",
            proxy_url="http://p:8888", proxy_key="k", embeddings_url="http://e:11434",
            telegram_token="t", discord_token="d"))
        config = render(inst)
        for deps in _waits_on(config).values():
            assert deps <= set(config["services"])


def test_no_service_asks_for_an_nvidia_device_or_runtime(tmp_path):
    inst = installer_at(tmp_path, answers(pieces=all_pieces(), modes={"voice": "here"},
                                          telegram_token="t", discord_token="d"))
    config = render(inst)

    for name, svc in config["services"].items():
        assert "nvidia" not in str(svc.get("runtime", "")).lower(), name
        assert "nvidia" not in json.dumps(svc.get("deploy", {})).lower(), name
        assert "gpus" not in svc, name
        assert "nvidia" not in json.dumps(svc.get("devices", [])).lower(), name
        assert not any(k.startswith("NVIDIA_") for k in svc.get("environment", {})), name


def test_every_mount_lies_inside_the_install_folder_and_exists_and_the_user_is_the_operator(
        tmp_path):
    inst = installer_at(tmp_path, answers(pieces=all_pieces(), modes={"voice": "here"},
                                          telegram_token="t", discord_token="d"),
                        host=a_host(uid=4321, gid=4322, docker_gid=4242))
    config = render(inst)
    root = inst.home.root
    rendered = compose.render(inst.home, inst.answers, inst.host)

    binds = [(name, Path(v["source"])) for name, svc in config["services"].items()
             for v in svc.get("volumes", []) if v.get("type") == "bind"]
    assert binds
    assert rendered.host_paths == [Path("/var/run/docker.sock")]
    for name, source in binds:
        if source in rendered.host_paths:
            continue
        assert source.resolve().is_relative_to(root), (name, source)
        assert source.exists(), (name, source)
    assert rendered.mountpoints
    for path in rendered.mountpoints:
        assert path.is_dir() and path.resolve().is_relative_to(root), path
    for name in ("wren", compose.VOICE, "hive-tools"):
        args = config["services"][name]["build"]["args"]
        assert (args["UID"], args["GID"]) == ("4321", "4322"), name
    assert config["services"]["hive-tools"]["build"]["args"]["DOCKER_GID"] == "4242"
    console = config["services"][compose.CONSOLE]["environment"]
    assert (console["HOST_UID"], console["HOST_GID"]) == ("4321", "4322")


def test_the_host_time_zone_lands_in_every_service_and_nothing_names_sparktobloom(tmp_path):
    inst = installer_at(tmp_path, answers(pieces=all_pieces(), modes={"voice": "here"},
                                          telegram_token="t", discord_token="d"),
                        host=a_host(tz="Pacific/Auckland"))
    config = render(inst)

    for name, svc in config["services"].items():
        assert svc["environment"]["TZ"] == "Pacific/Auckland", name
    assert "sparktobloom" not in json.dumps(config)
    assert "/home/daniel" not in json.dumps(config)


def test_the_console_rewrites_the_installs_own_env_and_tools_can_run_compose_on_it(tmp_path):
    inst = installer_at(tmp_path, answers(pieces=["console", "tools"]))
    config = render(inst)
    root = str(inst.home.root)

    console = config["services"][compose.CONSOLE]
    assert json.loads(console["environment"]["HIVE_ENV_FILES"]) == \
        {"nervous_system": "/env_hive/.env"}
    assert console["environment"]["HIVE_MIND_PROJECT_PATH"] == root
    env_mount = [v for v in console["volumes"] if v["target"] == "/env_hive/.env"]
    assert env_mount and env_mount[0]["source"] == str(inst.home.env_file)
    assert not env_mount[0].get("read_only")
    assert inst.home.env_file.is_file()

    tools_mounts = {v["target"]: v["source"] for v in config["services"]["hive-tools"]["volumes"]}
    assert tools_mounts["/var/run/docker.sock"] == "/var/run/docker.sock"
    assert tools_mounts[root] == root


def test_mind_lucent_hooks_and_console_carry_the_one_proxy_key_and_the_proxy_address(
        tmp_path, monkeypatch):
    """R5: the operator's key for an existing proxy is the hive's only one."""
    inst = installer_at(tmp_path, answers(
        pieces=["console"], modes={"proxy": "existing"},
        proxy_url="http://proxy.corp.example:8888", proxy_key="the-operator-key"))
    services = render(inst)["services"]
    proxy, key = "http://proxy.corp.example:8888", "the-operator-key"

    mind = services["wren"]["environment"]
    lucent = services[compose.LUCENT]["environment"]
    console = services[compose.CONSOLE]["environment"]
    assert (mind["DSH_API_KEY"], mind["MIND_PROXY_KEY"]) == (key, key)
    assert mind["DSH_PROXY_BASE_URL"] == f"{proxy}/v1"
    assert (lucent["INFERENCE_PROXY_URL"], lucent["INFERENCE_PROXY_KEY"]) == (proxy, key)
    assert (console["INFERENCE_PROXY_URL"], console["INFERENCE_PROXY_KEY"]) == (proxy, key)
    assert console["OLLAMA_BASE_URL"] == proxy

    # The capture hook resolves its proxy and key from the mind's own
    # environment, as the generated copy does in the container.
    for name in ("INFERENCE_PROXY_URL", "MIND_PROXY_KEY", "DSH_PROXY_BASE_URL", "DSH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in mind.items():
        monkeypatch.setenv(name, value)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]
                                    / "minds" / "harness" / "dsh_hook_scripts"))
    sys.modules.pop("hive_mind_env", None)
    try:
        import hive_mind_env

        assert hive_mind_env.proxy() == (proxy, key)
    finally:
        sys.modules.pop("hive_mind_env", None)


def test_services_build_from_hive_minds_modules_and_minds_cannot_read_them(tmp_path):
    inst = installer_at(tmp_path, answers(pieces=all_pieces(), modes={"voice": "here"},
                                          telegram_token="t", discord_token="d"))
    services = render(inst)["services"]
    modules = inst.home.root / "repos" / "hive-mind" / "modules"

    builds = {name: svc["build"]["context"] for name, svc in services.items() if "build" in svc}
    assert builds["hive-tools"] == str(modules / "hive-tools")
    assert builds[compose.CONSOLE] == str(modules / "hive")
    assert builds[compose.TERMINAL] == str(modules / "terminal")
    assert builds["inference-proxy"] == str(modules / "inference-proxy")
    console_mounts = {v["target"]: v for v in services[compose.CONSOLE]["volumes"]}
    assert console_mounts["/hive_tools_repo"]["source"] == str(modules / "hive-tools")
    assert console_mounts["/hive_tools_repo"]["read_only"] is True
    assert services[compose.CONSOLE]["environment"]["HIVE_TOOLS_DIR"] == str(modules / "hive-tools")
    for name in ("wren", compose.VOICE, compose.TELEGRAM, compose.DISCORD):
        hidden = {entry.split(":", 1)[0] for entry in services[name]["tmpfs"]}
        assert {"/usr/src/app/modules", "/usr/src/app/.git/modules"} <= hidden, name


def test_the_console_can_make_a_mind_on_an_installed_hive_and_a_rerun_keeps_it(tmp_path):
    """Daniel, 2026-11-17, Add a Mind on an installed hive: no env file for
    the mind's keys, no stack file to list it in, nothing registered. The
    console is told where each goes, can write there, and holds what issues
    a key at each service; what it then lists survives the installer."""
    inst = installer_at(tmp_path, answers(pieces=all_pieces()), host=a_host(
        web_proxy={"HTTP_PROXY": "http://ann:s3cret@proxy.corp:3128"}))  # secret-guard: allow
    config = render(inst)
    home = inst.home
    console = config["services"][compose.CONSOLE]
    env = console["environment"]

    # Its keys: a label the console has a writable file under.
    files = json.loads(env["HIVE_ENV_FILES"])
    env_mount = [v for v in console["volumes"] if v["target"] == files[env["HIVE_MIND_ENV_LABEL"]]]
    assert env_mount[0]["source"] == str(home.env_file) and not env_mount[0].get("read_only")
    written = {line.split("=", 1)[0]: line.split("=", 1)[1].strip("'")
               for line in home.env_file.read_text().splitlines() if "=" in line}
    assert written["HOST_PROJECT_DIR"] == str(home.root)
    for credential in ("COMMS_ADMIN_BEARER_TOKEN", "LUCENT_ADMIN_BEARER_TOKEN"):
        assert env[credential], credential

    # Its folder and its line in the stack: under a mount the console may
    # write, in a list that starts empty.
    writable = [v["target"] for v in console["volumes"]
                if v["source"] == str(home.minds) and not v.get("read_only")]
    assert writable
    listing = compose.minds_stack(home)
    assert env["HIVE_MIND_STACK_COMPOSE"] == f"{writable[0]}/{listing.name}"
    assert listing.read_text() == "include: []\n"
    # The list sits among the minds, so its entries are relative to itself.
    assert listing.parent == home.minds
    assert env["HIVE_MIND_STACK_MINDS_PREFIX"] == "."

    # What the console does there, then the installer run once more. The
    # mind's image is built through the host's web proxy, which the console
    # is handed by name: the fragment it writes is a file anyone can read.
    # As the container gets it: compose's own printout doubles each dollar.
    build_args = json.loads(env["HIVE_MIND_BUILD_ARGS"].replace("$$", "$"))
    assert "s3cret" not in json.dumps(build_args)
    fragment = home.minds / "atlas" / "container" / "compose.yaml"
    fragment.parent.mkdir(parents=True)
    fragment.write_text(json.dumps({"services": {"atlas": {
        "image": "busybox", "build": {"context": ".", "args": build_args}}}}))
    listing.write_text("include:\n  - path: atlas/container/compose.yaml\n")
    # The console holds .env open as one mounted file and writes the mind's
    # keys into it: the rerun must rewrite that same file, not put another
    # in its place.
    env_file_before = home.env_file.stat().st_ino
    again = render(inst)
    assert home.env_file.stat().st_ino == env_file_before
    assert again["services"]["atlas"]["build"]["args"]["HTTP_PROXY"] == \
        "http://ann:s3cret@proxy.corp:3128"  # secret-guard: allow
    assert compose.console_made_minds(home) == ["atlas"]
