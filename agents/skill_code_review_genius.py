"""Code Review Genius skill wrapper as MCP tool."""

import subprocess
from agent_tooling import tool

from core.path_validation import validate_documents_path


@tool(tags=["code"])
def code_review_genius(documents_path: str) -> str:
    """Perform structured code review against story requirements.

    Reviews all changed files using 9 quality dimensions and produces
    CODE-REVIEW.md with findings and remediation plan.

    Args:
        documents_path: Full path to story documents directory (e.g., .../docs/9531)

    Returns:
        Review summary or error details.
    """
    try:
        documents_path = validate_documents_path(documents_path)
    except ValueError as e:
        return f"Path validation failed: {e}"

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
