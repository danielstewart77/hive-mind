"""The mind the installer creates."""

from __future__ import annotations

from types import SimpleNamespace

from installer import steps
from minds import rotation_policy

from tests.unit.installer_helpers import answers, installer_at


class _Shell:
    """The one command the step runs — writing the dsh profile — succeeding."""

    def run(self, *args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_an_installed_mind_starts_with_a_rotation_threshold_of_thirty_percent(tmp_path):
    """A runtime.yaml with no line rotates at the whole window, so the mind
    the installer creates is written one, in the form its reader accepts."""
    inst = installer_at(tmp_path, answers(model="claude-sonnet-5"))

    steps.create_mind(steps.Ctx(inst, _Shell()))

    policy = rotation_policy.read_policy(inst.home.mind("wren") / "runtime.yaml")
    assert (policy["status"], policy["percentage"]) == ("ok", 30)
