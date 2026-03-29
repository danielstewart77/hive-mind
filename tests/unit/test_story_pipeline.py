"""Unit tests for core/story_pipeline.py -- post-merge pipeline operations."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.story_pipeline import (
    PipelineResult,
    PipelineStepResult,
    check_container_health,
    cleanup_story_directory,
    create_pull_request,
    git_pull_master,
    notify_completion,
    push_branch,
    run_post_merge_pipeline,
)


# ---------------------------------------------------------------------------
# Step 1: Dataclass field tests
# ---------------------------------------------------------------------------

class TestPipelineStepResultFields:
    """PipelineStepResult has the required fields."""

    def test_pipeline_step_result_fields(self) -> None:
        result = PipelineStepResult(
            success=True,
            step_name="test_step",
            detail="did something",
            error=None,
        )
        assert result.success is True
        assert result.step_name == "test_step"
        assert result.detail == "did something"
        assert result.error is None

    def test_pipeline_step_result_with_error(self) -> None:
        result = PipelineStepResult(
            success=False,
            step_name="fail_step",
            detail="tried something",
            error="it broke",
        )
        assert result.success is False
        assert result.error == "it broke"


class TestPipelineResultFields:
    """PipelineResult has the required fields."""

    def test_pipeline_result_fields(self) -> None:
        step = PipelineStepResult(
            success=True, step_name="s1", detail="ok", error=None
        )
        result = PipelineResult(
            success=True,
            steps=[step],
            pr_url="https://github.com/owner/repo/pull/1",
            card_name="Test Card",
        )
        assert result.success is True
        assert len(result.steps) == 1
        assert result.pr_url == "https://github.com/owner/repo/pull/1"
        assert result.card_name == "Test Card"

    def test_pipeline_result_no_pr_url(self) -> None:
        result = PipelineResult(
            success=True, steps=[], pr_url=None, card_name="Card"
        )
        assert result.pr_url is None


# ---------------------------------------------------------------------------
# Step 1: git_pull_master tests
# ---------------------------------------------------------------------------

class TestGitPullMaster:
    """Tests for git_pull_master() subprocess wrapper."""

    @patch("core.story_pipeline.subprocess.run")
    def test_git_pull_master_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Already up to date.", stderr=""
        )
        result = git_pull_master()
        assert result.success is True
        assert result.step_name == "git_pull"
        assert result.error is None

    @patch("core.story_pipeline.subprocess.run")
    def test_git_pull_master_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="error: could not merge"
        )
        result = git_pull_master()
        assert result.success is False
        assert result.step_name == "git_pull"
        assert result.error is not None
        assert len(result.error) > 0

    @patch("core.story_pipeline.subprocess.run")
    def test_git_pull_master_pull_step_failure(self, mock_run: MagicMock) -> None:
        """Checkout succeeds but git pull fails -- exercises the pull-specific error path."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # checkout succeeds
            MagicMock(returncode=1, stdout="", stderr="merge conflict"),  # pull fails
        ]
        result = git_pull_master()
        assert result.success is False
        assert result.step_name == "git_pull"
        assert result.error is not None
        assert "merge conflict" in result.error
        assert "git pull failed" in result.detail

    @patch("core.story_pipeline.subprocess.run")
    def test_git_pull_master_exception(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd="git", timeout=60
        )
        result = git_pull_master()
        assert result.success is False
        assert result.step_name == "git_pull"
        assert result.error is not None


# ---------------------------------------------------------------------------
# Step 2: check_container_health tests
# ---------------------------------------------------------------------------

class TestCheckContainerHealth:
    """Tests for check_container_health() subprocess wrapper."""

    @patch("core.story_pipeline.subprocess.run")
    def test_health_check_all_running(self, mock_run: MagicMock) -> None:
        containers = [
            {"Name": "hive_mind", "State": "running", "Status": "Up 5 minutes"},
            {"Name": "neo4j", "State": "running", "Status": "Up 5 minutes"},
        ]
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(containers),
            stderr="",
        )
        result = check_container_health()
        assert result.success is True
        assert result.step_name == "health_check"
        assert result.error is None

    @patch("core.story_pipeline.subprocess.run")
    def test_health_check_container_down(self, mock_run: MagicMock) -> None:
        containers = [
            {"Name": "hive_mind", "State": "running", "Status": "Up 5 minutes"},
            {"Name": "neo4j", "State": "exited", "Status": "Exited (1) 2 minutes ago"},
        ]
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(containers),
            stderr="",
        )
        result = check_container_health()
        assert result.success is False
        assert result.step_name == "health_check"
        assert "neo4j" in result.detail

    @patch("core.story_pipeline.subprocess.run")
    def test_health_check_command_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="docker compose not found",
        )
        result = check_container_health()
        assert result.success is False
        assert result.step_name == "health_check"
        assert result.error is not None


# ---------------------------------------------------------------------------
# Step 3: cleanup_story_directory tests
# ---------------------------------------------------------------------------

class TestCleanupStoryDirectory:
    """Tests for cleanup_story_directory() with path safety."""

    def test_cleanup_removes_directory(self, tmp_path: Path) -> None:
        stories_dir = tmp_path / "stories"
        stories_dir.mkdir()
        story_dir = stories_dir / "test-slug"
        story_dir.mkdir()
        (story_dir / "STATE.md").write_text("test content")

        with patch("core.story_pipeline.STORIES_DIR", stories_dir):
            result = cleanup_story_directory(str(story_dir))

        assert result.success is True
        assert result.step_name == "cleanup"
        assert not story_dir.exists()

    def test_cleanup_nonexistent_directory(self, tmp_path: Path) -> None:
        stories_dir = tmp_path / "stories"
        stories_dir.mkdir()
        nonexistent = stories_dir / "nonexistent-slug"

        with patch("core.story_pipeline.STORIES_DIR", stories_dir):
            result = cleanup_story_directory(str(nonexistent))

        assert result.success is True
        assert result.step_name == "cleanup"

    def test_cleanup_rejects_path_outside_stories(self, tmp_path: Path) -> None:
        stories_dir = tmp_path / "stories"
        stories_dir.mkdir()
        outside_dir = tmp_path / "core"
        outside_dir.mkdir()

        with patch("core.story_pipeline.STORIES_DIR", stories_dir):
            result = cleanup_story_directory(str(outside_dir))

        assert result.success is False
        assert result.error is not None

    def test_cleanup_rejects_traversal_attack(self, tmp_path: Path) -> None:
        stories_dir = tmp_path / "stories"
        stories_dir.mkdir()
        attack_path = str(stories_dir / ".." / ".." / "etc")

        with patch("core.story_pipeline.STORIES_DIR", stories_dir):
            result = cleanup_story_directory(attack_path)

        assert result.success is False
        assert result.error is not None

    def test_cleanup_rejects_empty_path(self) -> None:
        result = cleanup_story_directory("")
        assert result.success is False
        assert result.error is not None

    def test_cleanup_rejects_stories_root(self, tmp_path: Path) -> None:
        stories_dir = tmp_path / "stories"
        stories_dir.mkdir()

        with patch("core.story_pipeline.STORIES_DIR", stories_dir):
            result = cleanup_story_directory(str(stories_dir))

        assert result.success is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# Step 4: push_branch and create_pull_request tests
# ---------------------------------------------------------------------------

class TestPushBranch:
    """Tests for push_branch() subprocess wrapper."""

    @patch("core.story_pipeline.subprocess.run")
    def test_push_branch_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Everything up-to-date", stderr=""
        )
        result = push_branch("story/test-slug")
        assert result.success is True
        assert result.step_name == "push_branch"
        assert result.error is None

    @patch("core.story_pipeline.subprocess.run")
    def test_push_branch_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="fatal: remote error"
        )
        result = push_branch("story/test-slug")
        assert result.success is False
        assert result.step_name == "push_branch"
        assert result.error is not None


class TestCreatePullRequest:
    """Tests for create_pull_request() subprocess wrapper."""

    @patch("core.story_pipeline.subprocess.run")
    def test_create_pr_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/42\n",
            stderr="",
        )
        result = create_pull_request(
            branch="story/test",
            base="master",
            title="Test PR",
            body="Test body",
        )
        assert result.success is True
        assert result.step_name == "create_pr"
        assert "https://github.com/owner/repo/pull/42" in result.detail

    @patch("core.story_pipeline.subprocess.run")
    def test_create_pr_already_exists(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="a pull request for branch 'story/test' into branch 'master' already exists",
        )
        result = create_pull_request(
            branch="story/test",
            base="master",
            title="Test PR",
            body="Test body",
        )
        assert result.success is True
        assert result.step_name == "create_pr"
        assert "already exists" in result.detail.lower()

    @patch("core.story_pipeline.subprocess.run")
    def test_create_pr_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="GraphQL: Could not resolve to a Repository",
        )
        result = create_pull_request(
            branch="story/test",
            base="master",
            title="Test PR",
            body="Test body",
        )
        assert result.success is False
        assert result.step_name == "create_pr"
        assert result.error is not None


# ---------------------------------------------------------------------------
# Step 5: notify_completion tests
# ---------------------------------------------------------------------------

class TestNotifyCompletion:
    """Tests for notify_completion() subprocess wrapper."""

    @patch("core.story_pipeline.subprocess.run")
    def test_notify_completion_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Notification sent", stderr=""
        )
        result = notify_completion("Story closed: test")
        assert result.success is True
        assert result.step_name == "notify"
        assert result.error is None

    @patch("core.story_pipeline.subprocess.run")
    def test_notify_completion_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Connection refused"
        )
        result = notify_completion("Story closed: test")
        assert result.success is False
        assert result.step_name == "notify"
        assert result.error is not None

    @patch("core.story_pipeline.subprocess.run")
    def test_notify_completion_message_format(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        notify_completion("Story closed: Test Card | PR: https://example.com/pr/1")
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        # The message should appear in the command arguments
        assert any("Story closed: Test Card" in str(arg) for arg in cmd)


# ---------------------------------------------------------------------------
# Step 6: run_post_merge_pipeline tests
# ---------------------------------------------------------------------------

class TestRunPostMergePipeline:
    """Tests for run_post_merge_pipeline() orchestration."""

    @patch("core.story_pipeline.notify_completion")
    @patch("core.story_pipeline.cleanup_story_directory")
    @patch("core.story_pipeline.check_container_health")
    @patch("core.story_pipeline.git_pull_master")
    def test_full_pipeline_success(
        self,
        mock_git_pull: MagicMock,
        mock_health: MagicMock,
        mock_cleanup: MagicMock,
        mock_notify: MagicMock,
    ) -> None:
        mock_git_pull.return_value = PipelineStepResult(
            success=True, step_name="git_pull", detail="ok", error=None
        )
        mock_health.return_value = PipelineStepResult(
            success=True, step_name="health_check", detail="all healthy", error=None
        )
        mock_cleanup.return_value = PipelineStepResult(
            success=True, step_name="cleanup", detail="removed", error=None
        )
        mock_notify.return_value = PipelineStepResult(
            success=True, step_name="notify", detail="sent", error=None
        )

        result = run_post_merge_pipeline(
            card_name="Test Card",
            slug="test-slug",
            pr_url="https://github.com/owner/repo/pull/1",
        )
        assert result.success is True
        assert result.card_name == "Test Card"
        assert result.pr_url == "https://github.com/owner/repo/pull/1"
        assert len(result.steps) == 4

    @patch("core.story_pipeline.git_pull_master")
    def test_full_pipeline_stops_on_git_pull_failure(
        self, mock_git_pull: MagicMock
    ) -> None:
        mock_git_pull.return_value = PipelineStepResult(
            success=False, step_name="git_pull", detail="", error="merge conflict"
        )
        result = run_post_merge_pipeline(
            card_name="Test Card", slug="test-slug"
        )
        assert result.success is False
        assert len(result.steps) == 1
        assert result.steps[0].step_name == "git_pull"

    @patch("core.story_pipeline.check_container_health")
    @patch("core.story_pipeline.git_pull_master")
    def test_full_pipeline_stops_on_health_check_failure(
        self, mock_git_pull: MagicMock, mock_health: MagicMock
    ) -> None:
        mock_git_pull.return_value = PipelineStepResult(
            success=True, step_name="git_pull", detail="ok", error=None
        )
        mock_health.return_value = PipelineStepResult(
            success=False, step_name="health_check", detail="neo4j down", error="unhealthy"
        )
        result = run_post_merge_pipeline(
            card_name="Test Card", slug="test-slug"
        )
        assert result.success is False
        assert len(result.steps) == 2
        assert result.steps[1].step_name == "health_check"

    @patch("core.story_pipeline.notify_completion")
    @patch("core.story_pipeline.cleanup_story_directory")
    @patch("core.story_pipeline.check_container_health")
    @patch("core.story_pipeline.git_pull_master")
    def test_full_pipeline_includes_cleanup(
        self,
        mock_git_pull: MagicMock,
        mock_health: MagicMock,
        mock_cleanup: MagicMock,
        mock_notify: MagicMock,
    ) -> None:
        mock_git_pull.return_value = PipelineStepResult(
            success=True, step_name="git_pull", detail="ok", error=None
        )
        mock_health.return_value = PipelineStepResult(
            success=True, step_name="health_check", detail="ok", error=None
        )
        mock_cleanup.return_value = PipelineStepResult(
            success=True, step_name="cleanup", detail="removed", error=None
        )
        mock_notify.return_value = PipelineStepResult(
            success=True, step_name="notify", detail="sent", error=None
        )

        result = run_post_merge_pipeline(
            card_name="Test Card", slug="test-slug"
        )
        step_names = [s.step_name for s in result.steps]
        assert "cleanup" in step_names
        mock_cleanup.assert_called_once()

    @patch("core.story_pipeline.notify_completion")
    @patch("core.story_pipeline.cleanup_story_directory")
    @patch("core.story_pipeline.check_container_health")
    @patch("core.story_pipeline.git_pull_master")
    def test_full_pipeline_skips_cleanup_if_no_story_dir(
        self,
        mock_git_pull: MagicMock,
        mock_health: MagicMock,
        mock_cleanup: MagicMock,
        mock_notify: MagicMock,
    ) -> None:
        mock_git_pull.return_value = PipelineStepResult(
            success=True, step_name="git_pull", detail="ok", error=None
        )
        mock_health.return_value = PipelineStepResult(
            success=True, step_name="health_check", detail="ok", error=None
        )
        # cleanup returns success even if dir doesn't exist (idempotent)
        mock_cleanup.return_value = PipelineStepResult(
            success=True, step_name="cleanup", detail="directory does not exist", error=None
        )
        mock_notify.return_value = PipelineStepResult(
            success=True, step_name="notify", detail="sent", error=None
        )

        result = run_post_merge_pipeline(
            card_name="Test Card", slug="nonexistent-slug"
        )
        assert result.success is True
