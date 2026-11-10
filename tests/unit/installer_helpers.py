"""Shared set-up for the installer's tests: an install folder and a rendered
configuration read back through Docker Compose itself."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from installer import app, compose, plan, steps
from installer.host import Host
from installer.paths import Home
from installer.shell import Shell

needs_compose = pytest.mark.skipif(shutil.which("docker") is None,
                                   reason="docker compose renders these configurations")


def a_host(**overrides) -> Host:
    fields = {"uid": os.getuid(), "gid": os.getgid(), "docker_gid": 4242, "tz": "Europe/Oslo",
              "web_proxy": {}, "address": "192.0.2.10"}
    fields.update(overrides)
    return Host(**fields)


def answers(pieces=(), modes=None, **given) -> dict:
    out = {"pieces": list(pieces), "modes": dict(modes or {}),
           "admin_username": "ann", "admin_password": "first-password",
           "mind_name": "wren", "mind_id": "6f1d1c9e-0000-4000-8000-000000000001",
           "provider_kind": "anthropic", "provider_api_key": "sk-upstream",
           "provider_model": "claude-sonnet-5", "provider_model_window": "200000"}
    out.update(given)
    return out


def installer_at(tmp_path, answer_set: dict, host: Host | None = None, step_list=()):
    home = Home.at(tmp_path / "hive")
    home.ensure()
    inst = app.Installer(home, host or a_host(), step_list=list(step_list))
    # As the page hands them over: secrets typed this run are held apart and
    # never saved with the answers.
    inst.typed = {k: v for k, v in answer_set.items() if k in compose.SECRET_ANSWERS}
    inst.answers = {k: v for k, v in answer_set.items() if k not in compose.SECRET_ANSWERS}
    return inst


def render_raw(inst) -> dict:
    """The installer's step only: what it wrote, before compose reads it."""
    return steps.render_config(steps.Ctx(inst, Shell(inst.home.log)))


def render(inst) -> dict:
    """Write the configuration with the installer's own step, then read it
    back as `docker compose config` resolves it."""
    steps.render_config(steps.Ctx(inst, Shell(inst.home.log)))
    proc = subprocess.run([*compose.compose_cmd(inst.home), "config", "--format", "json"],
                          capture_output=True, text=True, timeout=60, check=True,
                          env=compose.compose_env())
    return json.loads(proc.stdout)


def all_pieces() -> list[str]:
    return [p for p in plan.PIECES if not plan.PIECES[p].required]
