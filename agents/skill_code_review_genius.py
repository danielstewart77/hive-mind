"""Code Review Genius skill wrapper as MCP tool."""

import subprocess
from agent_tooling import tool


@tool(tags=["code"])
def code_review_genius(documents_path: str) -> str:
    """Perform structured code review against story requirements.

    Reviews all changed files using 9 quality dimensions and produces
    CODE-REVIEW.md with findings and remediation plan.

    Args:
        documents_path: Full path to story documents directory (e.g., .../documents/9531)

    Returns:
        Review summary or error details.
    """
    result = subprocess.run(
        ["claude", "run", "code-review-genius", documents_path],
        capture_output=True,
        text=True,
        timeout=600,
    )

    if result.returncode == 0:
        return result.stdout
    else:
        return f"Code review failed:\n{result.stderr}\n{result.stdout}"
