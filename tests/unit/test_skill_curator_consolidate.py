"""Step 5 — 3.2 consolidation pass, wired but default-off."""

import importlib.util
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_curator/skill_curator.py")
TELEMETRY_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_telemetry/skill_telemetry.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _curator():
    return _load(SCRIPT_PATH, "skill_curator_under_test_c2")


def _telemetry():
    return _load(TELEMETRY_PATH, "skill_telemetry_under_test_c2")


def _make_skill(config_dir: Path, name: str, *, created_by="agent",
                symlink_target=None):
    tel = _telemetry()
    skills = config_dir / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    if symlink_target is not None:
        symlink_target.mkdir(parents=True, exist_ok=True)
        (symlink_target / "SKILL.md").write_text("# real\n", encoding="utf-8")
        (skills / name).symlink_to(symlink_target, target_is_directory=True)
    else:
        d = skills / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    tel.seed_record_if_missing(config_dir, name, created_by=created_by)


def test_disabled_returns_not_ran(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _make_skill(cfg, "a")
    before = tel.load_usage(cfg)
    res = cur.maybe_consolidate(cfg, "claude_cli", enabled=False)
    assert res["ran"] is False
    assert "reason" in res
    # No mutation.
    assert tel.load_usage(cfg) == before
    assert (cfg / "skills" / "a").is_dir()


def test_candidate_list_only_agent_created(tmp_path):
    cur = _curator()
    cfg = tmp_path / "cfg"
    _make_skill(cfg, "agent_one", created_by="agent")
    _make_skill(cfg, "humanish", created_by="human")
    _make_skill(cfg, "software", created_by="agent")  # protected router
    _make_skill(cfg, "plug", created_by="agent",
                symlink_target=tmp_path / "src" / "plug")

    candidates = cur.build_consolidation_candidates(cfg)
    names = {c["name"] for c in candidates}
    assert "agent_one" in names
    assert "humanish" not in names
    assert "software" not in names
    assert "plug" not in names


def test_prompt_names_protected_routers(tmp_path):
    cur = _curator()
    for name in ("software", "operations", "planning", "information", "communication"):
        assert name in cur.CURATOR_REVIEW_PROMPT
    assert "~/.hermes" not in cur.CURATOR_REVIEW_PROMPT


def test_enabled_assembles_dispatch_without_mutation(tmp_path):
    cur, tel = _curator(), _telemetry()
    cfg = tmp_path / "cfg"
    _make_skill(cfg, "agent_one", created_by="agent")
    before = tel.load_usage(cfg)

    res = cur.maybe_consolidate(cfg, "claude_cli", enabled=True)
    assert res["ran"] is True
    assert "prompt" in res
    assert cur.CURATOR_REVIEW_PROMPT in res["prompt"]
    assert any(c["name"] == "agent_one" for c in res["candidates"])
    # Proves Phase 2 reuse (no duplicated archiver): the absorbed-archive
    # contract is skill_manage delete --absorbed-into.
    assert "skill_manage delete --absorbed-into" in res["absorbed_archive_command"]
    # No mutation in-test (no model actually invoked).
    assert tel.load_usage(cfg) == before
    assert (cfg / "skills" / "agent_one").is_dir()
