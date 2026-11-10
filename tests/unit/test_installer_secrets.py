"""A rerun keeps the passwords and tokens the hive is using now."""

from __future__ import annotations

import json
import stat

from installer import secrets_store, steps
from installer.shell import Shell

from tests.unit.installer_helpers import answers, installer_at, needs_compose

pytestmark = needs_compose


def _rewrite_line(path, name, value):
    """What the console's credentials tab does: one line rewritten in place."""
    lines = [f"{name}={value}" if line.startswith(f"{name}=") else line
             for line in path.read_text().splitlines()]
    path.write_text("\n".join(lines) + "\n")


def test_a_second_run_keeps_every_value_including_one_changed_in_the_settings_since(tmp_path):
    inst = installer_at(tmp_path, answers(pieces=["console", "terminal", "telegram"],
                                          telegram_token="bot-token"))
    ctx = steps.Ctx(inst, Shell(inst.home.log))
    steps.render_config(ctx)
    first = secrets_store.parse_env(inst.home.env_file)
    _rewrite_line(inst.home.env_file, "COMMS_ADMIN_BEARER_TOKEN",
                  "rotated-on-the-console")  # secret-guard: allow — invented

    steps.render_config(ctx)
    second = secrets_store.parse_env(inst.home.env_file)

    assert second["COMMS_ADMIN_BEARER_TOKEN"] == "rotated-on-the-console"
    assert {k: v for k, v in second.items() if k != "COMMS_ADMIN_BEARER_TOKEN"} == \
        {k: v for k, v in first.items() if k != "COMMS_ADMIN_BEARER_TOKEN"}


def test_a_token_typed_on_an_earlier_run_does_not_undo_a_rotation_since(tmp_path):
    page = answers(pieces=["telegram"], telegram_token="typed-first")
    first_run = installer_at(tmp_path, page)
    assert first_run.start(page) == []
    first_run._thread.join(10)
    steps.render_config(steps.Ctx(first_run, Shell(first_run.home.log)))
    _rewrite_line(first_run.home.env_file, "TELEGRAM_BOT_TOKEN", "rotated-since")

    # The next run: a new installer process on the same folder, the token
    # field left blank.
    from installer.app import Installer
    second_run = Installer(first_run.home, first_run.host, step_list=[])
    assert "telegram_token" not in json.loads(first_run.home.answers.read_text())
    assert second_run.start({**second_run.answers, "telegram_token": ""}) == []
    second_run._thread.join(10)
    steps.render_config(steps.Ctx(second_run, Shell(second_run.home.log)))

    assert secrets_store.parse_env(second_run.home.env_file)["TELEGRAM_BOT_TOKEN"] == \
        "rotated-since"


def test_the_files_holding_secrets_are_readable_only_by_the_operator(tmp_path):
    inst = installer_at(tmp_path, answers())
    steps.render_config(steps.Ctx(inst, Shell(inst.home.log)))

    for path in (inst.home.env_file, inst.home.secrets, inst.home.compose_file):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


def test_an_inline_comment_is_not_part_of_a_value(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=plain # set by hand\nB='quoted # kept' # comment\n")
    assert secrets_store.parse_env(path) == {"A": "plain", "B": "quoted # kept"}
