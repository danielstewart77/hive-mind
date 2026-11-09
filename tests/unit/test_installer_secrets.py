"""A rerun keeps the passwords and tokens the hive is using now."""

from __future__ import annotations

from installer import secrets_store, steps
from installer.shell import Shell

from tests.unit.installer_helpers import answers, installer_at, needs_compose


@needs_compose
def test_a_second_run_keeps_every_value_including_one_changed_in_the_settings_since(tmp_path):
    inst = installer_at(tmp_path, answers(pieces=["console", "terminal", "telegram"],
                                          telegram_token="bot-token"))
    ctx = steps.Ctx(inst, Shell(inst.home.log))
    steps.render_config(ctx)
    first = secrets_store.parse_env(inst.home.env_file)
    # What the console's credentials tab does: rewrite one line in place.
    text = inst.home.env_file.read_text()
    inst.home.env_file.write_text(text.replace(
        f"COMMS_ADMIN_BEARER_TOKEN='{first['COMMS_ADMIN_BEARER_TOKEN']}'",
        "COMMS_ADMIN_BEARER_TOKEN=rotated-on-the-console"))  # secret-guard: allow — invented

    steps.render_config(ctx)
    second = secrets_store.parse_env(inst.home.env_file)

    assert second["COMMS_ADMIN_BEARER_TOKEN"] == "rotated-on-the-console"
    assert {k: v for k, v in second.items() if k != "COMMS_ADMIN_BEARER_TOKEN"} == \
        {k: v for k, v in first.items() if k != "COMMS_ADMIN_BEARER_TOKEN"}
