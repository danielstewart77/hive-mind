"""What a mind's roles let it use, enforced per tool call and per hook.

Feature 10.18. A mind under role management may use exactly the union of
what its roles grant. On every harness the built-in tools are enforced here,
by one guard and one table of tool names (`FAMILIES`), so a role grants the
same thing on dsh, Claude and Codex (stories 10.18.5 to 10.18.7). Each
adapter hands its harness the guard per spawn: dsh in the hooks file
`effective_hooks` writes, Claude in a settings file (`prepare_claude`),
Codex as config overrides (`prepare_codex`). On dsh the mind's own hooks
are gated by role as well, by the second command:

* ``role_guard.py guard`` — a PreToolUse hook on every tool. A built-in tool
  (shell, files, sub-minds, web) no held role allows is refused, with a
  reason the model reads.
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

**A skill is never refused.** A role gives a mind skills; it does not stand
between a mind and a skill it has. Whatever skill exists for the mind loads,
by tool call or typed as `/name`, whatever its roles list. Keeping a mind
from a skill is done by not giving it the skill (Daniel, 2026-11-19).

Standard library only: hooks run under the container's own ``python3``.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

#: The built-in tool families a role's allow-list names.
TOOLS = ("shell", "files", "sub-minds", "web")

#: Every harness's tool names, by the family a role allows them under. The
#: three harnesses name their tools differently and never the same way, so
#: one table serves all of them and one role grants the same on each.
FAMILIES = {
    # A persistent terminal, a background job and a workflow run are each a
    # shell by another name: a role that leaves shell unticked must leave
    # them unusable too.
    "shell": {"bash", "pwsh", "terminal_open", "terminal_send", "terminal_read",
              "terminal_signal", "terminal_close", "terminal_list",
              "job_kill", "job_list", "job_output",
              "cordis_run", "cordis_define", "cordis_stop", "cordis_undefine",
              # Claude; Codex reports its shell as Bash too.
              "Bash", "BashOutput", "KillShell", "KillBash", "Monitor", "PowerShell",
              "TaskOutput",
              # Codex's own names for a command, its stdin and its REPL.
              "exec_command", "write_stdin", "shell", "shell_command", "local_shell",
              "unified_exec", "js_repl"},
    "files": {"read", "write", "edit", "glob", "grep", "read_image",
              "str_replace", "str_replace_editor",
              "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "NotebookRead",
              "Glob", "Grep", "LS",
              "apply_patch", "view_image"},
    "web": {"web_fetch", "web_search", "WebFetch", "WebSearch"},
    "sub-minds": {"subagent", "list_agents", "send_message", "interrupt_agent",
                  "Task", "Agent", "SendMessage", "TaskStop", "Workflow",
                  # Codex's, which arrive under a `collaboration` prefix too.
                  "spawn_agent", "send_input", "wait_agent", "close_agent",
                  "resume_agent"},
}

#: Tool-name prefixes a family owns: Codex's sub-agent tools are all
#: `collaboration…`, and its web search arrives as an apps MCP tool.
FAMILY_PREFIXES = {
    "sub-minds": ("subagent", "collaboration"),
    "web": ("mcp__codex_apps__search_service",),
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


class NotStarted(RuntimeError):
    """A mind that cannot be held to its roles is not run; the message says why."""


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
    for family, prefixes in FAMILY_PREFIXES.items():
        if tool.startswith(prefixes):
            return family
    if tool in delegates:
        return "sub-minds"
    return None


# ------------------------------------------------------------------ grants

def fetch(mind: str, url: str, token: str, timeout: float = FETCH_TIMEOUT) -> dict | None:
    """The mind's grants from the gateway; None when it could not answer.

    A 404 is an answer, not a failure: a gateway without the route predates
    roles, and so does a mind it has never registered — neither is managed.

    The deadline is the whole call's, name lookup included. urllib's own
    timeout does not cover resolving the host, and a guard still waiting
    when dsh's hook timeout runs out is a guard dsh ignores — the tool runs.
    """
    if not (mind and url):
        return None
    answer: list = []
    worker = threading.Thread(
        target=lambda: answer.append(_fetch(mind, url, token, timeout)), daemon=True)
    worker.start()
    worker.join(timeout + 0.5)
    return answer[0] if answer else None


def _fetch(mind: str, url: str, token: str, timeout: float) -> dict | None:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/minds/{urllib.request.quote(mind, safe='')}/grants",
        headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"managed": False, "unregistered": True}
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
                  env.get("COMMS_URL", ""), (env.get("MIND_KEY") or env.get("COMMS_BEARER_TOKEN", "")).strip())
    if fresh is not None and fresh.get("unregistered") and saved.get("managed"):
        # The gateway knew this mind as managed when the process was started
        # and a managed mind stays managed, so a "never heard of it" now is a
        # credential or an id gone wrong inside this process, not an answer.
        return saved
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
    return None


def essential(name: str) -> bool:
    """Whether a hook keeps the mind alive, judged as the mind server judges
    its required hooks: by the name appearing in the command's name."""
    return any(token in name for token in ESSENTIAL_HOOKS)


def hook_allowed(grants: dict, name: str) -> bool:
    if not grants.get("managed") or essential(name):
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
                if essential(name):
                    continue
                hook["command"] = (f"{python} {shlex.quote(str(GUARD))} gate "
                                   f"{shlex.quote(name)} -- sh -c {shlex.quote(command)}")
    for event, group in guard_hooks(python).items():
        events.setdefault(event, []).insert(0, group)
    return config


def guard_hooks(python: str = "python3", checked: bool = False) -> dict[str, dict]:
    """The guard's hook group per event, in the shape every harness reads.

    PreToolUse on every tool, and nothing else: a typed prompt is never the
    guard's to judge.

    ``checked`` is for Claude and Codex, which run a hook through a shell
    and let a tool run on any exit but 2. A terminal pane keeps its command
    for as long as it lives, so an interpreter or a guard file that has
    since gone would otherwise exit 127 and wave every tool through; the
    command refuses instead.
    """
    def group(command: str) -> dict:
        run = f"{shlex.quote(python)} {shlex.quote(str(GUARD))} {command}"
        if checked:
            run = (f"[ -x {shlex.quote(python)} ] && [ -r {shlex.quote(str(GUARD))} ] || "
                   f"{{ echo 'The role guard cannot run, so this is refused.' >&2; exit {DENY}; }}; "
                   f"exec {run}")
        return {"hooks": [{"type": "command", "timeout": 10, "command": run}]}
    return {"PreToolUse": group("guard")}


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
            python: str = sys.executable or "python3",
            timeout: float = 10.0, on_grants=None) -> dict[str, str]:
    """Write what a dsh spawn needs and return the environment naming it.

    The grants are fetched now and written beside the hooks; a gateway that
    cannot answer refuses the spawn, because the gate and guard would have
    nothing to judge by. The hooks file is written whole and read back
    before dsh is handed it, since a file dsh cannot parse registers no
    hooks at all — guard, memory and rotation together.
    """
    grants = _fetch_for_spawn(mind, url, token, timeout)
    # The adapter answers with the mind's hooks file as it now stands: a mind
    # that had none may have just been given one.
    now = _hand_over(on_grants, grants)
    if isinstance(now, str):
        source_hooks = now
    try:
        return _write_spawn_files(dsh_home, source_hooks, grants, python)
    except (OSError, ValueError) as exc:
        # One kind of failure for every caller: a hooks file that will not
        # parse or a home that cannot be written is a mind that is not run,
        # said out loud, not a turn that ends in silence.
        raise NotStarted(f"the role guard's files could not be written: {exc}") from exc


def _hand_over(on_grants, grants: dict):
    """Give the adapter the grants just fetched, before the harness is
    handed its hooks: it is where the files a role names are put in place
    (`role_files.provision`). Returns what the adapter answered. Whatever it
    raises is its own to report and never refuses the spawn."""
    if on_grants is None:
        return None
    try:
        return on_grants(grants)
    except Exception as exc:  # noqa: BLE001
        _note(f"the files this mind's roles name were not put in place: {exc}")
        return None


def _fetch_for_spawn(mind: str, url: str, token: str, timeout: float) -> dict:
    grants = fetch(mind, url, token, timeout=timeout)
    if grants is None:
        raise NotStarted("the gateway did not say what this mind's roles allow, so it is not started")
    return grants


def _who(mind: str, url: str) -> dict[str, str]:
    """Who the guard asks about, and where, on the guard's own environment.

    A terminal pane inherits from a tmux server that may predate both, and
    a guard asking about the wrong mind is told "unmanaged".
    """
    return {"MIND_ID": mind, "COMMS_URL": url}


def prepare_claude(state_dir: Path, mind: str, url: str, token: str,
                   python: str = sys.executable or "python3",
                   timeout: float = 10.0, on_grants=None) -> tuple[list[str], dict[str, str]]:
    """The arguments and environment a Claude process is started with.

    The guard rides in a settings file of its own (`--settings`), which
    Claude merges with the mind's settings, so the mind's own hooks run as
    before and nothing in its config directory is rewritten. Written for
    every spawn, managed or not, like dsh's: a mind given roles mid-
    conversation is guarded from its next tool call. Raised, like dsh's,
    when the gateway cannot say what the mind may use.
    """
    grants = _fetch_for_spawn(mind, url, token, timeout)
    _hand_over(on_grants, grants)
    try:
        grants_file = state_dir / "role-grants.json"
        write_grants(grants_file, grants)
        settings = state_dir / "role-settings.json"
        write_grants(settings, {"hooks": {event: [group] for event, group
                                          in guard_hooks(python, checked=True).items()}})
    except OSError as exc:
        raise NotStarted(f"the role guard's files could not be written: {exc}") from exc
    return (["--settings", str(settings)],
            {"HIVE_ROLE_GRANTS_FILE": str(grants_file), **_who(mind, url)})


def toml_inline(value) -> str:
    """A value as a TOML inline literal, for a Codex `-c key=value` override."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Not ASCII-escaped: JSON writes a character outside the basic plane
        # as a surrogate pair, which TOML refuses.
        return json.dumps(value, ensure_ascii=False)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(toml_inline(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {toml_inline(v)}"
                               for k, v in value.items()) + "}"
    raise ValueError(f"no TOML form for {type(value).__name__}")


def prepare_codex(codex_home: Path, mind: str, url: str, token: str,
                  python: str = sys.executable or "python3",
                  timeout: float = 10.0, on_grants=None) -> tuple[list[str], dict[str, str]]:
    """The arguments and environment a Codex process is started with.

    The guard goes in as a `-c hooks.PreToolUse=...` override, ahead of
    whatever PreToolUse hooks the mind's `config.toml` already declares
    (an override replaces the key, so those are carried in with it), and
    nothing in the mind's codex home is rewritten. Codex's own web search
    runs inside the model's turn and reaches no hook, so for a managed
    mind whose roles leave web out it is switched off at spawn; a codex
    chat turn is a process of its own, so that too follows a role edit
    from the next turn.
    """
    import tomllib

    grants = _fetch_for_spawn(mind, url, token, timeout)
    _hand_over(on_grants, grants)
    try:
        grants_file = codex_home / "role-grants.json"
        write_grants(grants_file, grants)
    except OSError as exc:
        raise NotStarted(f"the role guard's files could not be written: {exc}") from exc
    try:
        config = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    except OSError:
        config = {}
    except tomllib.TOMLDecodeError as exc:
        # Codex would refuse the same file; said here, with the reason.
        raise NotStarted(f"the mind's codex config.toml does not parse: {exc}") from exc
    hooks = config.get("hooks") if isinstance(config.get("hooks"), dict) else {}
    own = hooks.get("PreToolUse") if isinstance(hooks.get("PreToolUse"), list) else []
    groups = [guard_hooks(python, checked=True)["PreToolUse"], *own]
    try:
        args = ["-c", f"hooks.PreToolUse={toml_inline(groups)}"]
    except ValueError as exc:
        raise NotStarted(f"the mind's codex hooks could not be carried over: {exc}") from exc
    if grants.get("managed") and "web" not in (grants.get("tools") or []):
        args += ["-c", 'web_search="disabled"']
    return args, {"HIVE_ROLE_GRANTS_FILE": str(grants_file), **_who(mind, url)}


def _write_spawn_files(dsh_home: Path, source_hooks: str, grants: dict,
                       python: str) -> dict[str, str]:
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
        _note(f"hook {name} withheld: no role this mind holds grants it")
        return 0
    done = subprocess.run(command, input=stdin)
    return done.returncode


def _note(message: str) -> None:
    """One line in the mind's hook log, so a withheld hook is never silent."""
    home = _mind_env(dict(os.environ)).get("DSH_HOME", "")
    if not home:
        return
    try:
        folder = Path(home).parent / "data" / "auto-remember"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "hooks.log", "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] role_guard: {message}\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    if argv[:1] == ["guard"]:
        return _guard()
    if argv[:1] == ["gate"]:
        return _gate(argv[1:])
    if argv[:1] == ["prompt"]:
        # Nothing is judged on a typed prompt any more. A pane or a settings
        # file written before 2026-11-19 still calls this, so it answers yes
        # without asking the gateway. The prompt is read and dropped, so a
        # harness writing a long one is not left with a closed pipe.
        try:
            sys.stdin.buffer.read()
        except OSError:
            pass
        return 0
    print("usage: role_guard.py guard | gate NAME -- COMMAND...", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
