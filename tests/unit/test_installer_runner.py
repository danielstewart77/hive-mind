"""A failed step is recorded, and the next run repeats and carries on."""

from __future__ import annotations

from installer import runner, steps
from installer.runner import Step
from installer.shell import StepFailed

from tests.unit.installer_helpers import answers, installer_at, needs_compose


@needs_compose
def test_a_failed_step_is_recorded_and_the_next_run_repeats_the_earlier_ones_unchanged(tmp_path):
    broken = {"yes": True}

    def flaky(ctx):
        if broken["yes"]:
            raise StepFailed("the registry was unreachable")
        return "ok"

    step_list = [Step("config", "Write the configuration", steps.render_config, 120),
                 Step("flaky", "A step that fails once", flaky, 30)]
    inst = installer_at(tmp_path, answers(), step_list=step_list)

    assert inst.runner.run() is False
    first = {s["name"]: s for s in runner.read_state(inst.home)["steps"]}
    assert first["config"]["status"] == runner.DONE
    assert first["flaky"]["status"] == runner.FAILED
    assert "the registry was unreachable" in first["flaky"]["error"]
    produced = (inst.home.compose_file.read_bytes(), inst.home.env_file.read_bytes())

    broken["yes"] = False
    assert inst.runner.run() is True
    second = {s["name"]: s for s in runner.read_state(inst.home)["steps"]}
    assert [second[n]["status"] for n in ("config", "flaky")] == [runner.DONE, runner.DONE]
    assert (inst.home.compose_file.read_bytes(), inst.home.env_file.read_bytes()) == produced
