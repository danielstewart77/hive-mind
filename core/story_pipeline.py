"""Post-merge story pipeline operations.

Provides pure functions that wrap git, filesystem, and subprocess operations
for the post-completion pipeline: git pull, container health check, GitHub
push/PR creation, notification, and story directory cleanup.

Each function returns a PipelineStepResult dataclass for testability.
The orchestration function run_post_merge_pipeline() chains them together
and returns a PipelineResult.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

from config import PROJECT_DIR

STORIES_DIR = PROJECT_DIR / "stories"


@dataclass
class PipelineStepResult:
    """Result from a single pipeline step."""

    success: bool
    step_name: str  # e.g. "git_pull", "health_check", "cleanup"
    detail: str  # human-readable description of what happened
    error: str | None  # error message if success=False


@dataclass
class PipelineResult:
    """Aggregated result from the full post-merge pipeline."""

    success: bool
    steps: list[PipelineStepResult]
    pr_url: str | None  # PR URL if available
    card_name: str  # story card name


# ---------------------------------------------------------------------------
# Step 1: Git pull
# ---------------------------------------------------------------------------


def git_pull_master() -> PipelineStepResult:
    """Checkout master and pull latest from origin.

    Runs ``git checkout master`` followed by ``git pull origin master``.

    Returns:
        PipelineStepResult with success=True if both commands succeed.
    """
    try:
        checkout = subprocess.run(
            ["git", "checkout", "master"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if checkout.returncode != 0:
            return PipelineStepResult(
                success=False,
                step_name="git_pull",
                detail=f"git checkout master failed: {checkout.stderr.strip()}",
                error=checkout.stderr.strip() or f"exit code {checkout.returncode}",
            )

        pull = subprocess.run(
            ["git", "pull", "origin", "master"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if pull.returncode != 0:
            return PipelineStepResult(
                success=False,
                step_name="git_pull",
                detail=f"git pull failed: {pull.stderr.strip()}",
                error=pull.stderr.strip() or f"exit code {pull.returncode}",
            )

        return PipelineStepResult(
            success=True,
            step_name="git_pull",
            detail=pull.stdout.strip() or "Already up to date.",
            error=None,
        )

    except (subprocess.TimeoutExpired, OSError) as exc:
        return PipelineStepResult(
            success=False,
            step_name="git_pull",
            detail="git pull operation failed with exception",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Step 2: Container health check
# ---------------------------------------------------------------------------


def check_container_health(project: str = "hive_mind") -> PipelineStepResult:
    """Check that all containers in the project are running.

    Runs ``docker compose -p <project> ps --format json`` and verifies
    each container's state is "running".

    Returns:
        PipelineStepResult with success=True only if all containers are running.
    """
    try:
        proc = subprocess.run(
            ["docker", "compose", "-p", project, "ps", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return PipelineStepResult(
            success=False,
            step_name="health_check",
            detail="docker compose command failed with exception",
            error=str(exc),
        )

    if proc.returncode != 0:
        return PipelineStepResult(
            success=False,
            step_name="health_check",
            detail=f"docker compose ps failed: {proc.stderr.strip()}",
            error=proc.stderr.strip() or f"exit code {proc.returncode}",
        )

    try:
        # docker compose ps --format json may output a JSON array or
        # newline-delimited JSON objects depending on version
        stdout = proc.stdout.strip()
        if not stdout:
            return PipelineStepResult(
                success=False,
                step_name="health_check",
                detail="No containers found",
                error="docker compose ps returned empty output",
            )

        # Try parsing as a JSON array first
        try:
            containers = json.loads(stdout)
            if not isinstance(containers, list):
                containers = [containers]
        except json.JSONDecodeError:
            # Fall back to newline-delimited JSON
            containers = [json.loads(line) for line in stdout.splitlines() if line.strip()]

    except (json.JSONDecodeError, TypeError) as exc:
        return PipelineStepResult(
            success=False,
            step_name="health_check",
            detail="Failed to parse docker compose output",
            error=str(exc),
        )

    unhealthy = []
    for c in containers:
        name = c.get("Name", c.get("name", "unknown"))
        state = c.get("State", c.get("state", "unknown"))
        if state.lower() != "running":
            unhealthy.append(f"{name} ({state})")

    if unhealthy:
        return PipelineStepResult(
            success=False,
            step_name="health_check",
            detail=f"Unhealthy containers: {', '.join(unhealthy)}",
            error=f"{len(unhealthy)} container(s) not running",
        )

    return PipelineStepResult(
        success=True,
        step_name="health_check",
        detail=f"All {len(containers)} containers running",
        error=None,
    )


# ---------------------------------------------------------------------------
# Step 3: Story directory cleanup
# ---------------------------------------------------------------------------


def cleanup_story_directory(story_path: str) -> PipelineStepResult:
    """Remove a story's working documents directory.

    Validates path safety before deletion: the path must resolve to a
    subdirectory of STORIES_DIR (not the root itself), with no traversal.

    Args:
        story_path: Absolute path to the story directory to remove.

    Returns:
        PipelineStepResult with success=True if the directory was removed
        or did not exist (idempotent).
    """
    if not story_path:
        return PipelineStepResult(
            success=False,
            step_name="cleanup",
            detail="Empty path provided",
            error="story_path must not be empty",
        )

    if "\x00" in story_path:
        return PipelineStepResult(
            success=False,
            step_name="cleanup",
            detail="Path contains null bytes",
            error="story_path must not contain null bytes",
        )

    resolved = os.path.realpath(story_path)
    stories_prefix = str(STORIES_DIR) + os.sep

    # Must be a subdirectory of STORIES_DIR, not STORIES_DIR itself
    if not resolved.startswith(stories_prefix):
        return PipelineStepResult(
            success=False,
            step_name="cleanup",
            detail=f"Path {resolved} is outside stories directory",
            error=f"Path must be a subdirectory of {STORIES_DIR}",
        )

    if not os.path.exists(resolved):
        return PipelineStepResult(
            success=True,
            step_name="cleanup",
            detail=f"Directory does not exist: {resolved}",
            error=None,
        )

    try:
        shutil.rmtree(resolved)
        return PipelineStepResult(
            success=True,
            step_name="cleanup",
            detail=f"Removed story directory: {resolved}",
            error=None,
        )
    except OSError as exc:
        return PipelineStepResult(
            success=False,
            step_name="cleanup",
            detail=f"Failed to remove {resolved}",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Step 4: GitHub push and PR creation
# ---------------------------------------------------------------------------


def push_branch(branch: str, skip_hitl: bool = False) -> PipelineStepResult:
    """Push a branch to origin.

    Args:
        branch: The branch name to push.
        skip_hitl: If True, set SKIP_HITL_PUSH=true in the subprocess env.

    Returns:
        PipelineStepResult with success=True if the push succeeds.
    """
    env = None
    if skip_hitl:
        env = {**os.environ, "SKIP_HITL_PUSH": "true"}

    try:
        proc = subprocess.run(
            ["git", "push", "origin", branch],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return PipelineStepResult(
            success=False,
            step_name="push_branch",
            detail="git push failed with exception",
            error=str(exc),
        )

    if proc.returncode != 0:
        return PipelineStepResult(
            success=False,
            step_name="push_branch",
            detail=f"git push origin {branch} failed",
            error=proc.stderr.strip() or f"exit code {proc.returncode}",
        )

    return PipelineStepResult(
        success=True,
        step_name="push_branch",
        detail=proc.stdout.strip() or proc.stderr.strip() or "Branch pushed",
        error=None,
    )


def create_pull_request(
    branch: str, base: str, title: str, body: str
) -> PipelineStepResult:
    """Create a pull request using the GitHub CLI.

    Args:
        branch: Head branch for the PR.
        base: Base branch to merge into.
        title: PR title.
        body: PR body/description.

    Returns:
        PipelineStepResult with success=True if the PR was created or
        already exists.
    """
    try:
        proc = subprocess.run(
            [
                "gh", "pr", "create",
                "--head", branch,
                "--base", base,
                "--title", title,
                "--body", body,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return PipelineStepResult(
            success=False,
            step_name="create_pr",
            detail="gh pr create failed with exception",
            error=str(exc),
        )

    if proc.returncode == 0:
        pr_url = proc.stdout.strip()
        return PipelineStepResult(
            success=True,
            step_name="create_pr",
            detail=f"PR created: {pr_url}",
            error=None,
        )

    # gh returns non-zero when a PR already exists
    if "already exists" in proc.stderr.lower():
        return PipelineStepResult(
            success=True,
            step_name="create_pr",
            detail=f"PR already exists for branch {branch}",
            error=None,
        )

    return PipelineStepResult(
        success=False,
        step_name="create_pr",
        detail=f"gh pr create failed for {branch}",
        error=proc.stderr.strip() or f"exit code {proc.returncode}",
    )


# ---------------------------------------------------------------------------
# Step 5: Notification
# ---------------------------------------------------------------------------


def notify_completion(message: str) -> PipelineStepResult:
    """Send a completion notification via the stateless notify tool.

    Args:
        message: The notification message to send.

    Returns:
        PipelineStepResult with success=True if the notification was sent.
    """
    notify_script = str(PROJECT_DIR / "tools" / "stateless" / "notify" / "notify.py")

    try:
        proc = subprocess.run(
            [sys.executable, notify_script, "send", "--message", message],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return PipelineStepResult(
            success=False,
            step_name="notify",
            detail="Notification failed with exception",
            error=str(exc),
        )

    if proc.returncode != 0:
        return PipelineStepResult(
            success=False,
            step_name="notify",
            detail="Notification command failed",
            error=proc.stderr.strip() or f"exit code {proc.returncode}",
        )

    return PipelineStepResult(
        success=True,
        step_name="notify",
        detail="Notification sent",
        error=None,
    )


# ---------------------------------------------------------------------------
# Step 6: Full pipeline orchestration
# ---------------------------------------------------------------------------


def run_post_merge_pipeline(
    card_name: str,
    slug: str,
    pr_url: str | None = None,
    project: str = "hive_mind",
) -> PipelineResult:
    """Run the full post-merge pipeline.

    Steps (in order):
    1. git_pull_master -- stop on failure
    2. check_container_health -- stop on failure
    3. cleanup_story_directory -- best-effort (log but don't stop)
    4. notify_completion -- best-effort (log but don't stop)

    Note: This does NOT rebuild containers (handled by skill via compose_up)
    and does NOT move the Planka card (handled by skill via planka_move_card).

    Args:
        card_name: Name of the story card.
        slug: Story slug (subdirectory name under stories/).
        pr_url: Optional PR URL to include in notification.
        project: Docker compose project name (default "hive_mind").

    Returns:
        PipelineResult with all step results.
    """
    steps: list[PipelineStepResult] = []

    # Step 1: Git pull -- mandatory
    git_result = git_pull_master()
    steps.append(git_result)
    if not git_result.success:
        return PipelineResult(
            success=False, steps=steps, pr_url=pr_url, card_name=card_name
        )

    # Step 2: Health check -- mandatory
    health_result = check_container_health(project)
    steps.append(health_result)
    if not health_result.success:
        return PipelineResult(
            success=False, steps=steps, pr_url=pr_url, card_name=card_name
        )

    # Step 3: Cleanup story directory -- best-effort
    story_path = str(STORIES_DIR / slug)
    cleanup_result = cleanup_story_directory(story_path)
    steps.append(cleanup_result)

    # Step 4: Notify -- best-effort
    parts = [f"Story closed: {card_name}"]
    if pr_url:
        parts.append(f"PR: {pr_url}")
    parts.append("Containers verified healthy.")
    if cleanup_result.success:
        parts.append(f"Cleanup: {cleanup_result.detail}")
    else:
        parts.append(f"Cleanup warning: {cleanup_result.error}")

    notify_result = notify_completion(" | ".join(parts))
    steps.append(notify_result)

    return PipelineResult(
        success=True, steps=steps, pr_url=pr_url, card_name=card_name
    )
