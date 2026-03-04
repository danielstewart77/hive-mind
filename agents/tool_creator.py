"""Runtime tool creation and dependency management.

Claude Code generates tool code and passes it here for file persistence
and dynamic registration. This is the mechanism for self-improvement.

Security: All submitted code is parsed with the ast module and checked
against a blocklist of dangerous patterns before being written to disk.
Code is staged in agents/staging/ first, validated, then promoted to agents/.
"""

import ast
import os
import re
import shutil
import subprocess
import sys

from agent_tooling import tool, discover_tools, get_tool_function

from core.audit import get_audit_logger
from core.tool_runner import make_isolated_wrapper

AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))
STAGING_DIR = os.path.join(AGENTS_DIR, "staging")
PROJECT_DIR = os.path.dirname(AGENTS_DIR)

os.makedirs(STAGING_DIR, exist_ok=True)

# Audit logger — uses shared RotatingFileHandler from core.audit
_audit = get_audit_logger()

# Strict regex for pip package specifiers — blocks URLs, git repos, local paths
_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9._-]+(\[[\w,\s]+\])?(([=!<>~]=|[<>])[^\s]+)?$")

# ---------------------------------------------------------------------------
# AST-based code validation (T1-Ring1)
# ---------------------------------------------------------------------------

# Dangerous built-in calls
_BLOCKED_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}

# Dangerous module imports
_BLOCKED_MODULES = {"pty", "ctypes", "socket", "multiprocessing", "code", "codeop"}

# Dangerous attribute patterns (e.g. subprocess with shell=True)
_BLOCKED_ATTRS = {"system"}  # os.system


class _ASTValidator(ast.NodeVisitor):
    """Walk the AST and collect security violations."""

    def __init__(self):
        self.violations: list[str] = []

    def visit_Call(self, node: ast.Call):
        # Check for blocked function calls: eval(), exec(), etc.
        if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALLS:
            self.violations.append(
                f"Blocked call: {node.func.id}() at line {node.lineno}"
            )
        # Check for os.system()
        if isinstance(node.func, ast.Attribute) and node.func.attr in _BLOCKED_ATTRS:
            self.violations.append(
                f"Blocked call: .{node.func.attr}() at line {node.lineno}"
            )
        # Check subprocess calls for shell=True
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("run", "Popen", "call", "check_call", "check_output"):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    self.violations.append(
                        f"Blocked: subprocess with shell=True at line {node.lineno}"
                    )
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in _BLOCKED_MODULES:
                self.violations.append(
                    f"Blocked import: {alias.name} at line {node.lineno}"
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module:
            top = node.module.split(".")[0]
            if top in _BLOCKED_MODULES:
                self.violations.append(
                    f"Blocked import: {node.module} at line {node.lineno}"
                )
        self.generic_visit(node)


def _validate_code(code: str) -> list[str]:
    """Parse code and return a list of security violations (empty = safe)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"Syntax error: {e}"]

    validator = _ASTValidator()
    validator.visit(tree)
    return validator.violations


@tool(tags=["system"])
def create_tool(file_name: str, code: str, allowed_env: str = "") -> str:
    """Write a new tool file to agents/ and register it immediately.

    The code MUST use the @tool() decorator from agent_tooling and include
    a clear docstring. Code is validated against a security blocklist before
    registration — dangerous patterns (eval, exec, shell=True, etc.) are
    rejected automatically.

    Dynamically created tools run in an isolated subprocess with a stripped
    environment. Only env vars listed in allowed_env are passed through.

    Example:

        from agent_tooling import tool

        @tool(tags=["example"])
        def my_tool(param: str) -> str:
            \"\"\"Description of what this tool does.\"\"\"
            return result

    Args:
        file_name: Python filename (e.g. "stock_prices.py"). Will be placed in agents/.
        code: Complete Python source code for the tool file.
        allowed_env: Comma-separated env var names the tool needs (e.g. "API_KEY,OTHER_VAR").
                     Only these are passed to the isolated subprocess. Empty = no env vars.

    Returns:
        Confirmation message with the registered tool path.
    """
    if not file_name.endswith(".py"):
        file_name += ".py"

    final_path = os.path.join(AGENTS_DIR, file_name)

    if os.path.exists(final_path):
        return f"Error: {final_path} already exists. Use a different name or edit the existing file."

    # Stage the file first
    staged_path = os.path.join(STAGING_DIR, file_name)

    _audit.info(
        "TOOL_CREATE: file=%s, code_length=%d\n--- CODE START ---\n%s\n--- CODE END ---",
        file_name, len(code), code,
    )

    with open(staged_path, "w") as f:
        f.write(code)

    # Validate the staged code
    violations = _validate_code(code)
    if violations:
        os.remove(staged_path)
        violation_list = "; ".join(violations)
        _audit.warning(
            "TOOL_REJECTED: file=%s, violations=[%s]", file_name, violation_list,
        )
        return f"Error: code rejected by security validation — {violation_list}"

    # Promote from staging to agents/
    shutil.move(staged_path, final_path)

    # Re-discover tools to pick up the new file
    discover_tools(["agents"])

    # Extract function names defined with @tool decorator in the new code
    env_list = [e.strip() for e in allowed_env.split(",") if e.strip()] if allowed_env else []
    module_name = "agents." + file_name.removesuffix(".py")

    # Find tool functions defined in this file and wrap them with isolation
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                is_tool = (
                    (isinstance(decorator, ast.Name) and decorator.id == "tool")
                    or (isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Name)
                        and decorator.func.id == "tool")
                )
                if is_tool:
                    func_name = node.name
                    # Replace the registered function with an isolated wrapper
                    try:
                        from agent_tooling.tool import tool_registry
                        wrapper = make_isolated_wrapper(module_name, func_name, env_list)
                        wrapper.__doc__ = get_tool_function(func_name).__doc__
                        tool_registry.tool_functions[func_name] = wrapper
                        _audit.info(
                            "TOOL_ISOLATED: %s.%s (allowed_env=%s)",
                            module_name, func_name, env_list,
                        )
                    except Exception as e:
                        _audit.warning(
                            "TOOL_ISOLATION_FAILED: %s.%s — %s", module_name, func_name, e,
                        )

    _audit.info("TOOL_PROMOTED: file=%s (passed AST validation, isolated)", file_name)
    return f"Tool registered from {final_path} (isolated subprocess, allowed_env={env_list})"


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
