"""The configuration written for a choice of pieces, as Docker Compose reads it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from installer import compose

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
    assert services["hive-tools"]["environment"]["OLLAMA_BASE_URL"] == \
        "http://gpu.corp.example:11434"


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
