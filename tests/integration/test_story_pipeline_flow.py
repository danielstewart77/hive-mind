"""Integration tests for core/story_pipeline.py -- pipeline flow and imports."""

from __future__ import annotations

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


class TestPipelineModuleImportable:
    """All public symbols from the pipeline module are importable."""

    def test_pipeline_module_importable(self) -> None:
        assert PipelineResult is not None
        assert PipelineStepResult is not None
        assert git_pull_master is not None
        assert check_container_health is not None
        assert cleanup_story_directory is not None
        assert push_branch is not None
        assert create_pull_request is not None
        assert notify_completion is not None
        assert run_post_merge_pipeline is not None


class TestPipelineStepSequence:
    """The pipeline runs steps in the correct order."""

    @patch("core.story_pipeline.notify_completion")
    @patch("core.story_pipeline.cleanup_story_directory")
    @patch("core.story_pipeline.check_container_health")
    @patch("core.story_pipeline.git_pull_master")
    def test_pipeline_step_sequence(
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
        assert step_names == ["git_pull", "health_check", "cleanup", "notify"]

    @patch("core.story_pipeline.notify_completion")
    @patch("core.story_pipeline.cleanup_story_directory")
    @patch("core.story_pipeline.check_container_health")
    @patch("core.story_pipeline.git_pull_master")
    def test_pipeline_result_contains_pr_url(
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

        pr_url = "https://github.com/owner/repo/pull/99"
        result = run_post_merge_pipeline(
            card_name="Test Card", slug="test-slug", pr_url=pr_url
        )
        assert result.pr_url == pr_url


class TestCleanupOnlyAffectsStoryDirectory:
    """Cleanup must not affect sibling or parent directories."""

    def test_cleanup_only_affects_story_directory(self, tmp_path) -> None:
        stories_dir = tmp_path / "stories"
        stories_dir.mkdir()
        target = stories_dir / "target-slug"
        target.mkdir()
        (target / "STATE.md").write_text("content")
        sibling = stories_dir / "sibling-slug"
        sibling.mkdir()
        (sibling / "STATE.md").write_text("sibling content")

        with patch("core.story_pipeline.STORIES_DIR", stories_dir):
            result = cleanup_story_directory(str(target))

        assert result.success is True
        assert not target.exists()
        # Sibling and parent must be untouched
        assert sibling.exists()
        assert (sibling / "STATE.md").read_text() == "sibling content"
        assert stories_dir.exists()
