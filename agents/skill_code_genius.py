"""Code Genius skill wrapper as MCP tool."""

import subprocess
from agent_tooling import tool

from core.path_validation import validate_documents_path


@tool(tags=["code"])
def code_genius(documents_path: str) -> str:
    """Implement features with TDD and ensure builds pass.

    Follows implementation plan, runs Angular and Dotnet builds,
    and self-corrects with up to 5 retries per build type.

    Args:
        documents_path: Full path to story documents directory (e.g., .../docs/9576)

    Returns:
        Build status and completion message.
    """
    try:
        documents_path = validate_documents_path(documents_path)
    except ValueError as e:
        return f"Path validation failed: {e}"

    result = subprocess.run(
        ["claude", "run", "code-genius", documents_path],
        capture_output=True,
        text=True,
        timeout=1800,  # 30 minute timeout for build cycles
    )

    if result.returncode == 0:
        return result.stdout
    else:
        return f"Implementation failed:\n{result.stderr}\n{result.stdout}"
