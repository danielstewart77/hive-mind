"""The order of the install, and what a step reads from the hive."""

from __future__ import annotations

from installer import steps


def test_nothing_is_written_before_the_names_are_checked_against_an_existing_hive():
    order = [s.name for s in steps.STEPS]
    assert all(order.index("collisions") < order.index(name) for name in steps.WRITING_STEPS)


def test_dsh_is_read_from_the_minds_harness_list():
    report = {"harnesses": [{"name": "claude", "available": True, "reason": None},
                            {"name": "dsh", "available": False, "reason": "no hooks"}],
              "default": "dsh"}
    assert steps.dsh_state(report) == "not available: no hooks"
    report["harnesses"][1].update(available=True, reason=None)
    assert steps.dsh_state(report) == "available"
