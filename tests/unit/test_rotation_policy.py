"""The durable rotation preference and its independent capacity calculation."""

from __future__ import annotations

import yaml

from minds import rotation_policy


def test_a_missing_preference_uses_the_full_published_capacity(tmp_path):
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("name: legacy\n")

    policy = rotation_policy.read_policy(runtime)
    threshold = rotation_policy.effective_threshold(100_000, policy["percentage"])

    assert policy == {"status": "ok", "percentage": 100, "error": None}
    assert threshold == 100_000


def test_percentage_calculation_preserves_floor_rounding_and_known_zero():
    assert rotation_policy.effective_threshold(131_072, 37) == 48_496
    assert rotation_policy.effective_threshold(1, 1) == 0
    assert rotation_policy.effective_threshold(131_072, 100) == 131_072


def test_unknown_or_invalid_capacity_does_not_create_a_threshold():
    for value in (None, 0, -1, True, "100000"):
        assert rotation_policy.effective_threshold(value, 40) is None


def test_a_missing_runtime_path_is_an_invalid_policy_not_a_default(tmp_path):
    missing = tmp_path / "missing" / "runtime.yaml"

    policy = rotation_policy.read_policy(missing)

    assert policy == {
        "status": "invalid", "percentage": None,
        "error": "invalid_runtime_policy", "reason": "config_unreadable",
    }
    assert not missing.exists()


def test_present_malformed_percentages_are_not_treated_as_missing(tmp_path):
    for index, value in enumerate(("bad", None, True, 40.5, "40", 0, 101)):
        runtime = tmp_path / f"runtime-{index}.yaml"
        runtime.write_text(yaml.safe_dump({"rotation_threshold_percent": value}))

        assert rotation_policy.read_policy(runtime) == {
            "status": "invalid", "percentage": None,
            "error": "invalid_runtime_policy",
        }


def test_malformed_yaml_and_non_mapping_runtime_are_invalid(tmp_path):
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("rotation_threshold_percent: [\n")
    scalar = tmp_path / "scalar.yaml"
    scalar.write_text("runtime\n")

    assert rotation_policy.read_policy(malformed)["status"] == "invalid"
    assert rotation_policy.read_policy(scalar)["status"] == "invalid"
