"""Where an installed mind's key lives: a folder of its own, outside the checkout.

Requirement 21. Rendered by the installer's own `compose.render`, so no docker
is needed (the sibling file reads the result back through docker compose)."""

from __future__ import annotations

from installer import compose, secrets_store

from tests.unit.installer_helpers import answers, installer_at


def test_each_mind_mounts_only_its_own_key_folder_read_only_and_the_env_file_holds_no_mind_key(tmp_path):
    """Requirement 21: the mind's key is a file in a folder outside the
    checkout that only that mind mounts; no other mind's folder, nor the folder
    of them all, is in its volumes; the console alone writes the root; and the
    install's .env carries no key for it. Rendered by the installer's own
    `compose.render` (no docker needed)."""
    inst = installer_at(tmp_path, answers(pieces=["console", "tools"]))
    home = inst.home
    generated = secrets_store.resolve(home, compose.secret_names(inst.answers, True))
    (home.mind_keys("wren")).mkdir(parents=True)
    (home.mind_keys("wren") / "key").write_text("the-proxy-key-0123456789\n")
    values = compose.env_values(home, inst.answers, generated, {"MIND_KEY_WREN": "an-old-line-0123456789"},
                                extra={"MIND_PROXY_KEY": "the-proxy-key-0123456789"})
    services = compose.render(home, inst.answers, inst.host).compose["services"]

    mind = services["wren"]
    mounts = {v["target"]: v for v in mind["volumes"]}
    key_mount = mounts["/run/mind-key"]
    assert key_mount["source"] == str(home.keys / "wren") and key_mount["read_only"] is True
    # Not the folder of every mind's keys, and not inside the checkout every mind mounts.
    assert all(v["source"] != str(home.keys) for v in mind["volumes"])
    assert not str(home.keys).startswith(str(home.repo("hive-mind")))
    assert mind["environment"]["MIND_KEY_FILE"] == "/run/mind-key/key"
    assert "MIND_KEY" not in mind["environment"]
    # The console writes the root; nothing else mounts it.
    writable = [name for name, svc in services.items()
                if any(v["source"] == str(home.keys) and not v.get("read_only")
                       for v in svc.get("volumes", []))]
    assert writable == [compose.CONSOLE]
    assert services[compose.CONSOLE]["environment"]["HIVE_KEY_ROOT"] == "/mind_keys"
    # And the .env: a line an older install kept is dropped once its file exists.
    assert "MIND_KEY_WREN" not in values and values["HOST_KEY_DIR"] == str(home.keys)


def test_the_installers_own_copy_never_keeps_the_minds_key_so_a_replacement_leaves_no_old_key_in_state(tmp_path):
    """Requirement 22: state/secrets.env is out of the console's reach, so it
    must not hold the mind's key (nor the services' key when it equals it), or
    a replaced key would survive there. A line an earlier install put there is
    dropped by the next write; other secrets are kept."""
    inst = installer_at(tmp_path, answers(pieces=["console"]))
    home = inst.home
    key = "the-minds-key-0123456789"
    home.state.mkdir(parents=True, exist_ok=True)
    home.secrets.write_text(f"MIND_PROXY_KEY='{key}'\nSERVICE_PROXY_KEY='{key}'\nHIVE_SECRET_KEY='keep-me'\n")

    secrets_store.remember(home, {"MIND_PROXY_KEY": key, "SERVICE_PROXY_KEY": key,
                                  "HIVE_TOOLS_TOKEN": "tools-token-abcdef"})

    kept = secrets_store.parse_env(home.secrets)
    assert key not in home.secrets.read_text()
    assert kept["HIVE_SECRET_KEY"] == "keep-me" and kept["HIVE_TOOLS_TOKEN"] == "tools-token-abcdef"
    # A services' key of its own (not the mind's) is still kept.
    secrets_store.remember(home, {"MIND_PROXY_KEY": key, "SERVICE_PROXY_KEY": "services-own-0123456789"})
    assert secrets_store.parse_env(home.secrets)["SERVICE_PROXY_KEY"] == "services-own-0123456789"


def test_resolve_also_drops_a_mind_key_an_earlier_install_left_in_the_installers_copy(tmp_path):
    inst = installer_at(tmp_path, answers(pieces=["console"]))
    home = inst.home
    home.state.mkdir(parents=True, exist_ok=True)
    home.secrets.write_text("MIND_PROXY_KEY='stale-old-key-0123456789'\nHIVE_SECRET_KEY='keep-me'\n")

    secrets_store.resolve(home, ["HIVE_SECRET_KEY"])

    assert "stale-old-key-0123456789" not in home.secrets.read_text()
    assert secrets_store.parse_env(home.secrets)["HIVE_SECRET_KEY"] == "keep-me"
