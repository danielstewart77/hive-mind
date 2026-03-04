"""Subprocess isolation for dynamically created MCP tools (T1-Ring2).

Runs untrusted tool code in a child process with a stripped environment.
Only explicitly declared env vars are passed through, preventing malicious
tools from reading API keys, tokens, or parent process memory.
"""

import json
import logging
import os
import subprocess
import textwrap

_log = logging.getLogger("hive_mind.tool_runner")

_VENV_PYTHON = "/opt/venv/bin/python3"

# Minimal env vars any Python process needs to function
_BASE_ENV = {
    "PATH": "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin",
    "PYTHONPATH": "/usr/src/app",
    "HOME": os.environ.get("HOME", "/home/hivemind"),
    "VIRTUAL_ENV": "/opt/venv",
    "LANG": "C.UTF-8",
}

DEFAULT_TIMEOUT = 30


def run_isolated(
    module_path: str,
    func_name: str,
    kwargs: dict,
    allowed_env: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Run a tool function in a subprocess with stripped environment.

    Args:
        module_path: Dotted module path (e.g. "agents.my_tool").
        func_name: Function name to call within the module.
        kwargs: Keyword arguments to pass to the function.
        allowed_env: Env var names to pass through from the parent process.
                     Only these (plus base vars) are visible to the tool.
        timeout: Max seconds before the subprocess is killed.

    Returns:
        The tool's return value (string).
    """
    env = dict(_BASE_ENV)
    if allowed_env:
        for key in allowed_env:
            val = os.environ.get(key)
            if val:
                env[key] = val

    # The subprocess imports the module, calls the function, prints JSON result
    script = textwrap.dedent(f"""\
        import importlib, json, sys
        sys.path.insert(0, '/usr/src/app')
        mod = importlib.import_module('{module_path}')
        func = getattr(mod, '{func_name}')
        kwargs = json.loads(sys.stdin.read())
        result = func(**kwargs)
        print(json.dumps({{"ok": True, "result": result}}))
    """)

    try:
        result = subprocess.run(
            [_VENV_PYTHON, "-c", script],
            input=json.dumps(kwargs),
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            cwd="/usr/src/app",
        )
    except subprocess.TimeoutExpired:
        _log.warning("Tool %s.%s timed out after %ds", module_path, func_name, timeout)
        return json.dumps({"error": f"Tool timed out after {timeout}s"})

    if result.returncode != 0:
        _log.warning(
            "Tool %s.%s failed (rc=%d): %s",
            module_path, func_name, result.returncode, result.stderr.strip(),
        )
        return json.dumps({"error": f"Tool process failed: {result.stderr.strip()[:500]}"})

    try:
        output = json.loads(result.stdout.strip())
        return output.get("result", result.stdout.strip())
    except (json.JSONDecodeError, KeyError):
        return result.stdout.strip()


def make_isolated_wrapper(
    module_path: str,
    func_name: str,
    allowed_env: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
):
    """Create a wrapper function that runs the target in an isolated subprocess.

    The returned callable has the same signature as a simple tool function
    (accepts **kwargs, returns str) and can replace the raw function in the
    tool registry.
    """
    def wrapper(**kwargs) -> str:
        return run_isolated(module_path, func_name, kwargs, allowed_env, timeout)

    wrapper.__name__ = func_name
    wrapper.__doc__ = f"[isolated] {func_name} (runs in subprocess with stripped env)"
    return wrapper
