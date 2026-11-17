"""Write a dsh mind's hooks: ``<minds>/<mind>/.dsh/hooks/`` and ``hooks.json``.

A dsh conversation is offered only when the mind's hook file carries the
hooks ``minds.mind_server`` requires — ``auto_remember`` and
``rotation_check`` on Stop, and a UserPromptSubmit hook — because a
conversation without them is one whose memory stops being written and which
grows until it overflows. This writes that set, from the scripts in
``minds/harness/dsh_hook_scripts/``:

* Stop: ``auto_remember.py`` (memory capture to lucent, classified through
  the inference proxy's chat route with the mind's key and
  ``HIVE_CLASSIFIER_MODEL``) and ``rotation_check.py`` (size report and rotation arm to
  comms).
* UserPromptSubmit: ``contextual_retrieval.py`` (standing rules, relevant
  memory and known persons from lucent) and ``rotation_fire.py`` (a terminal
  pane's staged rotation, fired on a typed prompt).

The scripts reach every service through the mind's own environment and
resolve every path from where they sit, so the copies are the same for every
mind. They are standard library only and run under the container's
``python3``. The hook file names them by their path inside the container,
because the adapter that runs the rotation check after a failed turn execs
the command without a shell.

The scripts are rewritten on every run, so a fix shipped in this repository
reaches a mind when the installer runs again. ``hooks.json`` is written only
when the mind has none: a hook added to it by hand is the operator's.

Standard library only::

    python3 -m minds.harness.dsh_hooks --mind NAME --out MINDS_DIR \\
        [--container-minds-dir /usr/src/app/minds]

It prints one JSON object naming what it wrote.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from pathlib import Path

from minds.harness.dsh_profile import profile_dir
from minds.harness.hook_commands import managed_python_command

#: Where the scripts are kept in this repository.
SCRIPTS_DIR = Path(__file__).resolve().parent / "dsh_hook_scripts"

#: Every file a mind's hooks directory receives.
SCRIPTS = (
    "hive_mind_env.py",
    "dsh_transcript.py",
    "auto_remember.py",
    "rotation_check.py",
    "rotation_fire.py",
    "contextual_retrieval.py",
)

#: Where a mind container sees the minds folder.
DEFAULT_CONTAINER_MINDS_DIR = "/usr/src/app/minds"

#: Per event, the hook groups in order: ``(script, timeout seconds or None)``.
#: A UserPromptSubmit hook holds the prompt, so each has a bound.
EVENTS = {
    "Stop": [[("auto_remember.py", None), ("rotation_check.py", None)]],
    "UserPromptSubmit": [[("contextual_retrieval.py", 15)], [("rotation_fire.py", 15)]],
}


def hooks_config(hooks_dir_in_container: str, *, python: str = "/opt/venv/bin/python") -> dict:
    """The hook file for scripts found at this path inside the container."""
    config: dict = {"hooks": {}}
    for event, groups in EVENTS.items():
        config["hooks"][event] = [
            {"hooks": [
                {"type": "command",
                 "command": f"{shlex.quote(python if script == 'rotation_check.py' else 'python3')} "
                            f"{shlex.quote(hooks_dir_in_container + '/' + script)}",
                 **({"timeout": timeout} if timeout else {})}
                for script, timeout in group
            ]}
            for group in groups
        ]
    return config


def _managed_rotation(command: object, managed_scripts: set[str]) -> bool:
    return any(
        managed_python_command(command, script_path=script) is not None
        for script in managed_scripts
    )


def _merge_rotation_hooks(existing: dict, generated: dict,
                          managed_scripts: set[str]) -> dict:
    """Refresh the two managed rotation entries without replacing user hooks."""
    result = dict(existing)
    hooks = dict(existing.get("hooks") or {})
    generated_hooks = generated.get("hooks") or {}
    for event in ("Stop", "UserPromptSubmit"):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
        kept = []
        for group in groups:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            commands = group.get("hooks")
            if not isinstance(commands, list):
                kept.append(group)
                continue
            retained = [hook for hook in commands if not (
                isinstance(hook, dict)
                and _managed_rotation(hook.get("command"), managed_scripts)
            )]
            if retained:
                kept.append({**group, "hooks": retained})
        wanted = []
        for group in generated_hooks.get(event, []):
            selected = [hook for hook in group.get("hooks", [])
                        if _managed_rotation(hook.get("command"), managed_scripts)]
            if selected:
                wanted.append({**group, "hooks": selected})
        hooks[event] = kept + wanted
    result["hooks"] = hooks
    return result


def write_hooks(mind: str, minds_dir: Path,
                container_minds_dir: str = DEFAULT_CONTAINER_MINDS_DIR,
                *, python: str = "/opt/venv/bin/python",
                home: Path | None = None,
                container_hooks_dir: str | None = None) -> dict:
    """Write this mind's hook scripts and, if it has none, its hook file."""
    if not container_minds_dir.startswith("/"):
        raise ValueError(f"the container minds folder must be absolute: {container_minds_dir!r}")
    dsh_home = Path(home) if home is not None else profile_dir(mind, minds_dir).parent.parent
    hooks_dir = dsh_home / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for script in SCRIPTS:
        target = hooks_dir / script
        shutil.copyfile(SCRIPTS_DIR / script, target)
        target.chmod(0o755)
    in_container = (container_hooks_dir or
                    (str(dsh_home) if home is not None else
                     f"{container_minds_dir.rstrip('/')}/{mind}/.dsh/hooks"))
    config_path = dsh_home / "hooks.json"
    generated = hooks_config(in_container, python=python)
    managed_scripts = {
        f"{in_container.rstrip('/')}/{name}"
        for name in ("rotation_check.py", "rotation_fire.py")
    }
    if config_path.exists():
        original = config_path.read_text()
        existing = json.loads(original)
        if not isinstance(existing, dict):
            raise ValueError(f"dsh hooks configuration must be a JSON object: {config_path}")
        merged = _merge_rotation_hooks(existing, generated, managed_scripts)
        encoded = json.dumps(merged, indent=2) + "\n"
        written = encoded != original
    else:
        encoded = json.dumps(generated, indent=2) + "\n"
        written = True
    if written:
        config_path.write_text(encoded)
    return {
        "mind": mind,
        "hooks_dir": str(hooks_dir),
        "hooks_config": str(config_path),
        "hooks_config_written": written,
        "scripts": list(SCRIPTS),
        "hooks_dir_in_container": in_container,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m minds.harness.dsh_hooks",
        description="Write <out>/<mind>/.dsh/hooks/ and <out>/<mind>/.dsh/hooks.json.",
    )
    parser.add_argument("--mind", required=True, help="the mind's folder name")
    parser.add_argument("--out", required=True, type=Path,
                        help="the minds folder the mind's own folder sits in")
    parser.add_argument("--container-minds-dir", default=DEFAULT_CONTAINER_MINDS_DIR,
                        help="where the mind's container sees that minds folder")
    parser.add_argument("--python", default="/opt/venv/bin/python",
                        help="the mind application's Python interpreter for policy hooks")
    args = parser.parse_args(argv)
    try:
        result = write_hooks(args.mind, args.out, args.container_minds_dir,
                             python=args.python)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
