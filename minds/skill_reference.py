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
delegates to, and an optional per-harness block:

    harness:
      claude: {model: sonnet, tools: Bash}
      codex: {model: gpt-5.6-terra, model_reasoning_effort: high}
      dsh: {whenToUse: ...}

A harness's block lands only in that harness's copy, and each copy carries
only the frontmatter its harness reads. A harness with no model named runs
the skill on the conversation's model.

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
regenerated. An edit made against a reference that has since moved is a
conflict and is refused rather than merged over the newer reference.

Three things leave copies alone and tell the operator instead. A skill
naming an agent with no reference, or naming a harness's own spawning tool
(skills name agents by name, never by the tool one harness spawns them
with), is refused outright and no copy changes. A model named for a harness
the proxy no longer offers it to leaves that one copy as it was. Each is
notified once per distinct reason, not once per Stop hook.
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
import tempfile
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
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

# The phrases by which one harness spawns a delegate. A skill that names one
# works under that harness and silently does nothing under the other two.
SPAWNING_PHRASES = ("Agent tool", "subagent_type", "spawn_agent", "Task tool")

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# dsh refuses to load a skill whose name is outside this grammar
# (`@deepseek-ai/dsh-skill`'s SKILL_NAME).
_DSH_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# A delegate's tool name: what the model calls, and an identifier in dsh's
# code mode, so snake case — the shape of every tool dsh ships
# (`subagent_fork`, `subagent_claude_code`).
_DSH_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Tool names a delegate must not shadow: dsh's own delegation and follow-up
# tools, and the name code mode reserves.
_DSH_RESERVED_TOOLS = frozenset({
    "run_code", "subagent", "subagent_fork", "subagent_codex",
    "subagent_claude_code", "send_message", "report", "list_agents",
    "job_output", "job_kill",
})

_DSH_SUBAGENT_ROW = "@deepseek-ai/dsh-tool-subagent"
#: The overlay every reference agent is rendered into, under `$DSH_HOME`.
DSH_AGENTS_OVERLAY = "agents.patch.yml"

# Claude Code resolves these itself. They are harness syntax for "a model of
# this tier" or "the conversation's", not names the proxy lists.
_HARNESS_MODEL_ALIASES = {"claude": frozenset({"inherit", "opus", "sonnet", "haiku"})}

SHARED_FIELDS = ("name", "description", "argument-hint", "agents")

# Which shared fields each harness's copy carries.
_SHARED_RENDERED: dict[tuple[str, str], tuple[str, ...]] = {
    (KIND_SKILL, "claude"): ("name", "description", "argument-hint"),
    (KIND_SKILL, "codex"): ("name", "description", "argument-hint"),
    (KIND_SKILL, "dsh"): ("name", "description"),
    (KIND_AGENT, "claude"): ("name", "description"),
    (KIND_AGENT, "codex"): ("name", "description"),
    # A delegate tool row has no field a description could ride in.
    (KIND_AGENT, "dsh"): (),
}

# Which of a harness's own fields its copy carries. None means whatever the
# block holds: Claude reads an open vocabulary and ignores what it does not
# know, while Codex and dsh are closed — a key they do not read stays in the
# reference and never reaches their copy.
_OWN_RENDERED: dict[tuple[str, str], tuple[str, ...] | None] = {
    (KIND_SKILL, "claude"): None,
    (KIND_SKILL, "codex"): (),
    (KIND_SKILL, "dsh"): ("whenToUse", "disable-model-invocation", "user-invocable", "metadata"),
    (KIND_AGENT, "claude"): None,
    (KIND_AGENT, "codex"): ("model", "model_reasoning_effort"),
    (KIND_AGENT, "dsh"): ("model",),
}

Catalog = Callable[[str], "Iterable[str] | None"]
Notifier = Callable[[str], None]


class RenderError(ValueError):
    """A skill or agent that cannot be rendered as asked."""


class RenderRefused(RenderError):
    """The reference names a missing agent or a harness's spawning tool."""


class RenderConflict(RenderError):
    """An in-place edit that cannot be merged without losing another change."""


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
    conflicts: list[dict] = field(default_factory=list)
    refused: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "rendered": self.rendered,
            "merged": self.merged,
            "adopted": self.adopted,
            "conflicts": self.conflicts,
            "refused": self.refused,
            "blocked": self.blocked,
            "skipped": self.skipped,
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


def harness_home(harness: str) -> Path:
    """The config home a harness reads, from the environment at call time."""
    name = normalize_harness(harness)
    if name == "codex":
        home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    elif name == "dsh":
        # dsh's own default (`@deepseek-ai/dsh-home-paths`).
        home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    else:
        home = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(home)


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
# Fingerprints
# ---------------------------------------------------------------------------


def fingerprint(path: Path) -> str | None:
    """A hash over a file, or over every file in a directory, path and content."""
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(b"file\0")
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.is_dir():
        return None
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(item.relative_to(path)).encode())
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Frontmatter
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


def _toml_string(value: str) -> str:
    """A TOML string that reads back as exactly `value`.

    Multi-line text goes out as a literal block so `developer_instructions`
    stays editable in place; anything a literal block cannot hold falls back
    to a basic string, which JSON's escaping produces validly.
    """
    literal_ok = (
        "\n" in value
        and "'''" not in value
        and not any(c in value for c in "\r\x7f")
        and not any(ord(c) < 0x20 and c not in "\t\n" for c in value)
    )
    if literal_ok:
        return "'''\n" + value + "'''"
    return json.dumps(value, ensure_ascii=False)


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


def load_reference(kind: str, name: str, mind_name: str | None = None) -> Reference | None:
    path = reference_path(kind, name, mind_name)
    text_path = path / SKILL_FILE if kind == KIND_SKILL else path
    try:
        text = text_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    frontmatter, body = split_frontmatter(text)
    return _reference_from(kind, name, frontmatter, body)


def reference_text(ref: Reference) -> str:
    data = dict(ref.fields)
    harness = {h: v for h, v in ref.harness.items() if v}
    if harness:
        data["harness"] = harness
    return compose_frontmatter(data, ref.body)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _own_fields(ref: Reference, harness: str) -> dict:
    own = ref.harness.get(harness) or {}
    vocab = _OWN_RENDERED[(ref.kind, harness)]
    if vocab is None:
        return dict(own)
    return {k: own[k] for k in vocab if k in own}


def _frontmatter_for(ref: Reference, harness: str) -> dict:
    out = {k: ref.fields[k] for k in _SHARED_RENDERED[(ref.kind, harness)] if k in ref.fields}
    out.update(_own_fields(ref, harness))
    return out


def rendered_model(ref: Reference, harness: str) -> str | None:
    """The model this harness's copy names, if its copy names one at all."""
    model = _own_fields(ref, harness).get("model")
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
        lines = [f"{key} = {_toml_string(str(fm[key]))}" for key in
                 ("name", "description", "model", "model_reasoning_effort") if key in fm]
        lines.append(f"developer_instructions = {_toml_string(ref.body)}")
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
    that honours a per-tool persona. A model named in the agent's dsh block
    pins the child; none named runs it on the conversation's model.
    """
    config: dict = {
        "provider": "spawn",
        "toolName": dsh_tool_name(ref.name),
        "persona": ref.body,
    }
    model = _own_fields(ref, "dsh").get("model")
    if model:
        config["agentOptions"] = {"model": str(model)}
    return {"id": _dsh_row_id(ref.name), "name": _DSH_SUBAGENT_ROW, "config": config}


def _dsh_template_refusal(ref: Reference) -> str | None:
    """dsh renders a persona as a strict template with no escape syntax.

    Any `{{` followed later by `}}` is read as a variable reference and fails
    the child's first request; a lone `{{` is literal prose.
    """
    opened = ref.body.find("{{")
    if opened >= 0 and "}}" in ref.body[opened + 2:]:
        return (
            f"agent {ref.name}: its body holds a {{{{...}}}} group, which dsh reads as "
            "a prompt variable and cannot escape; the dsh delegate was left as it was"
        )
    return None


# ---------------------------------------------------------------------------
# The dsh overlay
# ---------------------------------------------------------------------------

_OVERLAY_HEADER = """\
# Rendered from this mind's reference agents by skill_reference: one dsh
# delegate tool per agent. Edit a row's persona or model and the next check
# merges it into the reference; the dsh adapter loads this with --patch.
"""


def _overlay_rows(path: Path) -> dict[str, dict]:
    """The delegate rows in the overlay, by row id."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as exc:
        raise RenderError(f"{path} is not valid YAML: {exc}") from exc
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
    text = _OVERLAY_HEADER + _dump_yaml([{"insert": ordered}] if ordered else [])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _row_fingerprint(row: dict) -> str:
    digest = hashlib.sha256(b"row\0")
    digest.update(json.dumps(row, sort_keys=True, ensure_ascii=False).encode())
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


def _skip_reason(ref: Reference, harness: str) -> str | None:
    """A name this harness cannot load at all — skipped, not refused."""
    if harness != "dsh":
        return None
    if ref.kind == KIND_SKILL and not _DSH_SKILL_NAME.fullmatch(ref.name):
        return f"dsh loads no skill named {ref.name!r} (lowercase words joined by '-')"
    if ref.kind == KIND_AGENT:
        tool = dsh_tool_name(ref.name)
        if not _DSH_TOOL_NAME.fullmatch(tool):
            return f"agent {ref.name!r} makes no dsh tool name ({tool!r})"
        if tool in _DSH_RESERVED_TOOLS:
            return f"agent {ref.name!r} would shadow dsh's own {tool!r} tool"
    return None


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
    agents = ref.fields.get("agents") or []
    if isinstance(agents, str):
        agents = [agents]
    known = set(reference_names(KIND_AGENT, mind_name))
    missing = [str(a) for a in agents if str(a) not in known]
    if missing:
        return f"skill {ref.name} names agent(s) with no reference copy: {', '.join(missing)}"
    return None


def _staging_for(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{target.name}.incoming.", dir=target.parent))


def _replace(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _stage(ref: Reference, harness: str, source: Path | None) -> tuple[Path, Path]:
    """Build the copy in a staging directory; (staging dir, staged copy)."""
    target = copy_path(ref.kind, ref.name, harness)
    staging = _staging_for(target)
    staged = staging / target.name
    files = render_files(ref, harness)
    if "" in files:
        staged.write_text(files[""], encoding="utf-8")
        return staging, staged
    if source is not None and source.is_dir():
        # A skill is a directory: scripts and references travel with it.
        # Symlinks stay symlinks, as `skills_sync` copies them.
        shutil.copytree(source, staged, symlinks=True)
    else:
        staged.mkdir()
    for relative, text in files.items():
        (staged / relative).write_text(text, encoding="utf-8")
    return staging, staged


def _write_staged(staged: Path, target: Path) -> None:
    _replace(target)
    staged.rename(target)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _record_path(mind_name: str | None) -> Path:
    return reference_root(mind_name) / RECORD_FILE


def load_records(mind_name: str | None = None) -> dict:
    try:
        data = json.loads(_record_path(mind_name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    for kind in KINDS:
        if not isinstance(data.get(kind), dict):
            data[kind] = {}
    return data


def save_records(records: dict, mind_name: str | None = None) -> None:
    path = _record_path(mind_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".rendered.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked(mind_name: str | None):
    """One pass at a time: a Stop hook and a mind start can overlap."""
    root = reference_root(mind_name)
    root.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # windows-task minds: one process per mind anyway
        yield
        return
    with open(root / LOCK_FILE, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _notify_once(item: dict, key: str, reason: str, notify: Notifier) -> None:
    """Tell the operator, unless this exact reason was already told."""
    if item.get(key) == reason:
        return
    item[key] = reason
    try:
        notify(reason)
    except Exception:
        # A notification is best effort; the record still says why.
        pass


# ---------------------------------------------------------------------------
# Rendering an item into every harness
# ---------------------------------------------------------------------------


def _offered(catalog: Catalog, harness: str, model: str) -> bool | None:
    """Whether the proxy offers `model` to `harness`; None when it cannot say."""
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


def _render_into(
    ref: Reference, item: dict, outcome: Outcome, *,
    catalog: Catalog, notify: Notifier, mind_name: str | None,
) -> None:
    """Render `ref` into every harness whose copy is not already it."""
    label = f"{ref.kind} {ref.name}"
    source = reference_path(ref.kind, ref.name, mind_name)
    ref_hash = fingerprint(source)
    wrote = False
    for h in HARNESSES:
        entry = item.setdefault(h, {})
        skip = _skip_reason(ref, h)
        if skip:
            if entry.get("skipped") != skip:
                outcome.skipped.append({"item": label, "harness": h, "reason": skip})
            entry["skipped"] = skip
            continue
        entry.pop("skipped", None)
        model = rendered_model(ref, h)
        if model is not None:
            offered = _offered(catalog, h, model)
            if offered is None:
                # The proxy could not be asked. Nothing is known to be wrong,
                # so nothing is said; the next pass asks again.
                continue
            if not offered:
                reason = (
                    f"{label}: the proxy no longer offers {model} to {h}; "
                    f"the {h} copy was left as it was"
                )
                outcome.blocked.append({"item": label, "harness": h, "model": model})
                _notify_once(entry, "blocked", reason, notify)
                continue
        entry.pop("blocked", None)
        if _is_overlay_copy(ref.kind, h):
            template = _dsh_template_refusal(ref)
            if template:
                outcome.blocked.append({"item": label, "harness": h, "reason": template})
                _notify_once(entry, "refused", template, notify)
                continue
            entry.pop("refused", None)
            row = dsh_row(ref)
            new_hash = _row_fingerprint(row)
            if new_hash != copy_fingerprint(ref.kind, ref.name, h):
                path = copy_path(ref.kind, ref.name, h)
                rows = _overlay_rows(path)
                rows[row["id"]] = row
                _write_overlay(path, rows)
                wrote = True
            entry["fingerprint"] = new_hash
            entry["reference"] = ref_hash
            continue
        target = copy_path(ref.kind, ref.name, h)
        staging, staged = _stage(ref, h, source if ref.kind == KIND_SKILL else None)
        try:
            new_hash = fingerprint(staged)
            if new_hash != fingerprint(target):
                _write_staged(staged, target)
                wrote = True
            entry["fingerprint"] = new_hash
            entry["reference"] = ref_hash
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    if wrote:
        outcome.rendered.append(label)


def _write_reference(ref: Reference, copy_dir: Path | None, mind_name: str | None) -> None:
    """Replace the reference with `ref`, siblings from `copy_dir` for a skill."""
    target = reference_path(ref.kind, ref.name, mind_name)
    text = reference_text(ref)
    if ref.kind == KIND_AGENT:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, target)
        return
    staging = _staging_for(target)
    staged = staging / target.name
    try:
        sibling_source = copy_dir if copy_dir is not None else target
        if sibling_source.is_dir():
            shutil.copytree(sibling_source, staged, symlinks=True)
        else:
            staged.mkdir()
        (staged / SKILL_FILE).write_text(text, encoding="utf-8")
        _write_staged(staged, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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
    config = row.get("config") or {}
    fields = {}
    model = (config.get("agentOptions") or {}).get("model")
    if model:
        fields["model"] = model
    return fields, str(config.get("persona") or "")


def merged_reference(ref: Reference, harness: str) -> Reference:
    """`ref` with this harness's copy's body and own fields taken in.

    The shared fields this harness's copy carries are taken from the copy;
    shared fields it does not carry, and every other harness's block, are
    kept. A harness's own block keeps whatever its copy cannot express.
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
    vocab = _OWN_RENDERED[(ref.kind, h)]
    copy_own = {k: v for k, v in copy_fields.items() if k not in shared}
    if vocab is None:
        own = copy_own
    else:
        own = {k: v for k, v in (ref.harness.get(h) or {}).items() if k not in vocab}
        own.update({k: v for k, v in copy_own.items() if k in vocab})
    harness_blocks = dict(ref.harness)
    harness_blocks[h] = own
    return Reference(ref.kind, ref.name, fields, harness_blocks, body)


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


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

    edited = []
    for h in HARNESSES:
        entry = item.get(h) or {}
        if not entry.get("fingerprint"):
            continue
        current = copy_fingerprint(kind, name, h)
        if current is not None and current != entry["fingerprint"]:
            edited.append(h)

    reason = None
    if len(edited) > 1:
        reason = (
            f"{label} was edited in place under {' and '.join(edited)}; "
            "the edits were not merged and need reconciling by hand"
        )
    elif edited and item[edited[0]].get("reference") != ref_hash:
        reason = (
            f"{label}: the {edited[0]} copy was edited, but the reference changed "
            "since that copy was rendered; neither was overwritten"
        )
    if reason:
        outcome.conflicts.append({"item": label, "harnesses": edited, "reason": reason})
        _notify_once(item, "conflict", reason, notify)
        if raise_conflict:
            raise RenderConflict(reason)
        return
    item.pop("conflict", None)

    if edited:
        h = edited[0]
        candidate = merged_reference(ref, h)
        refused = refusal(candidate, mind_name)
        if refused:
            outcome.refused.append({"item": label, "reason": refused})
            _notify_once(item, "refused", refused, notify)
            return
        copy_dir = copy_path(kind, name, h) if kind == KIND_SKILL else None
        _write_reference(candidate, copy_dir, mind_name)
        ref = candidate
        ref_hash = fingerprint(reference_path(kind, name, mind_name))
        # The edited copy is what the reference now says, whether or not
        # rendering it back changes a byte.
        item[h]["fingerprint"] = copy_fingerprint(kind, name, h)
        item[h]["reference"] = ref_hash
        outcome.merged.append(f"{label} ({h})")

    refused = refusal(ref, mind_name)
    if refused:
        outcome.refused.append({"item": label, "reason": refused})
        _notify_once(item, "refused", refused, notify)
        return
    item.pop("refused", None)
    _render_into(ref, item, outcome, catalog=catalog, notify=notify, mind_name=mind_name)


def check(
    *, catalog: Catalog, notify: Notifier, mind_name: str | None = None,
    kinds: Iterable[str] = KINDS, names: Iterable[str] | None = None,
) -> Outcome:
    """Merge every in-place edit, then render every reference everywhere.

    Agents go first: a skill naming an agent is only renderable once that
    agent's reference exists, and a merged agent edit must be visible to the
    skills checked after it.
    """
    outcome = Outcome()
    wanted = set(names) if names is not None else None
    with _locked(mind_name):
        records = load_records(mind_name)
        for kind in sorted(set(kinds), key=lambda k: 0 if k == KIND_AGENT else 1):
            for name in reference_names(kind, mind_name):
                if wanted is not None and name not in wanted:
                    continue
                _sync_item(kind, name, records, outcome,
                           catalog=catalog, notify=notify, mind_name=mind_name)
        save_records(records, mind_name)
    return outcome


def merge_copy(
    kind: str, name: str, *, catalog: Catalog, notify: Notifier,
    mind_name: str | None = None,
) -> Outcome:
    """The same pass for one item, raising on a conflict instead of reporting it."""
    _validate(name)
    outcome = Outcome()
    with _locked(mind_name):
        records = load_records(mind_name)
        _sync_item(kind, name, records, outcome, catalog=catalog, notify=notify,
                   mind_name=mind_name, raise_conflict=True)
        save_records(records, mind_name)
    if outcome.refused:
        raise RenderRefused(outcome.refused[0]["reason"])
    return outcome


def adopt(
    kind: str, name: str, harness: str, *, catalog: Catalog, notify: Notifier,
    mind_name: str | None = None,
) -> Outcome:
    """Make a reference out of a copy that has none, then render the others.

    Explicit rather than part of `check`: a mind's config directory holds
    skills nobody has decided should follow it to every harness, and some
    homes are shared with another mind.
    """
    _validate(name)
    h = normalize_harness(harness)
    if load_reference(kind, name, mind_name) is not None:
        raise RenderError(f"{kind} {name} already has a reference copy")
    source = copy_path(kind, name, h)
    if copy_fingerprint(kind, name, h) is None:
        raise RenderError(f"No {h} copy of {kind} {name} to adopt")
    empty = Reference(kind, name, {"name": name}, {}, "")
    ref = merged_reference(empty, h)
    refused = refusal(ref, mind_name)
    if refused:
        try:
            notify(refused)
        except Exception:
            pass
        raise RenderRefused(refused)
    outcome = Outcome()
    with _locked(mind_name):
        records = load_records(mind_name)
        _write_reference(ref, source if kind == KIND_SKILL else None, mind_name)
        item = records[kind].setdefault(name, {})
        item[h] = {
            "fingerprint": copy_fingerprint(kind, name, h),
            "reference": fingerprint(reference_path(kind, name, mind_name)),
        }
        outcome.adopted.append(f"{kind} {name} ({h})")
        _render_into(ref, item, outcome, catalog=catalog, notify=notify, mind_name=mind_name)
        save_records(records, mind_name)
    return outcome


def remove(kind: str, name: str, mind_name: str | None = None) -> None:
    """Drop the reference, every copy rendered from it, and its record."""
    _validate(name)
    with _locked(mind_name):
        records = load_records(mind_name)
        item = records[kind].pop(name, {})
        for h in HARNESSES:
            if (item.get(h) or {}).get("fingerprint"):
                _remove_copy(kind, name, h)
        _replace(reference_path(kind, name, mind_name))
        save_records(records, mind_name)


def forget_record(kind: str, name: str, mind_name: str | None = None) -> None:
    """Forget what was rendered, so the next pass overwrites rather than merges."""
    _validate(name)
    with _locked(mind_name):
        records = load_records(mind_name)
        records[kind].pop(name, None)
        save_records(records, mind_name)


def copy_status(kind: str, name: str, harness: str, mind_name: str | None = None) -> str:
    """`rendered`, `edited`, `stale`, `missing` or `unmanaged` for one copy."""
    h = normalize_harness(harness)
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


def telegram_notifier(project_dir: Path | None = None) -> Notifier:
    """The mind's own notify tool, on the Telegram channel."""
    tool = (project_dir or PROJECT_DIR) / "tools" / "stateless" / "notify" / "notify.py"
    mind = os.environ.get("MIND_NAME") or "mind"

    def notify(message: str) -> None:
        subprocess.run(
            [sys.executable, str(tool), "send", "--message", f"[{mind}] {message}",
             "--channels", "telegram"],
            capture_output=True, timeout=30, check=False,
        )

    return notify
