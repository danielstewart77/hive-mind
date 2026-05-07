"""Tests for Nagatha's Codex-local runtime assets."""

from pathlib import Path
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[2]
NAGATHA_CODEX_HOME = REPO_ROOT / "minds" / "nagatha" / ".codex"


def test_nagatha_codex_config_declares_agent_limits() -> None:
    config = tomllib.loads((NAGATHA_CODEX_HOME / "config.toml").read_text())

    assert config["agents"]["max_threads"] == 6
    assert config["agents"]["max_depth"] == 1


def test_nagatha_has_required_pipeline_agents() -> None:
    agents_dir = NAGATHA_CODEX_HOME / "agents"
    expected = {
        "step-get-story.toml": "step-get-story",
        "step-planning.toml": "step-planning",
        "step-coding.toml": "step-coding",
        "step-review.toml": "step-review",
        "step-push-pr.toml": "step-push-pr",
    }

    for filename, agent_name in expected.items():
        data = tomllib.loads((agents_dir / filename).read_text())
        assert data["name"] == agent_name
        assert data["description"]
        assert data["developer_instructions"]


def test_nagatha_has_local_3am_skill() -> None:
    skill_path = NAGATHA_CODEX_HOME / "skills" / "3am" / "SKILL.md"
    content = skill_path.read_text()

    assert "name: 3am" in content
    assert "Nagatha's Codex subagents" in content


def test_nagatha_orchestrator_references_codex_agents() -> None:
    orchestrator = (NAGATHA_CODEX_HOME / "skills" / "orchestrator" / "SKILL.md").read_text()

    assert "using Codex subagents" in orchestrator
    for agent_name in (
        "step-get-story",
        "step-planning",
        "step-coding",
        "step-review",
        "step-push-pr",
    ):
        assert agent_name in orchestrator


def test_nagatha_mind_fragment_sets_codex_home() -> None:
    """The Nagatha mind container is the one that needs CODEX_HOME, not the bot.

    Post-Phase-1 consolidation moved per-mind config into
    minds/<name>/container/compose.yaml.  The repo bind-mounts the project
    into the container, so CODEX_HOME points at the in-project codex dir
    directly rather than a separate volume mount.
    """
    fragment = (REPO_ROOT / "minds" / "nagatha" / "container" / "compose.yaml").read_text()

    assert "nagatha:" in fragment
    assert "CODEX_HOME=/usr/src/app/minds/nagatha/.codex" in fragment
