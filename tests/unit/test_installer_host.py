"""The host's web proxy reaches the hive's builds and containers, never its
own traffic."""

from __future__ import annotations

from installer import compose, plan

from tests.unit.installer_helpers import a_host, answers


def test_no_proxy_covers_every_hive_service_and_every_address_given():
    host = a_host(web_proxy={"HTTPS_PROXY": "http://corp-proxy:3128", "NO_PROXY": ".corp"})
    given = answers(pieces=["tools"], modes={"embeddings": "existing", "proxy": "existing",
                           "postgres": "existing", "voice": "existing"},
                    embeddings_url="http://gpu.corp.example:11434",
                    proxy_url="http://10.1.2.3:8888", voice_url="http://voice-box:8422",
                    postgres_host="db.corp.example", proxy_key="k",
                    postgres_user="u", postgres_password="p")

    env = host.proxy_env([*plan.SERVICE_NAMES, "wren", *compose.given_hosts(given)])
    no_proxy = set(env["NO_PROXY"].split(","))

    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://corp-proxy:3128"
    assert env["NO_PROXY"] == env["no_proxy"]
    assert {".corp", "localhost", "wren", *plan.SERVICE_NAMES} <= no_proxy
    assert {"gpu.corp.example", "10.1.2.3", "voice-box", "db.corp.example"} <= no_proxy


def test_no_web_proxy_on_the_host_sets_none():
    assert a_host().proxy_env(plan.SERVICE_NAMES) == {}
