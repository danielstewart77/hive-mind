"""Runtime tool creation and dependency management.

Claude Code generates tool code and passes it here for file persistence
and dynamic registration. This is the mechanism for self-improvement.
"""

import logging
import os
import re
import subprocess
import sys
from agent_tooling import tool, discover_tools

AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))
PROJECT_DIR = os.path.dirname(AGENTS_DIR)

# Audit logger for security-sensitive operations
_audit = logging.getLogger("hive_mind.audit")
_audit.setLevel(logging.INFO)
_audit_handler = logging.FileHandler(os.path.join(PROJECT_DIR, "audit.log"))
_audit_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_audit.addHandler(_audit_handler)

# Strict regex for pip package specifiers — blocks URLs, git repos, local paths
_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9._-]+(\[[\w,\s]+\])?(([=!<>~]=|[<>])[^\s]+)?$")


@tool(tags=["system"])
def create_tool(file_name: str, code: str) -> str:
    """Write a new tool file to agents/ and register it immediately.

    The code MUST use the @tool() decorator from agent_tooling and include
    a clear docstring. Example:

        from agent_tooling import tool

        @tool(tags=["example"])
        def my_tool(param: str) -> str:
            \"\"\"Description of what this tool does.\"\"\"
            return result

    Args:
        file_name: Python filename (e.g. "stock_prices.py"). Will be placed in agents/.
        code: Complete Python source code for the tool file.

    Returns:
        Confirmation message with the registered tool path.
    """
    if not file_name.endswith(".py"):
        file_name += ".py"

    file_path = os.path.join(AGENTS_DIR, file_name)

    if os.path.exists(file_path):
        return f"Error: {file_path} already exists. Use a different name or edit the existing file."

    _audit.info(
        "TOOL_CREATE: file=%s, code_length=%d\n--- CODE START ---\n%s\n--- CODE END ---",
        file_name, len(code), code,
    )

    with open(file_path, "w") as f:
        f.write(code)

    # Re-discover tools to pick up the new file
    discover_tools(["agents"])

    return f"Tool registered from {file_path}"


@tool(tags=["system"])
def install_dependency(package: str) -> str:
    """Install a Python package needed by a tool.

    Args:
        package: Package name (e.g. "yfinance", "stripe==5.0.0")

    Returns:
        pip install output (stdout + stderr).
    """
    package = package.strip()
    if not _PACKAGE_RE.match(package):
        _audit.warning("INSTALL_REJECTED: invalid package specifier: %s", package)
        return (
            f"Error: invalid package specifier '{package}'. "
            "Only standard package names with optional version constraints are allowed "
            "(e.g. 'requests', 'stripe==5.0.0', 'boto3>=1.20')."
        )

    _audit.info("INSTALL_DEPENDENCY: package=%s", package)

    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", package],
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = (result.stdout + result.stderr).strip()
    return output if output else "Package installed successfully."
