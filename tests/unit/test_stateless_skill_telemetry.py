"""Unit tests for the stateless skill telemetry sidecar.

Drives the CLI via subprocess (round-trip to disk) and imports the module
directly for the pure helpers. Covers the per-mind sidecar I/O, bump
counters, lock-safety under concurrency, state validation, forget, and the
first-run backfill seeding.
"""

import importlib.util
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_telemetry/skill_telemetry.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("skill_telemetry_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(*args, timeout=15):
    return subprocess.run(
        [sys.executable, SCRIPT_PATH, *args],
        capture_output=True, text=True, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Step 1 — sidecar reader/writer + CLI
# ---------------------------------------------------------------------------

def test_bump_use_creates_and_reads_record(tmp_path):
    cfg = tmp_path
    r = _run("--action", "bump-use", "--skill", "foo", "--config-dir", str(cfg))
    assert r.returncode == 0, r.stderr

    sidecar = cfg / "skills" / ".usage.json"
    assert sidecar.exists()

    lst = _run("--action", "list", "--config-dir", str(cfg))
    data = json.loads(lst.stdout)
    assert data["foo"]["use_count"] == 1
    assert data["foo"]["last_used_at"] is not None


def test_latest_activity_excludes_created_at(tmp_path):
    mod = _load_module()
    rec = mod._empty_record()
    # Only created_at is set — no activity yet.
    assert mod.latest_activity_at(rec) is None
    rec["last_viewed_at"] = "2026-06-27T12:00:00+00:00"
    assert mod.latest_activity_at(rec) == "2026-06-27T12:00:00+00:00"


def test_concurrent_bumps_are_lock_safe(tmp_path):
    cfg = tmp_path
    n = 12

    def _bump(_):
        return _run("--action", "bump-use", "--skill", "race", "--config-dir", str(cfg))

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(_bump, range(n)))
    assert all(res.returncode == 0 for res in results)

    lst = _run("--action", "list", "--config-dir", str(cfg))
    data = json.loads(lst.stdout)
    assert data["race"]["use_count"] == n


def test_set_state_rejects_invalid_state(tmp_path):
    cfg = tmp_path
    _run("--action", "bump-use", "--skill", "foo", "--config-dir", str(cfg))
    r = _run("--action", "set-state", "--skill", "foo", "--state", "bogus", "--config-dir", str(cfg))
    assert r.returncode != 0
    assert "error" in json.loads(r.stdout)

    data = json.loads(_run("--action", "list", "--config-dir", str(cfg)).stdout)
    assert data["foo"]["state"] == "active"  # unchanged, never written bogus


def test_forget_removes_entry(tmp_path):
    cfg = tmp_path
    _run("--action", "bump-use", "--skill", "foo", "--config-dir", str(cfg))
    _run("--action", "forget", "--skill", "foo", "--config-dir", str(cfg))
    data = json.loads(_run("--action", "list", "--config-dir", str(cfg)).stdout)
    assert "foo" not in data


# ---------------------------------------------------------------------------
# Step 2 — first-run backfill seeding
# ---------------------------------------------------------------------------

def _make_skill(cfg: Path, name: str):
    d = cfg / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return d


def test_seed_creates_record_per_real_skill(tmp_path):
    cfg = tmp_path
    _make_skill(cfg, "alpha")
    _make_skill(cfg, "beta")

    r = _run("--action", "seed", "--config-dir", str(cfg))
    summary = json.loads(r.stdout)
    assert set(summary["seeded"]) == {"alpha", "beta"}

    data = json.loads(_run("--action", "list", "--config-dir", str(cfg)).stdout)
    for name in ("alpha", "beta"):
        assert data[name]["created_by"] == "human"
        assert data[name]["state"] == "active"
        assert data[name]["created_at"] is not None


def test_seed_skips_symlinked_skills(tmp_path):
    cfg = tmp_path
    _make_skill(cfg, "local")
    # A plugin skill: the skill dir itself is a symlink to an external dir.
    external = tmp_path / "external_plugin"
    external.mkdir()
    (external / "SKILL.md").write_text("---\nname: plugin\n---\n", encoding="utf-8")
    (cfg / "skills").mkdir(parents=True, exist_ok=True)
    os.symlink(str(external), str(cfg / "skills" / "plugin"))

    _run("--action", "seed", "--config-dir", str(cfg))
    data = json.loads(_run("--action", "list", "--config-dir", str(cfg)).stdout)
    assert "local" in data
    assert "plugin" not in data  # D2: symlink => plugin, never seeded


def test_seed_is_idempotent_and_preserves_counters(tmp_path):
    cfg = tmp_path
    _make_skill(cfg, "alpha")
    _run("--action", "seed", "--config-dir", str(cfg))
    for _ in range(3):
        _run("--action", "bump-use", "--skill", "alpha", "--config-dir", str(cfg))

    before = json.loads(_run("--action", "list", "--config-dir", str(cfg)).stdout)["alpha"]
    assert before["use_count"] == 3

    _run("--action", "seed", "--config-dir", str(cfg))  # re-run
    after = json.loads(_run("--action", "list", "--config-dir", str(cfg)).stdout)["alpha"]
    assert after["use_count"] == 3
    assert after["created_at"] == before["created_at"]
