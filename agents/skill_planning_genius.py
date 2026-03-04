"""Planning Genius skill wrapper as MCP tool."""

import subprocess
from agent_tooling import tool

from core.path_validation import validate_documents_path


@tool(tags=["code"])
def planning_genius(documents_path: str) -> str:
    """Create a precise TDD implementation plan from story documents.

    Deeply explores codebase for patterns and conventions, then produces
    IMPLEMENTATION.md with test-first development steps.

    Args:
        documents_path: Full path to story documents directory (e.g., .../documents/9576)

    Returns:
        Success message with plan location or error details.
    """
    try:
        documents_path = validate_documents_path(documents_path)
    except ValueError as e:
        return f"Path validation failed: {e}"

    result = subprocess.run(
        ["claude", "run", "planning-genius", documents_path],
        capture_output=True,
        text=True,
        timeout=600,  # 10 minute timeout
    )

    if result.returncode == 0:
        return result.stdout
    else:
        return f"Planning failed:\n{result.stderr}\n{result.stdout}"
