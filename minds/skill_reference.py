"""One harness-neutral copy of every skill and agent a mind runs.

A mind can switch harness mid-conversation, so its skills and agents have to
exist in every harness's form at once — Claude's markdown, Codex's SKILL.md
and agent TOML, dsh's skill directories and delegate tools. Keeping three hand
copies in step is the drift `skills_sync` already reports between two; so
each skill and agent has exactly one reference copy on the mind's own disk
(`minds/<name>/reference/{skills,agents}/`), and every harness copy is
*rendered* from it.

The reference frontmatter is `name`, `description`, an optional
`argument-hint`, an optional `agents` list naming the agents a skill
delegates to, an `excluded` list of harnesses the mind removed it from, and
an optional per-harness block:

    harness:
      claude: {model: sonnet, tools: Bash}
      codex: {model: gpt-5.6-terra, model_reasoning_effort: high}
      dsh: {whenToUse: ...}

A harness's block lands in that harness's copy verbatim and in no other, and
is read back verbatim on write-back — no allow-list stands between a field
and the harness that reads it. A harness with no model named runs the skill
on the conversation's model.

A harness is rendered only when the mind declares a home for it. Claude's
falls back to `~/.claude`; Codex's and dsh's never fall back, because
`~/.codex` on this workstation is another mind's live home, and a mind
writing into it is that mind's skills changing under it.

A dsh agent is not a file of its own. dsh's `subagent` tool cannot pick a
named preset — a child always joins its parent's composition — so a named
delegate is one `@deepseek-ai/dsh-tool-subagent` row whose `toolName` is the
agent's name and whose `persona` is its body. Every agent's row is rendered
into one overlay the mind owns, `$DSH_HOME/agents.patch.yml`, which the dsh
adapter hands the CLI as `--patch`; dsh's shipped presets and the profile's
own `cordis.patch.yml` are never edited. Each row is that agent's copy, and
its fingerprint is the row's, not the file's.

There are two independent implementations of this — this one for the
container minds and the edge repo's — and they never share code.

A rendered copy's fingerprint is recorded beside the reference
(`.rendered.json`), together with the fingerprint of the reference it was
rendered from. A copy whose fingerprint no longer matches is an in-place
edit, which is how a mind tunes a skill from inside whichever harness it is
running: its body and that harness's own fields are merged into the
reference, the other harnesses' fields kept, and the other copies are
regenerated. Nothing is ever overwritten that this machinery did not write:
an edit made against a reference that has since moved, differing edits in
two copies, or a copy that was there before any render are conflicts, which
`resolve` settles by naming the copy that wins. A recorded copy that has
disappeared — deleted by hand or archived by the curator — excludes that
harness rather than being put back.

Three things leave copies alone and tell the operator instead. A skill
naming an agent with no reference, or naming a harness's own spawning tool
(skills name agents by name, never by the tool one harness spawns them
with), is refused outright and no copy changes. A model named for a harness
the proxy no longer offers it to leaves that one copy as it was. Each is
notified once per distinct reason, not once per Stop hook, and counted as
sent only when the notifier says it went.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

PROJECT_DIR = Path(__file__).resolve().parents[1]

HARNESSES = ("claude", "codex", "dsh")
KIND_SKILL = "skill"
KIND_AGENT = "agent"
KINDS = (KIND_SKILL, KIND_AGENT)

SKILL_FILE = "SKILL.md"
RECORD_FILE = ".rendered.json"
LOCK_FILE = ".lock"
# The digest of a corrupt record file already reported, so a Stop hook
# firing every turn reports it once.
CORRUPT_MARK = ".rendered.corrupt"

# A skill is source, not a build artifact. Anything past this is a
# virtualenv or a node_modules that was never meant to travel.
MAX_SKILL_BYTES = 8 * 1024 * 1024
# What a skill builds or installs is not the skill: never fingerprinted,
# never copied into a reference.
_BUILT = frozenset({"__pycache__", "venv", ".venv", "node_modules", "site-packages"})

# The phrases by which one harness spawns a delegate. A skill that names one
# works under that harness and silently does nothing under the other two.
SPAWNING_PHRASES = (
    "Agent tool", "subagent_type", "spawn_agent", "Task tool",
    "subagent_fork", "subagent_codex", "subagent_claude_code",
)

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# dsh refuses to load a skill whose name is outside this grammar
# (`@deepseek-ai/dsh-skill`'s SKILL_NAME).
_DSH_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# A delegate's tool name: what the model calls, and an identifier in dsh's
# code mode, so snake case — the shape of every tool dsh ships.
_DSH_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Every tool dsh registers itself (each `defineTool` / `tools.register` name
# under deepseek-harness `packages/*/*/src`, plus the delegation tools the
# bundles configure and the name code mode reserves). A delegate named after
# one would shadow it.
DSH_CORE_TOOLS = frozenset({
    "ask_user_question", "bash", "cordis_define", "cordis_inspect_list",
    "cordis_inspect_query", "cordis_inspect_self", "cordis_run", "cordis_stop",
    "cordis_undefine", "create_goal", "get_goal", "glob", "grep",
    "interrupt_agent", "job_kill", "job_list", "job_output", "list_agents",
    "lsp", "pwsh", "ralph", "read", "read_image", "report", "schedule_create",
    "schedule_delete", "schedule_list", "send_message", "session_event_read",
    "session_event_search", "session_event_trace", "session_search",
    "session_trace", "skill", "str_replace_editor", "terminal_close",
    "terminal_list", "terminal_open", "terminal_read", "terminal_send",
    "terminal_signal", "todo_write", "update_goal", "web_fetch", "web_search",
    "write", "edit",
    "subagent", "subagent_fork", "subagent_codex", "subagent_claude_code",
    "run_code",
})

_DSH_SUBAGENT_ROW = "@deepseek-ai/dsh-tool-subagent"
#: The overlay every reference agent is rendered into, under `$DSH_HOME`.
DSH_AGENTS_OVERLAY = "agents.patch.yml"
# What a delegate row carries that is not the agent's own field.
_DSH_ROW_DERIVED = ("toolName", "persona")
_DSH_DEFAULT_PROVIDER = "spawn"

# Claude Code resolves these itself. They are harness syntax for "a model of
# this tier" or "the conversation's", not names the proxy lists.
_HARNESS_MODEL_ALIASES = {"claude": frozenset({"inherit", "opus", "sonnet", "haiku"})}

SHARED_FIELDS = ("name", "description", "argument-hint", "agents", "excluded")

# Which shared fields each harness's copy carries. Everything in the
# harness's own block is carried too, verbatim.
_SHARED_RENDERED: dict[tuple[str, str], tuple[str, ...]] = {
    (KIND_SKILL, "claude"): ("name", "description", "argument-hint"),
    (KIND_SKILL, "codex"): ("name", "description", "argument-hint"),
    (KIND_SKILL, "dsh"): ("name", "description"),
    (KIND_AGENT, "claude"): ("name", "description"),
    (KIND_AGENT, "codex"): ("name", "description"),
    # A delegate tool row has no field a description could ride in
    # (`@deepseek-ai/dsh-tool-subagent`'s config has none).
    (KIND_AGENT, "dsh"): (),
}

Catalog = Callable[[str], "Iterable[str] | None"]
# A notifier returns False (or raises) when the message did not go out.
Notifier = Callable[[str], "bool | None"]


class RenderError(ValueError):
    """A skill or agent that cannot be rendered as asked."""


class RenderRefused(RenderError):
    """The reference names a missing agent or a harness's spawning tool."""


class RenderConflict(RenderError):
    """An in-place edit that cannot be merged without losing another change."""


class HarnessUndeclared(RenderError):
    """The mind declares no home for this harness, so it is not rendered."""


class RecordsCorrupt(RenderError):
    """`.rendered.json` cannot be read; never treated as empty."""


@dataclass
class Reference:
    kind: str
    name: str
    fields: dict
    harness: dict
    body: str


@dataclass
class Outcome:
    """What one pass did, per item. The CLI prints this as JSON."""

    rendered: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rendered": self.rendered,
            "merged": self.merged,
            "adopted": self.adopted,
            "resolved": self.resolved,
            "excluded": self.excluded,
            "conflicts": self.conflicts,
            "refused": self.refused,
            "blocked": self.blocked,
            "skipped": self.skipped,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def normalize_harness(harness: str) -> str:
    """`claude_cli`, `codex_cli` and `dsh_cli` name the same three as bare names."""
    key = (harness or "").strip().lower()
    for name in HARNESSES:
        if key == name or key.startswith(name):
            return name
    raise RenderError(f"Unknown harness: {harness!r}")


_HOME_VARIABLES = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "dsh": "DSH_HOME"}


def undeclared_reason(harness: str) -> str | None:
    """Why this mind renders nothing for `harness`, or None when it does."""
    h = normalize_harness(harness)
    if h == "claude" or os.environ.get(_HOME_VARIABLES[h]):
        return None
    return f"no {h} home declared for this mind ({_HOME_VARIABLES[h]} is unset)"


def harness_home(harness: str) -> Path:
    """The config home a harness reads, from the environment at call time.

    Only Claude's has a default. Codex's and dsh's must be declared: the
    default `~/.codex` is a home another mind on the same machine runs from.
    """
    h = normalize_harness(harness)
    reason = undeclared_reason(h)
    if reason:
        raise HarnessUndeclared(reason)
    if h == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))
    return Path(os.environ[_HOME_VARIABLES[h]])


def copy_path(kind: str, name: str, harness: str) -> Path:
    """Where one harness's copy of a skill or agent lives."""
    h = normalize_harness(harness)
    home = harness_home(h)
    if kind == KIND_SKILL:
        return home / "skills" / name
    if h == "claude":
        return home / "agents" / f"{name}.md"
    if h == "codex":
        return home / "agents" / f"{name}.toml"
    # One overlay holds every agent's delegate row.
    return home / DSH_AGENTS_OVERLAY


def reference_root(mind_name: str | None = None) -> Path:
    name = mind_name or os.environ.get("MIND_NAME") or ""
    if not _NAME_RE.fullmatch(name):
        raise RenderError(f"Invalid mind name: {name!r}")
    return PROJECT_DIR / "minds" / name / "reference"


def reference_path(kind: str, name: str, mind_name: str | None = None) -> Path:
    root = reference_root(mind_name)
    if kind == KIND_SKILL:
        return root / "skills" / name
    return root / "agents" / f"{name}.md"


def _validate(name: str) -> str:
    if not _NAME_RE.fullmatch(name or ""):
        raise RenderError(f"Invalid name: {name!r}")
    return name


def reference_names(kind: str, mind_name: str | None = None) -> list[str]:
    root = reference_root(mind_name) / ("skills" if kind == KIND_SKILL else "agents")
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return []
    names = []
    for entry in entries:
        if kind == KIND_SKILL:
            if _NAME_RE.fullmatch(entry.name) and (entry / SKILL_FILE).is_file():
                names.append(entry.name)
        elif entry.suffix == ".md" and _NAME_RE.fullmatch(entry.stem):
            names.append(entry.stem)
    return sorted(names)


# ---------------------------------------------------------------------------
# Fingerprints and trees
# ---------------------------------------------------------------------------


def _is_built(name: str) -> bool:
    return name in _BUILT or name.endswith(".pyc")


def fingerprint(path: Path) -> str | None:
    """A hash over a file, or over every file in a directory, path and content.

    Symlinks are hashed by their link text and never followed: a skill
    carrying a symlink into a plugin directory changes when the link does,
    not when the plugin does. What a skill builds — bytecode, virtualenvs,
    node_modules — is not hashed, so running a skill never makes it look
    edited.
    """
    digest = hashlib.sha256()
    if path.is_symlink():
        digest.update(b"link\0" + os.readlink(path).encode())
        return digest.hexdigest()
    if path.is_file():
        digest.update(b"file\0")
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.is_dir():
        return None
    entries = []
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        here = Path(directory)
        dirnames[:] = sorted(d for d in dirnames if not _is_built(d))
        for name in list(dirnames):
            if (here / name).is_symlink():
                dirnames.remove(name)
                entries.append(here / name)
        entries.extend(here / f for f in filenames if not _is_built(f))
    for item in sorted(entries):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(b"\0")
        if item.is_symlink():
            digest.update(b"link\0" + os.readlink(item).encode())
        else:
            digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def tree_bytes(path: Path) -> int:
    """The size of a skill as it would travel: built directories excluded."""
    total = 0
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _is_built(d)]
        for name in filenames:
            item = Path(directory) / name
            if not _is_built(name) and not item.is_symlink():
                total += item.stat().st_size
    return total


def _guard_size(path: Path, label: str) -> None:
    if path.is_dir():
        size = tree_bytes(path)
        if size > MAX_SKILL_BYTES:
            raise RenderError(
                f"{label} is {size // (1024 * 1024)} MB — larger than a skill should be. "
                "Something built (a virtualenv, node_modules) is inside it."
            )


def _copytree(source: Path, target: Path) -> None:
    shutil.copytree(
        source, target, symlinks=True,
        ignore=lambda _dir, names: [n for n in names if _is_built(n)],
    )


def _aside(target: Path, tag: str) -> Path:
    """A path beside `target`, at the same depth, for staging or swapping."""
    target.parent.mkdir(parents=True, exist_ok=True)
    return target.parent / f".{target.name}.{tag}.{uuid.uuid4().hex[:8]}"


def _replace(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _swap_in(staged: Path, target: Path) -> None:
    """Put `staged` where `target` is, with no moment where neither exists.

    The old copy is renamed aside first and deleted last; a failed rename-in
    puts it back.
    """
    old = None
    if target.exists() or target.is_symlink():
        old = _aside(target, "old")
        os.rename(target, old)
    try:
        os.rename(staged, target)
    except BaseException:
        if old is not None:
            os.rename(old, target)
        raise
    if old is not None:
        _replace(old)


def _write_text(path: Path, text: str) -> None:
    staged = _aside(path, "incoming")
    try:
        staged.write_text(text, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(staged, path.stat().st_mode & 0o7777)
        os.replace(staged, path)
    except BaseException:
        with contextlib.suppress(OSError):
            staged.unlink()
        raise


# ---------------------------------------------------------------------------
# Frontmatter and TOML
# ---------------------------------------------------------------------------


class _Dumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, value: str):
    # Multi-line text as a literal block, so a persona or a long description
    # stays readable in the file a person edits.
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_Dumper.add_representer(str, _represent_str)


def _dump_yaml(data) -> str:
    return yaml.dump(
        data, Dumper=_Dumper, sort_keys=False, allow_unicode=True,
        default_flow_style=False, width=1_000_000,
    )


def split_frontmatter(text: str) -> tuple[dict, str]:
    """(frontmatter, body). Text with no frontmatter is all body."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 3)
    if end < 0:
        if text.endswith("\n---"):
            end = len(text) - 4
            body = ""
        else:
            return {}, text
    else:
        body = text[end + 5:]
    head = text[4:end] if end > 4 else ""
    try:
        data = yaml.safe_load(head) if head else {}
    except yaml.YAMLError:
        data = _lenient_frontmatter(head)
    if not isinstance(data, dict):
        raise RenderError("frontmatter is not a mapping")
    return data, body


def _lenient_frontmatter(head: str) -> dict:
    """`key: value` lines, the way Claude reads frontmatter YAML refuses.

    Claude accepts `argument-hint: [operation] [...]`, which is not YAML, and
    skills written for it carry exactly that. Refusing them would leave a
    working Claude skill impossible to adopt or merge.
    """
    data: dict = {}
    key = None
    for line in head.splitlines():
        if line[:1].isspace() and key is not None:
            data[key] = f"{data[key]} {line.strip()}".strip()
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise RenderError(f"frontmatter line is not `key: value`: {line!r}")
        key, value = key.strip(), value.strip()
        try:
            data[key] = yaml.safe_load(value) if value else ""
        except yaml.YAMLError:
            data[key] = value
    return data


def compose_frontmatter(data: dict, body: str) -> str:
    return "---\n" + _dump_yaml(data) + "---\n" + body


def _toml_string(value: str, multiline: bool = False) -> str:
    """A TOML string that reads back as exactly `value`.

    Multi-line text goes out as a literal block so `developer_instructions`
    stays editable in place; anything a literal block cannot hold falls back
    to a basic string, which JSON's escaping produces validly.
    """
    literal_ok = (
        multiline
        and "\n" in value
        and "'''" not in value
        and not any(c in value for c in "\r\x7f")
        and not any(ord(c) < 0x20 and c not in "\t\n" for c in value)
    )
    if literal_ok:
        return "'''\n" + value + "'''"
    return json.dumps(value, ensure_ascii=False)


_TOML_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(key: str) -> str:
    return key if _TOML_BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value, multiline: bool = False) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value, multiline)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items()
        ) + " }"
    raise RenderError(f"cannot write {value!r} as TOML")


# ---------------------------------------------------------------------------
# Reading and writing the reference
# ---------------------------------------------------------------------------


def _reference_from(kind: str, name: str, frontmatter: dict, body: str) -> Reference:
    fields = {k: v for k, v in frontmatter.items() if k != "harness"}
    harness = frontmatter.get("harness") or {}
    if not isinstance(harness, dict):
        raise RenderError(f"{kind} {name}: `harness` is not a mapping")
    fields["name"] = name
    blocks = {}
    for h, block in harness.items():
        if block is not None and not isinstance(block, dict):
            raise RenderError(f"{kind} {name}: `harness.{h}` is not a mapping")
        blocks[str(h)] = dict(block or {})
    return Reference(kind, name, fields, blocks, body)


def parse_reference(kind: str, name: str, text: str) -> Reference:
    frontmatter, body = split_frontmatter(text)
    return _reference_from(kind, name, frontmatter, body)


def load_reference(kind: str, name: str, mind_name: str | None = None) -> Reference | None:
    path = reference_path(kind, name, mind_name)
    text_path = path / SKILL_FILE if kind == KIND_SKILL else path
    try:
        text = text_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return parse_reference(kind, name, text)


def reference_text(ref: Reference) -> str:
    data = {k: v for k, v in ref.fields.items() if v not in (None, [], "") or k == "name"}
    harness = {h: v for h, v in ref.harness.items() if v}
    if harness:
        data["harness"] = harness
    return compose_frontmatter(data, ref.body)


def _excluded(ref: Reference) -> set[str]:
    value = ref.fields.get("excluded") or []
    return {str(v) for v in ([value] if isinstance(value, str) else value)}


def _with_excluded(ref: Reference, excluded: set[str]) -> Reference:
    fields = dict(ref.fields)
    if excluded:
        fields["excluded"] = sorted(excluded)
    else:
        fields.pop("excluded", None)
    return replace(ref, fields=fields)


def _write_reference(
    ref: Reference, copy_dir: Path | None, mind_name: str | None, text: str | None = None,
) -> None:
    """Replace the reference with `ref`, siblings from `copy_dir` for a skill.

    What a copy built (a venv, node_modules, bytecode) never enters the
    reference, and a copy past `MAX_SKILL_BYTES` is refused.
    """
    target = reference_path(ref.kind, ref.name, mind_name)
    text = reference_text(ref) if text is None else text
    if ref.kind == KIND_AGENT:
        _write_text(target, text)
        return
    sibling_source = copy_dir if copy_dir is not None else target
    if sibling_source.is_dir():
        _guard_size(sibling_source, f"{ref.kind} {ref.name}")
    staged = _aside(target, "incoming")
    try:
        if sibling_source.is_dir():
            _copytree(sibling_source, staged)
        else:
            staged.mkdir()
        (staged / SKILL_FILE).write_text(text, encoding="utf-8")
        _swap_in(staged, target)
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _frontmatter_for(ref: Reference, harness: str) -> dict:
    out = {k: ref.fields[k] for k in _SHARED_RENDERED[(ref.kind, harness)] if k in ref.fields}
    out.update(ref.harness.get(harness) or {})
    return out


def rendered_model(ref: Reference, harness: str) -> str | None:
    """The model this harness's copy names, if it names one."""
    model = (ref.harness.get(harness) or {}).get("model")
    return str(model) if model else None


def render_files(ref: Reference, harness: str) -> dict[str, str]:
    """One harness's copy as `{relative path: text}`; `""` is a file copy."""
    h = normalize_harness(harness)
    fm = _frontmatter_for(ref, h)
    if ref.kind == KIND_SKILL:
        return {SKILL_FILE: compose_frontmatter(fm, ref.body)}
    if h == "claude":
        return {"": compose_frontmatter(fm, ref.body)}
    if h == "codex":
        lines = [f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in fm.items()
                 if k != "developer_instructions"]
        lines.append(f"developer_instructions = {_toml_value(ref.body, multiline=True)}")
        return {"": "\n".join(lines) + "\n"}
    return {"": _dump_yaml(dsh_row(ref))}


def dsh_tool_name(name: str) -> str:
    """The delegate tool an agent is called by under dsh."""
    return re.sub(r"[^a-z0-9_]", "_", name.lower())


def _dsh_row_id(name: str) -> str:
    return f"agent-{name}"


def dsh_row(ref: Reference) -> dict:
    """One agent as a dsh delegate: a `tool-subagent` row named for it.

    `spawn` is the in-process provider every dsh bundle loads, and the one
    that honours a per-tool persona. Everything in the agent's dsh block is
    row config verbatim, except `model`, which is `agentOptions.model` — a
    model named there pins the child; none named runs it on the
    conversation's model.
    """
    block = dict(ref.harness.get("dsh") or {})
    model = block.pop("model", None)
    config: dict = {"provider": _DSH_DEFAULT_PROVIDER}
    config.update(block)
    if model:
        options = dict(config.get("agentOptions") or {})
        options["model"] = str(model)
        config["agentOptions"] = options
    config["toolName"] = dsh_tool_name(ref.name)
    config["persona"] = ref.body
    return {"id": _dsh_row_id(ref.name), "name": _DSH_SUBAGENT_ROW, "config": config}


def _dsh_collisions(mind_name: str | None) -> dict[str, list[str]]:
    """dsh tool names more than one reference agent maps to."""
    by_tool: dict[str, list[str]] = {}
    for name in reference_names(KIND_AGENT, mind_name):
        by_tool.setdefault(dsh_tool_name(name), []).append(name)
    return {tool: names for tool, names in by_tool.items() if len(names) > 1}


def _harness_refusal(ref: Reference, harness: str, mind_name: str | None) -> str | None:
    """Why this harness's copy may not be rendered, though the others may."""
    if harness != "dsh":
        return None
    if ref.kind == KIND_SKILL:
        if not _DSH_SKILL_NAME.fullmatch(ref.name):
            return (f"skill {ref.name}: dsh loads no skill by that name "
                    "(lowercase words joined by '-'); not rendered for dsh")
        return None
    tool = dsh_tool_name(ref.name)
    if not _DSH_TOOL_NAME.fullmatch(tool):
        return f"agent {ref.name}: makes no dsh tool name ({tool!r}); not rendered for dsh"
    if tool in DSH_CORE_TOOLS:
        return f"agent {ref.name}: would shadow dsh's own {tool!r} tool; not rendered for dsh"
    clash = _dsh_collisions(mind_name).get(tool)
    if clash:
        return (f"agent {ref.name}: agents {', '.join(clash)} all map to dsh tool "
                f"{tool!r}; not rendered for dsh")
    # dsh renders a persona as a strict template with no escape syntax: any
    # `{{` followed later by `}}` is a variable reference and fails the
    # child's first request; a lone `{{` is literal prose.
    opened = ref.body.find("{{")
    if opened >= 0 and "}}" in ref.body[opened + 2:]:
        return (
            f"agent {ref.name}: its body holds a {{{{...}}}} group, which dsh reads as "
            "a prompt variable and cannot escape; the dsh delegate was left as it was"
        )
    return None


def _agents_named(ref: Reference) -> list[str]:
    """Every agent a reference names, top level and inside harness blocks."""
    named: list[str] = []
    for source in [ref.fields, *ref.harness.values()]:
        value = source.get("agents") or []
        named.extend(str(a) for a in ([value] if isinstance(value, str) else value))
    return named


def refusal(ref: Reference, mind_name: str | None = None) -> str | None:
    """Why this reference may not be rendered anywhere, or None."""
    if ref.kind != KIND_SKILL:
        return None
    for phrase in SPAWNING_PHRASES:
        if phrase.lower() in ref.body.lower():
            return (
                f"skill {ref.name} names a harness spawning tool ({phrase!r}); "
                "name the agent instead"
            )
    known = set(reference_names(KIND_AGENT, mind_name))
    missing = sorted({a for a in _agents_named(ref) if a not in known})
    if missing:
        return f"skill {ref.name} names agent(s) with no reference copy: {', '.join(missing)}"
    return None


# ---------------------------------------------------------------------------
# The dsh overlay
# ---------------------------------------------------------------------------

_OVERLAY_HEADER = """\
# Rendered from this mind's reference agents by skill_reference: one dsh
# delegate tool per agent. Edit a row and the next check merges it into the
# reference; the dsh adapter loads this with --patch.
"""


def _overlay_rows(path: Path) -> dict[str, dict]:
    """The delegate rows in the overlay, by row id."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise RenderError(f"{path} cannot be read: {exc}") from exc
    rows: dict[str, dict] = {}
    for entry in data or []:
        if not isinstance(entry, dict):
            continue
        for row in entry.get("insert") or []:
            if isinstance(row, dict) and row.get("id"):
                rows[str(row["id"])] = row
    return rows


def _write_overlay(path: Path, rows: dict[str, dict]) -> None:
    ordered = [rows[key] for key in sorted(rows)]
    _write_text(path, _OVERLAY_HEADER + _dump_yaml([{"insert": ordered}] if ordered else []))


def _row_fingerprint(row: dict) -> str:
    digest = hashlib.sha256(b"row\0")
    digest.update(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str).encode())
    return digest.hexdigest()


def _is_overlay_copy(kind: str, harness: str) -> bool:
    return kind == KIND_AGENT and normalize_harness(harness) == "dsh"


def copy_fingerprint(kind: str, name: str, harness: str) -> str | None:
    """One copy's fingerprint: its file or directory, or its overlay row."""
    path = copy_path(kind, name, harness)
    if _is_overlay_copy(kind, harness):
        row = _overlay_rows(path).get(_dsh_row_id(name))
        return _row_fingerprint(row) if row is not None else None
    return fingerprint(path)


def _remove_copy(kind: str, name: str, harness: str) -> None:
    path = copy_path(kind, name, harness)
    if _is_overlay_copy(kind, harness):
        rows = _overlay_rows(path)
        if rows.pop(_dsh_row_id(name), None) is not None:
            _write_overlay(path, rows)
        return
    _replace(path)


# ---------------------------------------------------------------------------
# Reading a harness copy back into reference terms
# ---------------------------------------------------------------------------


def _read_copy(kind: str, name: str, harness: str) -> tuple[dict, str]:
    """(frontmatter-equivalent fields, body) of one harness's copy."""
    h = normalize_harness(harness)
    path = copy_path(kind, name, h)
    if kind == KIND_SKILL:
        return split_frontmatter((path / SKILL_FILE).read_text(encoding="utf-8"))
    if h == "claude":
        return split_frontmatter(path.read_text(encoding="utf-8"))
    if h == "codex":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        body = str(data.pop("developer_instructions", ""))
        return data, body
    row = _overlay_rows(path).get(_dsh_row_id(name)) or {}
    config = dict(row.get("config") or {})
    body = str(config.pop("persona", "") or "")
    for key in _DSH_ROW_DERIVED:
        config.pop(key, None)
    if config.get("provider") == _DSH_DEFAULT_PROVIDER:
        config.pop("provider")
    options = dict(config.pop("agentOptions", None) or {})
    if options.get("model"):
        config["model"] = options.pop("model")
    if options:
        config["agentOptions"] = options
    return config, body


def merged_reference(ref: Reference, harness: str) -> Reference:
    """`ref` with this harness's copy's body and own fields taken in.

    The shared fields this harness's copy carries are taken from the copy;
    shared fields it does not carry, and every other harness's block, are
    kept. Everything else in the copy is that harness's own block, verbatim.
    """
    h = normalize_harness(harness)
    copy_fields, body = _read_copy(ref.kind, ref.name, h)
    shared = _SHARED_RENDERED[(ref.kind, h)]
    fields = dict(ref.fields)
    for key in shared:
        if key == "name":
            continue
        if key in copy_fields:
            fields[key] = copy_fields[key]
        else:
            fields.pop(key, None)
    blocks = dict(ref.harness)
    blocks[h] = {k: v for k, v in copy_fields.items() if k not in shared}
    return Reference(ref.kind, ref.name, fields, blocks, body)


def _view(kind: str, name: str, harness: str) -> str:
    """A copy's read-back with its model left out.

    Recorded at render, so an edit that changes nothing but the model is
    recognisable later even after the reference has moved on.
    """
    fields, body = _read_copy(kind, name, harness)
    fields = {k: v for k, v in fields.items() if k != "model"}
    blob = json.dumps([fields, body], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _same_content(a: Reference, b: Reference) -> bool:
    """Two merge candidates that agree on everything harness-neutral."""
    neutral = [k for k in SHARED_FIELDS if k != "excluded"]
    return a.body == b.body and all(a.fields.get(k) == b.fields.get(k) for k in neutral)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _record_path(mind_name: str | None) -> Path:
    return reference_root(mind_name) / RECORD_FILE


def load_records(mind_name: str | None = None) -> dict:
    """The render record. A file that cannot be read is an error, not empty.

    Read as empty, a corrupt record turns every rendered copy into one this
    machinery never wrote — and those are never overwritten — or, worse,
    into nothing to compare against.
    """
    path = _record_path(mind_name)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = None
    data: object = {}
    if raw is not None:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecordsCorrupt(f"{path} cannot be read: {exc}") from exc
        if not isinstance(data, dict):
            raise RecordsCorrupt(f"{path} is not a JSON object")
    for kind in KINDS:
        if not isinstance(data.get(kind), dict):
            data[kind] = {}
    return data


def save_records(records: dict, mind_name: str | None = None) -> None:
    path = _record_path(mind_name)
    _write_text(path, json.dumps(records, indent=2, sort_keys=True))


@contextlib.contextmanager
def _locked(mind_name: str | None):
    """One pass at a time: a Stop hook and a mind start can overlap."""
    root = reference_root(mind_name)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / LOCK_FILE, "a+") as handle:
        try:
            import fcntl
        except ImportError:  # windows-task minds
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _send(notify: Notifier, message: str) -> bool:
    try:
        return notify(message) is not False
    except Exception:
        return False


def _notify_once(holder: dict, key: str, reason: str, notify: Notifier) -> None:
    """Tell the operator, unless this exact reason was already told.

    Recorded as told only once the notifier says it went: a reason marked
    sent after a failed send is a reason nobody ever hears.
    """
    if holder.get(key) == reason:
        return
    if _send(notify, reason):
        holder[key] = reason


def _load_records_or_report(notify: Notifier, mind_name: str | None) -> dict:
    """The records, or a refusal of the whole pass that is reported once."""
    try:
        return load_records(mind_name)
    except RecordsCorrupt as exc:
        root = reference_root(mind_name)
        digest = hashlib.sha256(_record_path(mind_name).read_bytes()).hexdigest()
        mark = root / CORRUPT_MARK
        try:
            told = mark.read_text(encoding="utf-8").strip() == digest
        except (OSError, UnicodeDecodeError):
            told = False
        if not told and _send(notify, f"skill render refused: {exc}; nothing was rendered"):
            with contextlib.suppress(OSError):
                mark.write_text(digest, encoding="utf-8")
        raise


# ---------------------------------------------------------------------------
# Rendering an item into every harness
# ---------------------------------------------------------------------------


def _offered(catalog: Catalog, harness: str, model: str) -> bool | None:
    """Whether the proxy offers `model` to `harness`; None when it cannot say.

    An empty listing is a proxy that could not answer, not one offering
    nothing: refusing every model on it would notify for every copy at once.
    """
    if model in _HARNESS_MODEL_ALIASES.get(harness, ()):
        return True
    try:
        listed = catalog(harness)
    except Exception:
        return None
    names = {str(n) for n in (listed or [])}
    if not names:
        return None
    return model in names


def _stage_copy(ref: Reference, harness: str, source: Path | None) -> Path:
    """Build one file or directory copy beside its target."""
    target = copy_path(ref.kind, ref.name, harness)
    staged = _aside(target, "incoming")
    files = render_files(ref, harness)
    if "" in files:
        staged.write_text(files[""], encoding="utf-8")
        return staged
    if source is not None and source.is_dir():
        # A skill is a directory: scripts and references travel with it.
        _guard_size(source, f"{ref.kind} {ref.name}")
        _copytree(source, staged)
    else:
        staged.mkdir()
    for relative, text in files.items():
        (staged / relative).write_text(text, encoding="utf-8")
    return staged


def _render_into(
    ref: Reference, item: dict, outcome: Outcome, *,
    catalog: Catalog, notify: Notifier, mind_name: str | None,
    only: Iterable[str] | None = None, force: Iterable[str] = (),
) -> None:
    """Render `ref` into every harness whose copy is not already it.

    `force` names harnesses whose existing copy is overwritten even if this
    machinery never wrote it — the explicit install and resolve paths.
    """
    label = f"{ref.kind} {ref.name}"
    source = reference_path(ref.kind, ref.name, mind_name)
    ref_hash = fingerprint(source)
    excluded = _excluded(ref)
    forced = set(force)
    wrote = False
    for h in HARNESSES:
        if only is not None and h not in only:
            continue
        if h in excluded:
            continue
        reason = undeclared_reason(h)
        if reason:
            outcome.skipped.append({"item": label, "harness": h, "reason": reason})
            continue
        entry = item.setdefault(h, {})
        refused = _harness_refusal(ref, h, mind_name)
        if refused:
            outcome.blocked.append({"item": label, "harness": h, "reason": refused})
            _notify_once(entry, "refused", refused, notify)
            continue
        entry.pop("refused", None)
        model = rendered_model(ref, h)
        if model is not None:
            offered = _offered(catalog, h, model)
            if offered is None:
                # The proxy could not be asked. Nothing is known to be wrong,
                # so nothing is said; the next pass asks again.
                continue
            if not offered:
                blocked = (
                    f"{label}: the proxy no longer offers {model} to {h}; "
                    f"the {h} copy was left as it was"
                )
                outcome.blocked.append({"item": label, "harness": h, "model": model})
                _notify_once(entry, "blocked", blocked, notify)
                continue
        entry.pop("blocked", None)

        current = copy_fingerprint(ref.kind, ref.name, h)
        recorded = entry.get("fingerprint")
        if recorded and current == recorded and entry.get("reference") == ref_hash:
            continue  # nothing moved: no staging at all

        if _is_overlay_copy(ref.kind, h):
            row = dsh_row(ref)
            new_hash = _row_fingerprint(row)
        else:
            staged = _stage_copy(ref, h, source if ref.kind == KIND_SKILL else None)
            new_hash = fingerprint(staged)

        try:
            if not recorded and current is not None and current != new_hash and h not in forced:
                # A copy this machinery never wrote. It may be a mind's own
                # tuning; overwriting it is how that tuning disappears.
                unmanaged = (
                    f"{label}: a {h} copy exists that was never rendered from the "
                    f"reference and differs from it; keep it with "
                    f"`skill_render.py resolve {ref.name} --kind {ref.kind} --from {h}`, "
                    f"or discard it by installing for {h}"
                )
                outcome.conflicts.append({"item": label, "harnesses": [h], "reason": unmanaged})
                _notify_once(entry, "unmanaged", unmanaged, notify)
                continue
            entry.pop("unmanaged", None)
            if new_hash != current:
                if _is_overlay_copy(ref.kind, h):
                    path = copy_path(ref.kind, ref.name, h)
                    rows = _overlay_rows(path)
                    rows[row["id"]] = row
                    _write_overlay(path, rows)
                else:
                    _swap_in(staged, copy_path(ref.kind, ref.name, h))
                wrote = True
            entry["fingerprint"] = new_hash
            entry["reference"] = ref_hash
            entry["view"] = _view(ref.kind, ref.name, h)
        finally:
            if not _is_overlay_copy(ref.kind, h) and staged.exists():
                _replace(staged)
    if wrote:
        outcome.rendered.append(label)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def _resolve_hint(kind: str, name: str, harnesses: Iterable[str]) -> str:
    choices = " or ".join(
        f"`skill_render.py resolve {name} --kind {kind} --from {h}`" for h in harnesses
    )
    return f"resolve with {choices}"


def _sync_item(
    kind: str, name: str, records: dict, outcome: Outcome, *,
    catalog: Catalog, notify: Notifier, mind_name: str | None,
    raise_conflict: bool = False,
) -> None:
    label = f"{kind} {name}"
    ref = load_reference(kind, name, mind_name)
    if ref is None:
        return
    item = records[kind].setdefault(name, {})
    ref_hash = fingerprint(reference_path(kind, name, mind_name))
    excluded = _excluded(ref)
    live = [h for h in HARNESSES if h not in excluded and undeclared_reason(h) is None]

    gone, edited = [], []
    for h in live:
        entry = item.get(h) or {}
        if not entry.get("fingerprint"):
            continue
        current = copy_fingerprint(kind, name, h)
        if current is None:
            gone.append(h)
        elif current != entry["fingerprint"]:
            edited.append(h)

    # A blocked copy whose only change is its model: the operator answering
    # the notification. Taken as that one field, whatever the reference did.
    model_only = [
        h for h in edited
        if item[h].get("blocked") and _view(kind, name, h) == item[h].get("view")
    ]
    edited = [h for h in edited if h not in model_only]

    reason = None
    candidates = [merged_reference(ref, h) for h in edited]
    if len(candidates) > 1 and not all(_same_content(candidates[0], c) for c in candidates[1:]):
        reason = (
            f"{label} was edited differently under {' and '.join(edited)}; nothing was "
            f"merged — {_resolve_hint(kind, name, edited)}"
        )
    elif any(item[h].get("reference") != ref_hash for h in edited):
        moved = [h for h in edited if item[h].get("reference") != ref_hash]
        reason = (
            f"{label}: the {' and '.join(moved)} copy was edited, but the reference changed "
            f"since it was rendered; neither was overwritten — "
            f"{_resolve_hint(kind, name, moved)}"
        )
    if reason:
        outcome.conflicts.append({"item": label, "harnesses": edited, "reason": reason})
        _notify_once(item, "conflict", reason, notify)
        if raise_conflict:
            raise RenderConflict(reason)
        return
    item.pop("conflict", None)

    candidate = ref
    for h in edited:
        candidate = merged_reference(candidate, h)
    for h in model_only:
        fields, _ = _read_copy(kind, name, h)
        blocks = dict(candidate.harness)
        block = dict(blocks.get(h) or {})
        if fields.get("model"):
            block["model"] = fields["model"]
        else:
            block.pop("model", None)
        blocks[h] = block
        candidate = replace(candidate, harness=blocks)
    if gone:
        candidate = _with_excluded(candidate, _excluded(candidate) | set(gone))

    if edited or model_only or gone:
        refused = refusal(candidate, mind_name)
        if refused:
            # An edit that would be refused is not merged: the reference
            # keeps what it was, and the copy stays as edited.
            outcome.refused.append({"item": label, "reason": refused})
            _notify_once(item, "refused", refused, notify)
            return
        copy_dir = None
        if kind == KIND_SKILL and edited:
            copy_dir = copy_path(kind, name, edited[0])
        _write_reference(candidate, copy_dir, mind_name)
        ref = candidate
        ref_hash = fingerprint(reference_path(kind, name, mind_name))
        for h in edited + model_only:
            item[h]["fingerprint"] = copy_fingerprint(kind, name, h)
            item[h]["reference"] = ref_hash
            item[h]["view"] = _view(kind, name, h)
        for h in model_only:
            # Its body is still from before the block; render it afresh.
            item[h]["reference"] = None
        for h in edited:
            outcome.merged.append(f"{label} ({h})")
        for h in model_only:
            outcome.merged.append(f"{label} ({h} model)")
        for h in gone:
            item.pop(h, None)
            outcome.excluded.append({"item": label, "harness": h})

    refused = refusal(ref, mind_name)
    if refused:
        outcome.refused.append({"item": label, "reason": refused})
        _notify_once(item, "refused", refused, notify)
        return
    item.pop("refused", None)
    _render_into(ref, item, outcome, catalog=catalog, notify=notify, mind_name=mind_name)


def _isolated(
    kind: str, name: str, records: dict, outcome: Outcome, notify: Notifier,
    mind_name: str | None, call: Callable[[], None],
) -> None:
    """Run one item's work; a failure is that item's, reported, never the pass's.

    The records are saved after every item, so a later item failing cannot
    leave an earlier item's fresh copies unrecorded — which the next pass
    would read as copies nobody rendered.
    """
    try:
        call()
    except RenderConflict:
        raise
    except Exception as exc:  # noqa: BLE001
        label = f"{kind} {name}"
        reason = f"{label} could not be rendered: {exc}"
        outcome.errors.append({"item": label, "reason": str(exc)})
        _notify_once(records[kind].setdefault(name, {}), "error", reason, notify)
    else:
        records[kind].get(name, {}).pop("error", None)
    save_records(records, mind_name)


def check(
    *, catalog: Catalog, notify: Notifier, mind_name: str | None = None,
    kinds: Iterable[str] = KINDS, names: Iterable[str] | None = None,
) -> Outcome:
    """Merge every in-place edit, then render every reference everywhere.

    Agents go first: a skill naming an agent is only renderable once that
    agent's reference exists, and a merged agent edit must be visible to the
    skills checked after it. A mind with no references has nothing to do
    and nothing is created on its disk.
    """
    outcome = Outcome()
    if not reference_root(mind_name).is_dir():
        return outcome
    wanted = set(names) if names is not None else None
    with _locked(mind_name):
        records = _load_records_or_report(notify, mind_name)
        for kind in sorted(set(kinds), key=lambda k: 0 if k == KIND_AGENT else 1):
            for name in reference_names(kind, mind_name):
                if wanted is not None and name not in wanted:
                    continue
                def one(k=kind, n=name):
                    _sync_item(k, n, records, outcome, catalog=catalog, notify=notify,
                               mind_name=mind_name)

                _isolated(kind, name, records, outcome, notify, mind_name, one)
    return outcome


def merge_copy(
    kind: str, name: str, *, catalog: Catalog, notify: Notifier,
    mind_name: str | None = None,
) -> Outcome:
    """The same pass for one item, raising on a conflict instead of reporting it."""
    _validate(name)
    outcome = Outcome()
    with _locked(mind_name):
        records = _load_records_or_report(notify, mind_name)
        try:
            _sync_item(kind, name, records, outcome, catalog=catalog, notify=notify,
                       mind_name=mind_name, raise_conflict=True)
        finally:
            save_records(records, mind_name)
    if outcome.refused:
        raise RenderRefused(outcome.refused[0]["reason"])
    return outcome


def install(
    kind: str, name: str, harness: str, text: str, source_dir: Path | None, *,
    catalog: Catalog, notify: Notifier, mind_name: str | None = None,
) -> Outcome:
    """Make `text` the reference and render it, overwriting only `harness`.

    Validated before anything is written: a refused install leaves the
    reference, the records and every copy as they were. The named harness's
    copy is replaced whatever it held, since naming it is the explicit
    overwrite; every other copy goes through the ordinary pass, so a tuned
    copy elsewhere is a conflict, not a casualty. Installing for a harness
    the mind had excluded puts it back.
    """
    _validate(name)
    h = normalize_harness(harness)
    harness_home(h)
    new = parse_reference(kind, name, text)
    refused = refusal(new, mind_name)
    if refused:
        _send(notify, refused)
        raise RenderRefused(refused)
    if source_dir is not None:
        _guard_size(source_dir, f"{kind} {name}")
    outcome = Outcome()
    with _locked(mind_name):
        records = _load_records_or_report(notify, mind_name)
        old = load_reference(kind, name, mind_name)
        excluded = (_excluded(old) if old else set()) - {h}
        # Written as shipped unless the mind's own exclusions must ride along,
        # so a fresh install compares equal to the repo's copy.
        verbatim = text if excluded == _excluded(new) else None
        new = _with_excluded(new, excluded)
        _write_reference(new, source_dir, mind_name, verbatim)
        item = records[kind].setdefault(name, {})
        item.pop(h, None)
        try:
            _render_into(new, item, outcome, catalog=catalog, notify=notify,
                         mind_name=mind_name, only={h}, force={h})
            _sync_item(kind, name, records, outcome, catalog=catalog, notify=notify,
                       mind_name=mind_name)
        finally:
            save_records(records, mind_name)
    return outcome


def adopt(
    kind: str, name: str, harness: str, *, catalog: Catalog, notify: Notifier,
    mind_name: str | None = None,
) -> Outcome:
    """Make a reference out of a copy that has none, then render the others.

    Explicit rather than part of `check`: a mind's config directory holds
    skills nobody has decided should follow it to every harness.
    """
    _validate(name)
    h = normalize_harness(harness)
    if load_reference(kind, name, mind_name) is not None:
        raise RenderError(f"{kind} {name} already has a reference copy")
    source = copy_path(kind, name, h)
    if copy_fingerprint(kind, name, h) is None:
        raise RenderError(f"No {h} copy of {kind} {name} to adopt")
    if kind == KIND_SKILL:
        _guard_size(source, f"{kind} {name}")
    empty = Reference(kind, name, {"name": name}, {}, "")
    ref = merged_reference(empty, h)
    refused = refusal(ref, mind_name)
    if refused:
        _send(notify, refused)
        raise RenderRefused(refused)
    outcome = Outcome()
    with _locked(mind_name):
        records = _load_records_or_report(notify, mind_name)
        _write_reference(ref, source if kind == KIND_SKILL else None, mind_name)
        item = records[kind].setdefault(name, {})
        item[h] = {
            "fingerprint": copy_fingerprint(kind, name, h),
            "reference": fingerprint(reference_path(kind, name, mind_name)),
            "view": _view(kind, name, h),
        }
        outcome.adopted.append(f"{kind} {name} ({h})")
        try:
            _render_into(ref, item, outcome, catalog=catalog, notify=notify, mind_name=mind_name)
        finally:
            save_records(records, mind_name)
    return outcome


def resolve(
    kind: str, name: str, harness: str, *, catalog: Catalog, notify: Notifier,
    mind_name: str | None = None,
) -> Outcome:
    """Settle a conflict: this harness's copy wins and becomes the reference.

    Its body and own fields are merged over the reference as an edit would
    be, and every other copy is regenerated from the result, overwritten
    whatever it held — naming the winner is the decision the conflict asked
    for.
    """
    _validate(name)
    h = normalize_harness(harness)
    if copy_fingerprint(kind, name, h) is None:
        raise RenderError(f"No {h} copy of {kind} {name} to resolve from")
    ref = load_reference(kind, name, mind_name) or Reference(kind, name, {"name": name}, {}, "")
    candidate = _with_excluded(merged_reference(ref, h), _excluded(ref) - {h})
    refused = refusal(candidate, mind_name)
    if refused:
        _send(notify, refused)
        raise RenderRefused(refused)
    source = copy_path(kind, name, h)
    if kind == KIND_SKILL:
        _guard_size(source, f"{kind} {name}")
    outcome = Outcome()
    with _locked(mind_name):
        records = _load_records_or_report(notify, mind_name)
        _write_reference(candidate, source if kind == KIND_SKILL else None, mind_name)
        item = records[kind][name] = {}
        item[h] = {
            "fingerprint": copy_fingerprint(kind, name, h),
            "reference": fingerprint(reference_path(kind, name, mind_name)),
            "view": _view(kind, name, h),
        }
        outcome.resolved.append(f"{kind} {name} ({h})")
        try:
            _render_into(candidate, item, outcome, catalog=catalog, notify=notify,
                         mind_name=mind_name, force=set(HARNESSES))
        finally:
            save_records(records, mind_name)
    return outcome


def remove(kind: str, name: str, mind_name: str | None = None) -> None:
    """Drop the reference, every copy rendered from it, and its record."""
    _validate(name)
    with _locked(mind_name):
        records = load_records(mind_name)
        item = records[kind].pop(name, {})
        for h in HARNESSES:
            if undeclared_reason(h) is None and (item.get(h) or {}).get("fingerprint"):
                _remove_copy(kind, name, h)
        _replace(reference_path(kind, name, mind_name))
        save_records(records, mind_name)


def forget_record(kind: str, name: str, mind_name: str | None = None) -> None:
    """Forget what was rendered for an item."""
    _validate(name)
    with _locked(mind_name):
        records = load_records(mind_name)
        records[kind].pop(name, None)
        save_records(records, mind_name)


def copy_status(kind: str, name: str, harness: str, mind_name: str | None = None) -> str:
    """`rendered`, `edited`, `stale`, `missing`, `unmanaged` or `excluded`."""
    h = normalize_harness(harness)
    ref = load_reference(kind, name, mind_name)
    if ref is not None and h in _excluded(ref):
        return "excluded"
    current = copy_fingerprint(kind, name, h)
    entry = (load_records(mind_name)[kind].get(name) or {}).get(h) or {}
    if current is None:
        return "missing"
    if not entry.get("fingerprint"):
        return "unmanaged"
    if current != entry["fingerprint"]:
        return "edited"
    if entry.get("reference") != fingerprint(reference_path(kind, name, mind_name)):
        return "stale"
    return "rendered"


# ---------------------------------------------------------------------------
# Production transports
# ---------------------------------------------------------------------------


def proxy_catalog(mind_name: str | None = None) -> Catalog:
    """The mind's own `/models?harness=` listing, as model names per harness."""
    import asyncio
    import inspect

    name = mind_name or os.environ.get("MIND_NAME") or ""
    runtime_path = PROJECT_DIR / "minds" / name / "runtime.yaml"

    def catalog(harness: str) -> list[str] | None:
        h = normalize_harness(harness)
        try:
            from minds import models_api
            from minds.runtime_api import load_runtime
        except ImportError:
            return None
        if "harness" in inspect.signature(models_api.build_catalog).parameters:
            rows = asyncio.run(models_api.build_catalog(runtime_path, harness=h))
        else:
            # A listing that cannot be asked per harness answers only for the
            # mind's own; any other harness is a question it cannot answer.
            own = str(load_runtime(runtime_path).get("harness") or "")
            if normalize_harness(own) != h:
                return None
            rows = asyncio.run(models_api.build_catalog(runtime_path))
        return [str(row.get("name")) for row in rows if row.get("name")] or None

    return catalog


def _mind_python(project_dir: Path) -> str:
    """The mind's own interpreter: the one with its notify dependencies."""
    root = Path(os.environ.get("HIVE_PROJECT_DIR") or project_dir)
    for candidate in (root / ".venv" / "bin" / "python", root / ".venv" / "Scripts" / "python.exe"):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def telegram_notifier(project_dir: Path | None = None) -> Notifier:
    """The mind's own notify tool, on the Telegram channel.

    Returns whether the message went, which is what decides whether the
    reason is recorded as told.
    """
    root = project_dir or PROJECT_DIR
    tool = root / "tools" / "stateless" / "notify" / "notify.py"
    mind = os.environ.get("MIND_NAME") or "mind"

    def notify(message: str) -> bool:
        try:
            done = subprocess.run(
                [_mind_python(root), str(tool), "send", "--message", f"[{mind}] {message}",
                 "--channels", "telegram"],
                capture_output=True, timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0

    return notify
