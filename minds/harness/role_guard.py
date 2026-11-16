"""What a DSH mind's roles let it use, enforced per tool call and per hook.

Feature 10.18. A mind under role management may use exactly the union of
what its roles grant. On dsh that is enforced here, by two commands the
adapter writes into the hooks file it hands dsh (`effective_hooks`):

* ``role_guard.py guard`` — a PreToolUse hook on every tool. A built-in tool
  (shell, files, sub-minds, web) no held role allows is refused, and so is a
  skill no held role grants, with a reason the model reads.
* ``role_guard.py gate NAME -- COMMAND...`` — wraps each of the mind's own
  hooks. The hook runs when a held role grants NAME and is skipped when none
  does.

**Read on every event, never once.** dsh reads its hooks file once, when the
process starts, so what the roles grant cannot be baked into that file: a
resident process would keep a revoked tool until it restarted. Each event
asks the gateway (`GET /minds/{id}/grants`) instead, so a role changed on
the console is obeyed on the very next tool call (protected behaviour 2).

**Fail closed.** dsh blocks a tool only on exit 2 or an explicit deny; any
other failure lets it run. So everything here is caught and turned into a
refusal for a managed mind: a guard that crashes, times out against the
gateway or reads a broken file denies rather than waving the tool through.
When the gateway does not answer, the grants the adapter wrote at spawn
(`HIVE_ROLE_GRANTS_FILE`) stand in — the gateway had just dispatched that
turn, so they are at most one turn old.

**Hooks a mind cannot run without are never gated** (`ESSENTIAL_HOOKS`):
memory capture, the rotation check and fire, contextual retrieval, the
surface marker, the sub-agent reporter the console draws sub-minds from, and
this guard itself — the set the mind server requires before it offers dsh,
on either kind of mind. A role leaving one out would not restrict the mind; it would
stop it remembering, or let it grow until it overflowed.

Standard library only: hooks run under the container's own ``python3``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

#: The built-in tool families a role's allow-list names.
TOOLS = ("shell", "files", "sub-minds", "web")

#: dsh's tool names, by the family a role allows them under.
FAMILIES = {
    "shell": {"bash", "pwsh"},
    "files": {"read", "write", "edit", "glob", "grep", "read_image",
              "str_replace", "str_replace_editor"},
    "web": {"web_fetch", "web_search"},
    "sub-minds": {"subagent", "list_agents", "send_message", "interrupt_agent"},
}

#: The hooks that keep a mind alive rather than shape what it does.
ESSENTIAL_HOOKS = frozenset({
    "auto_remember", "rotation_check", "rotation_fire", "contextual_retrieval",
    "surface_inject", "subagent_report", "role_guard",
})

#: Script suffixes a hook command's name is read from.
SCRIPT_SUFFIXES = (".sh", ".py", ".js", ".mjs", ".ts")

#: Seconds the gateway gets to answer before the spawn-time grants stand in.
FETCH_TIMEOUT = 2.0

#: The exit code dsh's hook bridge treats as a refusal, stderr its reason.
DENY = 2

GUARD = Path(__file__).resolve()


# ------------------------------------------------------------------ naming

def hook_name(command: str) -> str:
    """The name a role grants a hook command by.

    The file name, without its suffix, of the first argument that is a
    script; a command with no script argument is named by its whole text,
    so it can still be granted exactly.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for word in words:
        base = word.rsplit("/", 1)[-1]
        for suffix in SCRIPT_SUFFIXES:
            if base.endswith(suffix) and len(base) > len(suffix):
                return base[: -len(suffix)]
    return command.strip()


def family_of(tool: str, delegates: set[str]) -> str | None:
    """The built-in family a dsh tool belongs to, or None for any other."""
    for family, names in FAMILIES.items():
        if tool in names:
            return family
    if tool.startswith("subagent") or tool in delegates:
        return "sub-minds"
    return None


# ------------------------------------------------------------------ grants

def fetch(mind: str, url: str, token: str, timeout: float = FETCH_TIMEOUT) -> dict | None:
    """The mind's grants from the gateway; None when it could not answer.

    A 404 is an answer, not a failure: a gateway without the route predates
    roles, and so does a mind it has never registered — neither is managed.
    """
    if not (mind and url):
        return None
    request = urllib.request.Request(
        f"{url.rstrip('/')}/minds/{urllib.request.quote(mind, safe='')}/grants",
        headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"managed": False}
        return None
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def write_grants(path: Path, grants: dict) -> None:
    """Write the grants file whole or not at all: a reader never sees half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=".role-grants-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(grants, handle)
        os.replace(staging, path)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise


def read_grants(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _mind_env(env: dict[str, str]) -> dict[str, str]:
    """The environment, with what it lacks filled from the mind's own folder.

    A browser-terminal pane does not always carry the mind's gateway
    credential; the mind folder's `.env` and `runtime.yaml` do, the same
    fallback every other hook here uses. The mind folder is the parent of
    `DSH_HOME`.
    """
    merged = dict(env)
    home = merged.get("DSH_HOME", "")
    if not home:
        return merged
    folder = Path(home).parent
    try:
        for line in (folder / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                merged.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass
    if not merged.get("MIND_ID"):
        try:
            for line in (folder / "runtime.yaml").read_text(encoding="utf-8").splitlines():
                if line.startswith("mind_id:"):
                    merged["MIND_ID"] = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
        except OSError:
            pass
    return merged


def current(env: dict[str, str] | None = None) -> dict:
    """The grants to judge this event by: the gateway's now, else spawn's.

    Raises when neither can be had, which the callers turn into a refusal.
    """
    env = _mind_env(dict(os.environ) if env is None else env)
    path_text = env.get("HIVE_ROLE_GRANTS_FILE", "")
    path = Path(path_text) if path_text else None
    saved = read_grants(path) or {}
    fresh = fetch(env.get("MIND_ID") or env.get("MIND_NAME", ""),
                  env.get("COMMS_URL", ""), env.get("COMMS_BEARER_TOKEN", ""))
    if fresh is not None:
        # The delegate tool names are the adapter's to record: they come from
        # the agents overlay this process was started with.
        fresh.setdefault("delegates", saved.get("delegates", []))
        return fresh
    if saved:
        return saved
    raise RuntimeError("neither the gateway nor the spawn's grants file said what this mind may use")


# ------------------------------------------------------------------ decisions

def tool_refusal(grants: dict, tool: str, arguments: dict | None) -> str | None:
    """Why this tool call is refused, or None when the roles allow it."""
    if not grants.get("managed"):
        return None
    family = family_of(tool, set(grants.get("delegates") or []))
    if family is not None and family not in (grants.get("tools") or []):
        return (f"The {family} tool ({tool}) is not allowed for this mind's roles "
                f"({', '.join(grants.get('roles') or []) or 'none'}).")
    if tool == "skill":
        name = str((arguments or {}).get("name") or "").strip()
        if name not in (grants.get("skills") or []):
            return (f"The skill {name or '(unnamed)'} is not granted by this mind's roles "
                    f"({', '.join(grants.get('roles') or []) or 'none'}).")
    return None


def hook_allowed(grants: dict, name: str) -> bool:
    if not grants.get("managed") or name in ESSENTIAL_HOOKS:
        return True
    return name in (grants.get("hooks") or [])


# ------------------------------------------------------------------ the file dsh reads

def effective_hooks(source: dict | None, python: str = "python3") -> dict:
    """The hooks file dsh is handed: the mind's own, gated, plus the guard.

    Every hook the mind declares is kept in its place and order; each one a
    role may withhold is wrapped by the gate, and the guard is added on
    PreToolUse for every tool. Written whatever the mind declared — a mind
    with no hooks file of its own still gets the guard, or it would run
    unguarded while the console showed it holding roles.
    """
    config = json.loads(json.dumps(source or {}))
    events = config.setdefault("hooks", {})
    if not isinstance(events, dict):
        raise ValueError("the mind's hooks file has no hooks mapping")
    for groups in events.values():
        for group in groups if isinstance(groups, list) else []:
            for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                command = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(command, str) or not command.strip():
                    continue
                name = hook_name(command)
                if name in ESSENTIAL_HOOKS:
                    continue
                hook["command"] = (f"{python} {shlex.quote(str(GUARD))} gate "
                                   f"{shlex.quote(name)} -- sh -c {shlex.quote(command)}")
    events.setdefault("PreToolUse", []).insert(0, {"hooks": [{
        "type": "command", "command": f"{python} {shlex.quote(str(GUARD))} guard",
        "timeout": 10}]})
    return config


def delegate_names(patch_file: Path) -> list[str]:
    """The delegate tool names in a mind's agents overlay, read plainly.

    The overlay is YAML written by the skill render, one `toolName:` per
    delegate row; a line scan is enough and keeps this standard library.
    """
    try:
        lines = patch_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    names = []
    for line in lines:
        stripped = line.strip().lstrip("- ").strip()
        if stripped.startswith("toolName:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            if value:
                names.append(value)
    return names


def prepare(dsh_home: Path, source_hooks: str, mind: str, url: str, token: str,
            python: str = "python3") -> dict[str, str]:
    """Write what a dsh spawn needs and return the environment naming it.

    The grants are fetched now and written beside the hooks; a gateway that
    cannot answer refuses the spawn, because the gate and guard would have
    nothing to judge by. The hooks file is written whole and read back
    before dsh is handed it, since a file dsh cannot parse registers no
    hooks at all — guard, memory and rotation together.
    """
    grants = fetch(mind, url, token, timeout=10.0)
    if grants is None:
        raise RuntimeError("the gateway did not say what this mind's roles allow, so it is not started")
    grants["delegates"] = delegate_names(dsh_home / "agents.patch.yml")
    grants_file = dsh_home / "role-grants.json"
    write_grants(grants_file, grants)
    source = None
    if source_hooks and Path(source_hooks).is_file():
        # A file that is there and cannot be parsed refuses the spawn: dsh
        # would register none of its hooks, memory and rotation among them.
        # One that is not there is a mind with no hooks of its own, which
        # the availability check already reports; it still gets the guard.
        source = json.loads(Path(source_hooks).read_text(encoding="utf-8"))
    hooks_file = dsh_home / "hooks.effective.json"
    write_grants(hooks_file, effective_hooks(source, python))
    json.loads(hooks_file.read_text(encoding="utf-8"))
    return {"DSH_HOOKS_CONFIG": str(hooks_file), "HIVE_ROLE_GRANTS_FILE": str(grants_file)}


# ------------------------------------------------------------------ commands

def _guard() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
        grants = current()
        reason = tool_refusal(grants, str(event.get("tool_name") or ""),
                              event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {})
    except Exception as exc:  # noqa: BLE001 - every failure is a refusal
        print(f"The role guard could not decide, so the tool is refused: {exc}", file=sys.stderr)
        return DENY
    if reason:
        print(reason, file=sys.stderr)
        return DENY
    return 0


def _gate(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("usage: role_guard.py gate NAME -- COMMAND...", file=sys.stderr)
        return 1
    name, command = argv[0], argv[2:]
    stdin = sys.stdin.buffer.read()
    try:
        allowed = hook_allowed(current(), name)
    except Exception:  # noqa: BLE001 - a managed mind's hook is withheld
        saved = read_grants(Path(os.environ["HIVE_ROLE_GRANTS_FILE"])) if os.environ.get("HIVE_ROLE_GRANTS_FILE") else None
        allowed = bool(saved) and not saved.get("managed")
    if not allowed:
        return 0
    done = subprocess.run(command, input=stdin)
    return done.returncode


def main(argv: list[str]) -> int:
    if argv[:1] == ["guard"]:
        return _guard()
    if argv[:1] == ["gate"]:
        return _gate(argv[1:])
    print("usage: role_guard.py guard | gate NAME -- COMMAND...", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
