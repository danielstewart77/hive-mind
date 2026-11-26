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
