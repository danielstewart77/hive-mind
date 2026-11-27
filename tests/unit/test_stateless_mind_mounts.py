"""The mounts check: which minds on a machine can read a secret that is not their own.

The check is imported and run for real over compose files the test writes in a
temporary install; the install's own renderer and the tracked example mind are
run through it too, so what a new mind is made with is held to the same rule.
Nothing here touches a real install or a real container.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
import yaml

from installer import compose
from tests.unit.installer_helpers import a_host, answers, installer_at

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "mind_mounts", REPO / "tools/stateless/mind_mounts/mind_mounts.py")
mind_mounts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mind_mounts)

ADMIN = "the-gateway-admin-value"


@pytest.fixture(autouse=True)
def _no_ambient(monkeypatch):
    for name in ("HOST_PROJECT_DIR", "HOST_DEV_DIR", "HOST_KEY_DIR", "HOST_CODE_DIR",
                 "HIVE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)


def _install(tmp_path: Path, minds=("ada", "bea")) -> Path:
    """An install folder: a settings file, a shared keyring, two minds, a key
    folder for each, and a clean clone of the code."""
    root = tmp_path / "install"
    (root / "data" / "keyring").mkdir(parents=True)
    (root / ".env").write_text(f"COMMS_ADMIN_BEARER_TOKEN={ADMIN}\n")
    (root / "repos" / "hive-mind").mkdir(parents=True)
    for name in minds:
        (root / "minds" / name / "container").mkdir(parents=True)
        (root / "minds" / name / "runtime.yaml").write_text(f"name: {name}\nmind_id: id-{name}\n")
        (root / "keys" / name).mkdir(parents=True)
    return root


def _fragment(root: Path, name: str, volumes: list[str], environment=()) -> Path:
    path = root / "minds" / name / "container" / "compose.yaml"
    path.write_text(yaml.safe_dump({"services": {name: {
        "environment": [f"MIND_NAME={name}", f"MIND_ID=id-{name}",
                        "MIND_KEY_FILE=/run/mind-key/key", *environment],
        "volumes": volumes,
    }}}, sort_keys=False))
    return path


def _own(root: Path, name: str) -> list[str]:
    return [f"{root}/minds/{name}:/usr/src/app/minds/{name}:rw",
            f"{root}/keys/{name}:/run/mind-key:ro"]


def _check(root: Path, *extra: str, capsys) -> tuple[int, dict]:
    code = mind_mounts.main(["check", "--root", str(root), *extra])
    return code, json.loads(capsys.readouterr().out)


def test_a_mind_that_mounts_the_install_folder_or_is_handed_the_admin_credential_is_named_and_the_check_fails(
        tmp_path, capsys):
    root = _install(tmp_path)
    _fragment(root, "ada", [f"{root}:/usr/src/app:rw", *_own(root, "ada")])
    _fragment(root, "bea", _own(root, "bea"),
              environment=[f"COMMS_ADMIN_BEARER_TOKEN={ADMIN}"])

    code, report = _check(root, "--operators", "nobody", capsys=capsys)

    assert code == 1 and report["ok"] is False
    by_mind = {}
    for found in report["findings"]:
        by_mind.setdefault(found["mind"], []).append(found)
    assert set(by_mind) == {"ada", "bea"}
    ada = by_mind["ada"][0]
    assert ada["kind"] == "mount" and ada["line"] and f"{root}:/usr/src/app:rw" in ada["entry"]
    assert any("settings file" in r for r in ada["reasons"])
    assert any("another mind's folder" in r for r in ada["reasons"])
    bea = by_mind["bea"][0]
    assert bea["kind"] == "environment" and bea["entry"] == "COMMS_ADMIN_BEARER_TOKEN"
    assert ADMIN not in json.dumps(report)


def test_a_mind_that_mounts_only_its_own_folder_and_key_folder_passes(tmp_path, capsys):
    root = _install(tmp_path)
    _fragment(root, "ada", _own(root, "ada"))
    _fragment(root, "bea", _own(root, "bea"))

    code, report = _check(root, "--operators", "nobody", capsys=capsys)

    assert code == 0 and report["findings"] == []
    assert report["checked"] == ["ada", "bea"]


def test_a_mind_with_the_operator_role_is_never_listed_and_the_role_is_read_from_the_mind_id(
        tmp_path, capsys):
    root = _install(tmp_path)
    _fragment(root, "ada", [f"{root}:/usr/src/app:rw", *_own(root, "ada")],
              environment=[f"COMMS_ADMIN_BEARER_TOKEN={ADMIN}"])
    _fragment(root, "bea", [f"{root}:/usr/src/app:rw", *_own(root, "bea")])

    code, report = _check(root, "--operators", "id-ada", capsys=capsys)

    assert report["operators"] == ["ada"]
    assert {f["mind"] for f in report["findings"]} == {"bea"} and code == 1


def test_a_settings_file_hidden_inside_the_code_mount_is_not_reached_but_an_unhidden_one_is(
        tmp_path, capsys):
    root = _install(tmp_path, minds=("ada",))
    code_dir = tmp_path / "clone"
    (code_dir / "minds").mkdir(parents=True)
    (code_dir / ".env").write_text("X=1\n")
    secret = ["--secret", str(code_dir / ".env")]

    _fragment(root, "ada", [f"{code_dir}:/usr/src/app:ro", *_own(root, "ada")])
    code, report = _check(root, *secret, "--operators", "nobody", capsys=capsys)
    assert code == 1 and any("settings file" in r for f in report["findings"] for r in f["reasons"])

    _fragment(root, "ada", [f"{code_dir}:/usr/src/app:ro", *_own(root, "ada"),
                            "/dev/null:/usr/src/app/.env:ro"])
    code, report = _check(root, *secret, "--operators", "nobody", capsys=capsys)
    assert code == 0, report


def test_a_mount_the_check_cannot_place_is_a_finding_not_a_pass(tmp_path, capsys):
    root = _install(tmp_path, minds=("ada",))
    _fragment(root, "ada", ["${SOMEWHERE_UNSET}:/usr/src/app:ro", *_own(root, "ada")])

    code, report = _check(root, "--operators", "nobody", capsys=capsys)

    assert code == 1
    assert any("cannot work out" in r for f in report["findings"] for r in f["reasons"])


def test_it_stops_rather_than_guess_when_it_cannot_tell_who_is_an_operator(tmp_path, capsys):
    root = _install(tmp_path, minds=("ada",))
    _fragment(root, "ada", _own(root, "ada"))

    code = mind_mounts.main(["check", "--root", str(root)])
    out = json.loads(capsys.readouterr().out)

    assert code == 2 and out["kind"] == "Stop" and "operator" in out["error"]


def test_the_container_the_installer_makes_for_a_mind_without_the_operator_role_passes(tmp_path, capsys):
    inst = installer_at(tmp_path, answers(pieces=["console", "tools", "telegram"],
                                          telegram_token="t"), a_host())
    home = inst.home
    rendered = compose.render(home, {**inst.answers, **inst.typed}, inst.host)
    compose.prepare_mounts(rendered)
    (home.root / ".env").write_text(f"COMMS_ADMIN_BEARER_TOKEN={ADMIN}\n")
    (home.minds / "wren").mkdir(parents=True, exist_ok=True)
    (home.minds / "wren" / "runtime.yaml").write_text("name: wren\n")
    stack = tmp_path / "stack.json"
    stack.write_text(json.dumps(rendered.compose))

    code, report = _check(home.root, "--compose", str(stack), "--operators", "nobody",
                          capsys=capsys)

    assert report["checked"] == ["wren"]
    assert code == 0 and report["findings"] == [], report["findings"]


def test_the_tracked_example_mind_passes_where_the_whole_checkout_mount_it_replaced_did_not(
        tmp_path, capsys, monkeypatch):
    root = _install(tmp_path, minds=("example",))
    code_dir = root / "repos" / "hive-mind"
    (code_dir / "minds" / "example").mkdir(parents=True)
    monkeypatch.setenv("HOST_PROJECT_DIR", str(root))
    monkeypatch.setenv("HOST_KEY_DIR", str(root / "keys"))
    monkeypatch.setenv("HOST_CODE_DIR", str(code_dir))
    monkeypatch.setenv("HOST_DSH_DIR", str(root / "dsh"))
    template = root / "minds" / "example" / "container" / "compose.yaml"
    template.write_text((REPO / "minds/example/container/compose.yaml").read_text())

    code, report = _check(root, "--compose", str(template), "--operators", "nobody", capsys=capsys)
    assert code == 0 and report["findings"] == [], report["findings"]

    old = template.read_text().replace(
        "${HOST_CODE_DIR:?set HOST_CODE_DIR to a clean clone of the repository}:/usr/src/app:ro",
        "${HOST_PROJECT_DIR:-.}:/usr/src/app:rw")
    assert old != template.read_text()
    template.write_text(old)
    code, report = _check(root, "--compose", str(template), "--operators", "nobody", capsys=capsys)
    assert code == 1 and report["findings"]


async def test_the_operator_role_is_read_from_the_hive_database(tmp_path, capsys, monkeypatch):
    hive_data = pytest.importorskip("hive_data")
    pytest.importorskip("asyncpg")
    admin_dsn = os.environ.get("HIVE_DATA_TEST_ADMIN_DSN", "").strip()
    if not admin_dsn:
        pytest.skip("HIVE_DATA_TEST_ADMIN_DSN is unset")
    import asyncio

    from hive_data import migrate, roles
    from hive_data.testing import claimed_database

    root = _install(tmp_path)
    _fragment(root, "ada", [f"{root}:/usr/src/app:rw", *_own(root, "ada")])
    _fragment(root, "bea", [f"{root}:/usr/src/app:rw", *_own(root, "bea")])
    async with claimed_database(admin_dsn, "staff_ledger_test_") as dsn:
        await migrate.apply(dsn)
        monkeypatch.setenv("HIVE_DATABASE_URL", dsn)
        await hive_data.connect(verify=False)
        try:
            await roles.create_role("Operator", slug="operator")
            role = await roles.role_by_slug("operator")
            await roles.set_mind_roles("id-ada", [role["id"]])
            await roles.set_mind_roles("id-bea", [])
        finally:
            await hive_data.close()

        code = await asyncio.to_thread(mind_mounts.main, ["check", "--root", str(root)])
        report = json.loads(capsys.readouterr().out)

    assert report["operators"] == ["ada"]
    assert {f["mind"] for f in report["findings"]} == {"bea"} and code == 1


def _reasons(report: dict) -> str:
    return " | ".join(r for f in report["findings"] for r in f["reasons"]) + " | " + \
        " | ".join(f["entry"] for f in report["findings"])


def test_each_way_of_reaching_a_secret_is_named_by_the_check(tmp_path, capsys):
    root = _install(tmp_path, minds=("ada", "bea"))
    sets = {
        "another mind's key folder": [f"{root}/keys/bea:/k:ro", *_own(root, "ada")],
        "the whole key root": [f"{root}/keys:/k:ro", *_own(root, "ada")],
        "the shared keyring": [f"{root}/data:/d:rw", *_own(root, "ada")],
        "the Docker socket": ["/var/run/docker.sock:/var/run/docker.sock", *_own(root, "ada")],
        "a session_token": [f"{root}/minds/ada/session_token:/t:ro", *_own(root, "ada")],
        "a long-form bind": [{"type": "bind", "source": str(root), "target": "/usr/src/app"}],
    }
    for label, volumes in sets.items():
        _fragment(root, "ada", volumes)
        code, report = _check(root, "--operators", "nobody", capsys=capsys)
        assert code == 1 and report["findings"], label
    # A tmpfs on a folder hides it; the same tmpfs on a parent does not.
    masks = ["/dev/null:/usr/src/app/.env:ro"]
    for tmpfs, clean in ((["/usr/src/app/minds/bea", "/usr/src/app/data", "/usr/src/app/keys"], True),
                         (["/usr"], False)):
        path = _fragment(root, "ada", [f"{root}:/usr/src/app:ro", *masks, *_own(root, "ada")])
        doc = yaml.safe_load(path.read_text())
        doc["services"]["ada"]["tmpfs"] = tmpfs
        path.write_text(yaml.safe_dump(doc))
        code, report = _check(root, "--operators", "nobody", capsys=capsys)
        assert (code == 0) is clean, report["findings"]


def test_env_file_and_a_dict_environment_with_a_passed_through_credential_are_named(tmp_path, capsys):
    root = _install(tmp_path, minds=("ada",))
    path = _fragment(root, "ada", _own(root, "ada"))
    doc = yaml.safe_load(path.read_text())
    doc["services"]["ada"]["env_file"] = [".env"]
    doc["services"]["ada"]["environment"] = {"MIND_NAME": "ada", "MIND_KEY_FILE": "/run/mind-key/key",
                                             "GATEWAY_TOKEN": None, "EMPTY_TOKEN": ""}
    path.write_text(yaml.safe_dump(doc))

    code, report = _check(root, "--operators", "nobody", capsys=capsys)

    entries = {f["entry"] for f in report["findings"]}
    assert code == 1 and entries == {"env_file", "GATEWAY_TOKEN"}, entries


def test_an_override_file_that_only_adds_a_mount_to_a_minds_service_is_checked(tmp_path, capsys) -> None:
    root = tmp_path / "install"
    (root / "minds" / "ada").mkdir(parents=True)
    (root / ".env").write_text("X=1\n")
    override = tmp_path / "override.yaml"
    override.write_text(f"services:\n  ada:\n    volumes:\n      - {root}:/x:rw\n")
    code, report = _check(root, "--compose", str(override), "--operators", "nobody", capsys=capsys)
    assert code == 1
    assert report["checked"] == ["ada"]
    assert report["findings"] and report["findings"][0]["mind"] == "ada"
