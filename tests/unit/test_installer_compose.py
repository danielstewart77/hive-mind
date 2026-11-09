"""The configuration written for a choice of pieces, as Docker Compose reads it."""

from __future__ import annotations

import json
import os
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
        pieces=["telegram", "voice"],
        modes={"proxy": "existing", "embeddings": "existing", "voice": "existing"},
        proxy_url="http://proxy.corp.example:8888", proxy_key="client-key-1",
        embeddings_url="http://gpu.corp.example:11434", voice_url="http://voice.corp.example:8422",
        telegram_token="123:bot"))
    config = render(inst)
    services = config["services"]

    assert "inference-proxy" not in services and compose.OLLAMA not in services
    assert compose.VOICE not in services
    mind = services["wren"]["environment"]
    assert mind["DSH_PROXY_BASE_URL"] == "http://proxy.corp.example:8888/v1"
    assert mind["DSH_API_KEY"] == "client-key-1"
    assert services[compose.LUCENT]["environment"]["OLLAMA_BASE_URL"] == \
        "http://gpu.corp.example:11434"
    bot = services[compose.TELEGRAM]["environment"]
    assert bot["VOICE_SERVER_URL"] == "http://voice.corp.example:8422"
    assert bot["TELEGRAM_BOT_TOKEN"] == "123:bot"


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
        assert not any(k.startswith("NVIDIA_") for k in svc.get("environment", {})), name


def test_every_mount_lies_inside_the_install_folder_and_exists_and_the_user_is_the_operator(
        tmp_path):
    inst = installer_at(tmp_path, answers(pieces=all_pieces(), modes={"voice": "here"},
                                          telegram_token="t", discord_token="d"),
                        host=a_host(docker_gid=4242))
    config = render(inst)
    root = inst.home.root

    binds = [(name, Path(v["source"])) for name, svc in config["services"].items()
             for v in svc.get("volumes", []) if v.get("type") == "bind"]
    assert binds
    for name, source in binds:
        assert source.resolve().is_relative_to(root), (name, source)
        assert source.exists(), (name, source)
    for name in ("wren", compose.VOICE, "hive-tools"):
        args = config["services"][name]["build"]["args"]
        assert (args["UID"], args["GID"]) == (str(os.getuid()), str(os.getgid())), name
    assert config["services"]["hive-tools"]["build"]["args"]["DOCKER_GID"] == "4242"
