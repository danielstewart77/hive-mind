"""Installable Claude and Codex rotation hooks for one mind.

The command reads the harness's native Stop/UserPromptSubmit payload. It
measures the latest conversation state, loads the mind's current runtime
preference, and reports/arms through the existing comms routes. Capacity is
reused only when it belongs to the model named by the event.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import sys
import time
import tomllib
import urllib.request
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from minds import models_api, rotation_marker_api, rotation_policy


def _runtime_path(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("HIVE_RUNTIME_PATH") or
                APP_ROOT / "minds" / os.environ.get("MIND_NAME", "example") / "runtime.yaml")


def _command(python: str, app_root: Path, action: str, harness: str,
             runtime_path: Path) -> str:
    values = [python, str(app_root / "minds" / "harness" / "rotation_hooks.py"),
              action, "--harness", harness, "--runtime", str(runtime_path),
              "--app-root", str(app_root)]
    return " ".join(shlex.quote(value) for value in values)


def _is_rotation(command: object, script_path: Path) -> bool:
    if not isinstance(command, str) or not command:
        return False
    try:
        arguments = shlex.split(command)
    except ValueError:
        return False
    managed_path = Path(script_path).resolve()
    for index, argument in enumerate(arguments[:-1]):
        try:
            candidate = Path(argument).resolve()
        except (OSError, ValueError):
            continue
        if candidate == managed_path and arguments[index + 1] in ("check", "fire"):
            return True
    return False


def _claude_settings(text: str) -> dict:
    value = json.loads(text) if text.strip() else {}
    if not isinstance(value, dict):
        raise TypeError("Claude settings must be a JSON object")
    return value


def _merge_claude_hooks(settings: dict, generated: dict, script_path: Path) -> dict:
    result = dict(settings)
    hooks = dict(settings.get("hooks") or {})
    for event in ("Stop", "UserPromptSubmit"):
        groups = hooks.get(event)
        kept = []
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            commands = group.get("hooks")
            if not isinstance(commands, list):
                kept.append(group)
                continue
            retained = [item for item in commands if not (
                isinstance(item, dict) and _is_rotation(item.get("command"), script_path)
            )]
            if retained:
                kept.append({**group, "hooks": retained})
        wanted = []
        for group in generated["hooks"].get(event, []):
            selected = [item for item in group["hooks"]
                        if _is_rotation(item.get("command"), script_path)]
            if selected:
                # Claude hooks are grouped by matcher. Match all Stop and user
                # prompt events unless an operator's own matcher narrows them.
                wanted.append({"matcher": "", "hooks": selected})
        hooks[event] = kept + wanted
    result["hooks"] = hooks
    return result


def _codex_hook_group(event: str, command: str) -> str:
    quoted = json.dumps(command)
    return (f"[[hooks.{event}]]\n"
            "[[hooks." + event + ".hooks]]\n"
            'type = "command"\n'
            f"command = {quoted}\n")


def _remove_codex_rotation_entries(text: str, script_path: Path) -> str:
    """Remove generated rotation commands while retaining sibling user hooks."""
    lines = text.splitlines(keepends=True)
    start: int | None = None
    event: str | None = None
    chunks: list[tuple[int, int, str]] = []
    header_re = re.compile(r"^\s*\[\[hooks\.(Stop|UserPromptSubmit)\]\]\s*(?:#.*)?$")
    nested_re = re.compile(
        r"^\s*\[\[hooks\.(Stop|UserPromptSubmit)\.hooks\]\]\s*(?:#.*)?$"
    )

    def finish(end: int) -> None:
        nonlocal start, event
        if start is not None and event is not None:
            chunks.append((start, end, "".join(lines[start:end])))
        start = None
        event = None

    for index, line in enumerate(lines):
        match = header_re.match(line.rstrip("\r\n"))
        if match:
            finish(index)
            start, event = index, match.group(1)
            continue
        if start is not None and line.lstrip().startswith("["):
            stripped = line.strip()
            if not stripped.startswith(f"[[hooks.{event}.") and not stripped.startswith(
                f"[hooks.{event}."
            ):
                finish(index)
    finish(len(lines))
    remove = set()
    for first, last, _body in chunks:
        event = header_re.match(lines[first].rstrip("\r\n")).group(1)
        starts = [index for index in range(first + 1, last)
                  if (match := nested_re.match(lines[index].rstrip("\r\n")))
                  and match.group(1) == event]
        entries = []
        for offset, entry_start in enumerate(starts):
            entry_end = starts[offset + 1] if offset + 1 < len(starts) else last
            entry = "".join(lines[entry_start:entry_end])
            try:
                parsed = tomllib.loads(entry)
                event_hooks = parsed.get("hooks", {}).get(event, {})
                managed = any(
                    isinstance(hook, dict)
                    and _is_rotation(hook.get("command"), script_path)
                    for hook in event_hooks.get("hooks", [])
                )
            except (tomllib.TOMLDecodeError, AttributeError, TypeError):
                managed = False
            entries.append((entry_start, entry_end, managed))

        managed_entries = [entry for entry in entries if entry[2]]
        if managed_entries and len(managed_entries) == len(entries):
            # The group contains no sibling hook to retain. Removing the whole
            # generated group also prevents empty Stop tables accumulating on
            # each refresh.
            remove.update(range(first, last))
        else:
            for entry_start, entry_end, managed in managed_entries:
                if managed:
                    remove.update(range(entry_start, entry_end))
    return "".join(line for index, line in enumerate(lines) if index not in remove)


def _write_claude(home: Path, runtime_path: Path, python: str, app_root: Path) -> bool:
    path = home / "settings.json"
    original = path.read_text() if path.exists() else "{}\n"
    settings = _claude_settings(original)
    generated = {"hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": _command(
            python, app_root, "check", "claude", runtime_path)}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": _command(
            python, app_root, "fire", "claude", runtime_path)}]}],
    }}
    script_path = app_root / "minds" / "harness" / "rotation_hooks.py"
    merged = _merge_claude_hooks(settings, generated, script_path)
    encoded = json.dumps(merged, indent=2) + "\n"
    if encoded != original:
        path.write_text(encoded)
        return True
    return False


def _write_codex(home: Path, runtime_path: Path, python: str, app_root: Path) -> bool:
    path = home / "config.toml"
    original = path.read_text() if path.exists() else ""
    if original.strip():
        tomllib.loads(original)
    script_path = app_root / "minds" / "harness" / "rotation_hooks.py"
    retained = _remove_codex_rotation_entries(original, script_path)
    suffix = "\n" if retained and not retained.endswith("\n\n") else ""
    encoded = (retained + suffix
               + _codex_hook_group("Stop", _command(
                   python, app_root, "check", "codex", runtime_path))
               + "\n"
               + _codex_hook_group("UserPromptSubmit", _command(
                   python, app_root, "fire", "codex", runtime_path)))
    tomllib.loads(encoded)
    if encoded != original:
        path.write_text(encoded)
        return True
    return False


def write_harness_hooks(
    harness: str,
    home: Path,
    *,
    runtime_path: Path,
    python: str | None = None,
    app_root: Path | None = None,
) -> dict[str, Any]:
    """Install/refresh only managed hooks in an already provisioned home."""
    if harness not in ("claude", "codex"):
        raise ValueError(f"unsupported native hook harness: {harness}")
    home = Path(home)
    if not home.is_dir():
        return {"installed": False, "reason": "home_not_declared", "harness": harness}
    runtime_path = Path(runtime_path).resolve()
    root = Path(app_root or APP_ROOT).resolve()
    interpreter = python or sys.executable
    changed = (_write_claude(home, runtime_path, interpreter, root) if harness == "claude"
               else _write_codex(home, runtime_path, interpreter, root))
    return {"installed": True, "changed": changed, "harness": harness,
            "config": str(home / ("settings.json" if harness == "claude" else "config.toml"))}


def _usage_total(usage: object, fields: tuple[str, ...]) -> int | None:
    if not isinstance(usage, dict):
        return None
    values = []
    for name in fields:
        value = usage.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values.append(value)
    total = sum(values)
    return total if total > 0 else None


def _read_json_lines(path: object) -> list[dict]:
    if not path:
        return []
    try:
        lines = Path(str(path)).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def measure_claude(event: dict) -> dict[str, Any]:
    entries = _read_json_lines(event.get("transcript_path"))
    start = 0
    for index in range(len(entries) - 1, -1, -1):
        if entries[index].get("type") == "user":
            start = index + 1
            break
    for entry in reversed(entries[start:]):
        if entry.get("type") != "assistant":
            continue
        message = entry.get("message") or {}
        usage = message.get("usage") if isinstance(message, dict) else None
        total = _usage_total(usage, (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
            "output_tokens",
        ))
        if total is not None:
            return {"tokens": total, "kind": "usage",
                    "model": str(message.get("model") or event.get("model") or "")}
        # The latest assistant call is the relevant request. Do not resurrect
        # an older token count when this call returned no usable measurement.
        return {"tokens": None, "kind": "none",
                "model": str(message.get("model") or event.get("model") or "")}
    return {"tokens": None, "kind": "none", "model": str(event.get("model") or "")}


def measure_codex(event: dict) -> dict[str, Any]:
    entries = _read_json_lines(event.get("transcript_path"))
    model = ""
    latest_usage: object = None
    for entry in entries:
        if entry.get("type") == "turn_context":
            payload = entry.get("payload") or {}
            model = str(payload.get("model") or model)
        elif entry.get("type") == "event_msg":
            payload = entry.get("payload") or {}
            if payload.get("type") == "token_count":
                info = payload.get("info") or {}
                latest_usage = info.get("last_token_usage")
    usage = latest_usage if isinstance(latest_usage, dict) else {}
    total = usage.get("total_tokens")
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        total = _usage_total(usage, ("input_tokens", "cached_input_tokens", "output_tokens"))
    return {"tokens": total if isinstance(total, int) and total > 0 else None,
            "kind": "usage" if isinstance(total, int) and total > 0 else "none",
            "model": model or str(event.get("model") or "")}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


async def _proxy_window(runtime_path: Path, harness: str, model: str) -> int | None:
    if not model:
        return None
    try:
        window, _ = await models_api.model_sizing(runtime_path, harness, model)
    except Exception:  # noqa: BLE001 — a missing listing disables proactive rotation
        return None
    return _positive_int(window)


def _window(measured: dict, harness: str, runtime_path: Path) -> int | None:
    model = str(measured.get("model") or "")
    spawned_model = os.environ.get("HIVE_MODEL", "")
    spawned_harness = os.environ.get("HIVE_HARNESS", "")
    if model and model == spawned_model and harness == spawned_harness:
        raw = os.environ.get("HIVE_MODEL_CONTEXT_WINDOW", "")
        if raw.isascii() and raw.isdecimal():
            return _positive_int(int(raw))
    return asyncio.run(_proxy_window(runtime_path, harness, model))


def _post(url: str, bearer: str, payload: dict, *, timeout: float = 10) -> dict:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {bearer}",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode())
    return result if isinstance(result, dict) else {}


def _state_dir(runtime_path: Path) -> Path:
    explicit = os.environ.get("AUTO_REMEMBER_LOG_DIR")
    return Path(explicit) if explicit else runtime_path.parent / "data" / "auto-remember"


def _log(runtime_path: Path, text: str) -> None:
    try:
        folder = _state_dir(runtime_path)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "hooks.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] rotation_hooks: {text}\n")
    except OSError:
        pass


def check(event: dict, harness: str, runtime_path: Path) -> dict | None:
    conversation_id = str(event.get("session_id") or event.get("thread_id") or "")
    if not conversation_id:
        return None
    measured = measure_claude(event) if harness == "claude" else measure_codex(event)
    if not measured["model"]:
        measured["model"] = os.environ.get("HIVE_MODEL", "")
    policy = rotation_policy.read_policy(runtime_path)
    percentage = policy["percentage"] if policy["status"] == "ok" else None
    window = _window(measured, harness, runtime_path)
    threshold = rotation_policy.effective_threshold(window, percentage)
    invalid = policy["status"] != "ok"
    if invalid:
        _log(runtime_path, f"invalid_runtime_policy code=invalid_runtime_policy "
             f"reason={policy.get('reason') or 'invalid_value'} sid={conversation_id}")

    comms_url = os.environ.get("COMMS_URL", "").rstrip("/")
    bearer = os.environ.get("COMMS_BEARER_TOKEN", "")
    client_type = os.environ.get("SURFACE", "")
    client_ref = os.environ.get("CLIENT_REF", "")
    report = {"conversation_id": conversation_id, "kind": measured["kind"],
              "tokens": measured["tokens"], "model": measured["model"] or None,
              "client_type": client_type, "client_ref": client_ref,
              "surface": os.environ.get("HIVE_SURFACE") or None}
    if window is not None:
        report["window"] = window
    if threshold is not None and not invalid:
        report["threshold"] = threshold
    if comms_url and bearer and client_type and client_ref:
        try:
            _post(f"{comms_url}/sessions/context", bearer, report)
        except Exception as exc:  # noqa: BLE001 — a failed report must not fail a turn
            _log(runtime_path, f"context report failed sid={conversation_id}: {type(exc).__name__}")

    overflowed = event.get("error_code") == "CONTEXT_WINDOW_EXCEEDED"
    rotate = bool(not invalid and not overflowed and threshold is not None
                  and measured["tokens"] is not None and measured["tokens"] >= threshold)
    if rotate and comms_url and bearer and client_type and client_ref:
        try:
            reply = _post(f"{comms_url}/sessions/arm-rotation", bearer, {
                "conversation_id": conversation_id, "client_type": client_type,
                "client_ref": client_ref, "started_at": None,
            }, timeout=30)
            if reply.get("ok") and os.environ.get("HIVE_SURFACE") == "terminal":
                marker_path = rotation_marker_api.state_dir(runtime_path.parent) / (
                    f"rotation-staged-{conversation_id}.json")
                marker_path.parent.mkdir(parents=True, exist_ok=True)
                marker = {"conversation_id": conversation_id, "state": "ready",
                          "staged_at": time.time(), "updated_at": time.time(),
                          "client_type": client_type, "client_ref": client_ref}
                temporary = marker_path.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_text(json.dumps(marker), encoding="utf-8")
                os.replace(temporary, marker_path)
        except Exception as exc:  # noqa: BLE001 — the next Stop can retry
            _log(runtime_path, f"arm failed sid={conversation_id}: {type(exc).__name__}")
    return {"conversation_id": conversation_id, "measured": measured,
            "threshold": threshold if not invalid else None, "rotate": rotate}


def _typed_prompt(event: dict) -> bool:
    prompt = str(event.get("prompt") or event.get("user_prompt") or "").lstrip()
    sources = event.get("prompt_sources")
    if isinstance(sources, list) and sources:
        return all(source == "user" for source in sources)
    return bool(prompt) and not prompt.startswith(("<task-notification", "<system-reminder",
                                                    "<command-name", "<local-command-"))


def fire(event: dict, runtime_path: Path) -> None:
    if os.environ.get("HIVE_SURFACE") != "terminal" or not _typed_prompt(event):
        return
    conversation_id = str(event.get("session_id") or event.get("thread_id") or "")
    if not conversation_id:
        return
    marker_path = rotation_marker_api.state_dir(runtime_path.parent) / (
        f"rotation-staged-{conversation_id}.json")
    try:
        marker = json.loads(marker_path.read_text())
    except (OSError, ValueError):
        return
    if not isinstance(marker, dict):
        return
    marker["state"] = "firing"
    marker["updated_at"] = time.time()
    marker_path.write_text(json.dumps(marker))
    comms_url = os.environ.get("COMMS_URL", "").rstrip("/")
    bearer = os.environ.get("COMMS_BEARER_TOKEN", "")
    try:
        response = _post(f"{comms_url}/sessions/fire-rotation", bearer, {
            "client_type": marker.get("client_type") or "",
            "client_ref": marker.get("client_ref") or "",
            "conversation_id": conversation_id,
            "prompt": str(event.get("prompt") or event.get("user_prompt") or ""),
        }, timeout=60)
    except Exception as exc:  # noqa: BLE001 — failed fire remains retryable
        response = {"rotated": False}
        _log(runtime_path, f"fire failed sid={conversation_id}: {type(exc).__name__}")
    if response.get("rotated"):
        rotation_marker_api.drop_markers(runtime_path.parent, conversation_id)
    else:
        marker["state"] = "ready"
        marker["updated_at"] = time.time()
        marker_path.write_text(json.dumps(marker))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rotation_hooks")
    parser.add_argument("action", choices=("check", "fire"))
    parser.add_argument("--harness", choices=("claude", "codex"), required=True)
    parser.add_argument("--runtime", default="")
    parser.add_argument("--app-root", default="")
    args = parser.parse_args(argv)
    if args.app_root:
        root = str(Path(args.app_root).resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
    runtime_path = _runtime_path(args.runtime or None)
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    try:
        if args.action == "check":
            check(event, args.harness, runtime_path)
        else:
            fire(event, runtime_path)
    except Exception as exc:  # noqa: BLE001 — hooks never poison a turn
        _log(runtime_path, f"hook failed code={type(exc).__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
