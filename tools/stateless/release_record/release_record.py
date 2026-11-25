#!/usr/bin/env python3
"""Write a release record: what this machine's hive runs, from exactly which source.

    release_record.py [--root DIR] [--compose FILE ...] [--repo DIR ...]
                      [--container NAME ...] [--read-installed-commits] [--out FILE]

Run on the host that runs the hive, as the operator. It prints one JSON
record on stdout and exits 0 (`--out FILE` also writes it there). It only
reads: it never builds, starts, stops, pulls or changes anything, and the
only file it ever writes is `--out`. `docker` is called to read with
`compose config`, `ps`, `inspect` and `image inspect`. Only with
`--read-installed-commits` does it also `exec` one fixed read-only python
snippet in a running container, to learn which commit a branch-named install
fetched; without it that commit is left null.
`git` is called to read (`rev-parse`, `status --porcelain`, `remote get-url`,
`ls-files`). It runs on the host, so no mind container gains Docker access
from it.

What it covers
    The main stack is `--root`'s `docker-compose.yml` (and its sibling
    `docker-compose.override.yml`, which compose loads by itself); `--root`
    defaults to the checkout this script lives in. Each `--compose FILE`
    (repeatable) is a further stack of its own, for the console, terminal
    and tools containers that start from their own compose files. Every
    service `docker compose --profile '*' config` resolves appears, running
    or not. The names `voice-server`, `voice-server-kokoro`, `hive-site`,
    `terminal-site` and `hive-tools` are expected: one that no compose file
    declares is looked for as a container by that name (`--container NAME`
    adds more such names), and one found nowhere is listed under
    `missing_expected`, never left out.

Record fields (the manual for the whole record: docs/setup/release-record.md)
    schema_version        1
    recorded_at           UTC time of the run
    identity              the release's identity and nothing else (`complete`
                          is false and `string` is null when no commit or no
                          image id could be read, so a failed read never looks
                          like a release):
                          `hive_mind_commit` (full commit id of --root),
                          `image_ids_hash` (sha256 over the sorted lines
                          `project/service=<image id or none>`), and
                          `string` ("<commit>+<hash>"). Read this key back to
                          name a release; it holds no branch name.
    information_only      `branch`: the branch checked out in --root. A label
                          for people. It is not an identity.
    hive_mind             state of --root: commit, dirty, tracked_changes,
                          untracked, remote (password removed).
    stacks                one entry per compose project: name, files in use.
    compose_files         per file: path, sha256 of its bytes, tracked by git
                          or not, role (compose, override, include).
    local_differences     compose files git does not track, override files,
                          mind settings files (`minds/*/runtime.yaml`, names
                          of their `env:` entries only),
                          includes, env files read, names of the variables set
                          in each env file, and path variables that point
                          outside the checkout.
    services              one entry per service: stack, service, container,
                          running, image_id, image_reference, image_digest,
                          dockerfile, target, base_image, contexts (path,
                          commit, dirty), extra_sources (build contexts added
                          and `git+` installs, each with the named ref, whether
                          that ref is a commit, and `installed_commit` read
                          from the running container when it can be), mounts
                          (host source laid over the container: host, container,
                          read_only, commit, dirty), env_var_names, env_files.
    repos                 each `--repo DIR`: commit and dirty state, for
                          checkouts outside the stack (the inference proxy).
    outside_stack         services this record does not cover (the proxy on
                          the host and the edge minds in their own checkouts).
    missing_expected      expected names found nowhere.
    notes                 things it could not read, and why.

Never printed: an environment value, an env file's content, a build argument
value, a URL's password part or a keyring entry. Names only. Path variables
are the one place a value appears: a variable whose name ends in DIR, PATH,
HOME, ROOT or FILE and whose value is a path that points outside --root.

`--out FILE` is refused (exit 2) when FILE would sit inside a git work tree:
records belong in the machine's state directory under the install, never in a
source checkout. The directory must already exist.

Exit codes: 0 record written, 2 bad invocation or refused `--out`.
Standard library only; every external call goes through `run`.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]

SCHEMA_VERSION = 1
EXPECTED_NAMES = ("voice-server", "voice-server-kokoro", "hive-site",
                  "terminal-site", "hive-tools")
OUTSIDE_STACK = [
    {"name": "inference-proxy",
     "why": "runs as a bare host process, not in any compose file; "
            "give its checkout with --repo"},
    {"name": "skippy", "why": "edge mind in its own checkout"},
    {"name": "mordecai", "why": "edge mind in its own checkout"},
]
_PATH_VAR = re.compile(r"(DIR|PATH|HOME|ROOT|FILE)$")
_HEX = re.compile(r"^[0-9a-f]{7,64}$")
_GIT_URL = re.compile(r"git\+[a-z+]+://[^\s\"'\\]+")
_SNIPPET = (
    "import sys, importlib.metadata as m\n"
    "for n in sys.argv[1:]:\n"
    "    try:\n"
    "        t = m.distribution(n).read_text('direct_url.json')\n"
    "    except Exception:\n"
    "        continue\n"
    "    if t:\n"
    "        print(t)\n"
    "        break\n"
)


def run(argv: list[str], cwd: str | Path | None = None) -> tuple[int, str]:
    """The one door to docker and git: return (exit code, stdout).

    Tests replace this function with captured output. A missing program is
    exit code 127 with no output."""
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=120)
    except (FileNotFoundError, NotADirectoryError):
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return done.returncode, done.stdout


def scrub_url(text: str) -> str:
    """Remove the user and password part of any URL in text."""
    return re.sub(r"(://)[^/\s\"']*@", r"\1", text)


def _json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


# --- git -----------------------------------------------------------------

def git_state(path: str | Path, cache: dict) -> dict | None:
    """Commit and dirty state of the work tree holding `path`, or None."""
    key = str(path)
    if key in cache:
        return cache[key]
    state = None
    rc, top = run(["git", "--no-optional-locks", "-C", key, "rev-parse", "--show-toplevel"])
    if rc == 0 and top.strip():
        rc, commit = run(["git", "--no-optional-locks", "-C", key, "rev-parse", "HEAD"])
        _, status = run(["git", "--no-optional-locks", "-C", key, "status", "--porcelain", "--", "."])
        _, remote = run(["git", "--no-optional-locks", "-C", key, "remote", "get-url", "origin"])
        _, branch = run(["git", "--no-optional-locks", "-C", key, "rev-parse", "--abbrev-ref", "HEAD"])
        lines = [x for x in status.splitlines() if x.strip()]
        untracked = sum(1 for x in lines if x.startswith("??"))
        tracked = len(lines) - untracked
        state = {
            "path": key,
            "toplevel": top.strip(),
            "commit": commit.strip() if rc == 0 and commit.strip() else None,
            "dirty": bool(lines),
            "tracked_changes": tracked,
            "untracked": untracked,
            "remote": scrub_url(remote.strip()) or None,
            "_branch": branch.strip() or None,
        }
    cache[key] = state
    return state


def _public(state: dict | None) -> dict | None:
    if state is None:
        return None
    return {k: v for k, v in state.items() if not k.startswith("_")}


def _is_tracked(path: Path) -> bool:
    rc, _ = run(["git", "-C", str(path.parent), "ls-files", "--error-unmatch",
                 "--", path.name])
    return rc == 0


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# --- compose files -------------------------------------------------------

def _siblings(path: Path) -> list[Path]:
    """The file plus the override compose loads next to it, when present."""
    files = [path]
    name = path.name
    for base, over in (("docker-compose.yml", "docker-compose.override.yml"),
                       ("docker-compose.yaml", "docker-compose.override.yaml"),
                       ("compose.yaml", "compose.override.yaml"),
                       ("compose.yml", "compose.override.yml")):
        if name == base and (path.parent / over).is_file():
            files.append(path.parent / over)
    return files


def _includes(path: Path) -> list[Path]:
    """Paths named under a top-level `include:` key (plain text scan)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    found, inside = [], False
    for line in lines:
        if re.match(r"^include:\s*$", line):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith((" ", "-", "\t")):
                inside = False
                continue
            m = re.match(r"^\s*(?:-\s*)?(?:path:\s*)?(?:-\s*)?([^\s#:][^\s#]*)\s*(?:#.*)?$",
                         line)
            if m and not line.strip().startswith(("#", "env_file", "project_directory")):
                found.append((path.parent / m.group(1).strip("'\"")))
    return found


def _env_keys(path: Path) -> list[str]:
    """Variable names (never values) set in an env file."""
    try:
        text = path.read_text()
    except OSError:
        return []
    keys = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m:
            keys.append(m.group(1))
    return sorted(set(keys))


def _env_path_vars(path: Path, root: Path) -> list[dict]:
    """Path variables of an env file whose value points outside root."""
    out = []
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not m or not _PATH_VAR.search(m.group(1)):
            continue
        value = m.group(2).strip("'\"")
        if "://" in value or not value.startswith(("/", "./", "../", "~")):
            continue
        resolved = Path(os.path.expanduser(value))
        if not resolved.is_absolute():
            resolved = path.parent / resolved
        resolved = Path(os.path.normpath(resolved))
        try:
            resolved.relative_to(root)
            continue
        except ValueError:
            out.append({"variable": m.group(1), "path": str(resolved),
                        "env_file": str(path)})
    return out


def _runtime_env_names(path: Path) -> list[str]:
    """Names (never values) under the top-level `env:` map of a runtime.yaml."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    names, inside = [], False
    for line in lines:
        if re.match(r"^env:\s*$", line):
            inside = True
        elif inside:
            m = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*)\s*:", line)
            if m:
                names.append(m.group(1))
            elif line.strip() and not line.startswith((" ", "\t", "#")):
                inside = False
    return sorted(set(names))


def _compose_config(files: list[Path], notes: list[str]) -> dict | None:
    argv = ["docker", "compose"]
    for f in files:
        argv += ["-f", str(f)]
    argv += ["--profile", "*", "config", "--format", "json"]
    rc, out = run(argv, cwd=files[0].parent)
    data = _json(out) if rc == 0 else None
    if not isinstance(data, dict):
        notes.append(f"docker compose config failed for {files[0]} (exit {rc})")
        return None
    return data


# --- Dockerfile ----------------------------------------------------------

def parse_dockerfile(text: str, args: dict, target: str | None) -> dict:
    """Base image and git installs of the stage chain that builds `target`."""
    defaults: dict[str, str] = {}
    stages: list[dict] = []
    current: dict | None = None
    for raw in text.replace("\\\n", " ").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        word, _, rest = line.partition(" ")
        word = word.upper()
        if word == "ARG":
            name, eq, val = rest.strip().partition("=")
            if eq and name not in defaults:
                defaults[name] = val.strip("'\"")
        elif word == "FROM":
            m = re.match(r"^(?:--\S+\s+)*(\S+)(?:\s+[Aa][Ss]\s+(\S+))?", rest.strip())
            if m:
                current = {"from": m.group(1), "name": m.group(2), "lines": []}
                stages.append(current)
        elif current is not None:
            current["lines"].append(line)

    def subst(value: str) -> str:
        def one(m):
            name, default = m.group(1) or m.group(3), m.group(2)
            if name in args and args[name] is not None:
                return str(args[name])
            if name in defaults:
                return defaults[name]
            return default if default is not None else m.group(0)
        return re.sub(r"\$\{(\w+)(?::?-([^}]*))?\}|\$(\w+)", one, value)

    if not stages:
        return {"base_image": None, "git_installs": []}
    by_name = {s["name"]: s for s in stages if s["name"]}
    stage = by_name.get(target) if target else stages[-1]
    if stage is None:
        return {"base_image": None, "git_installs": []}
    chain, seen = [], set()
    while stage is not None and id(stage) not in seen:
        seen.add(id(stage))
        chain.append(stage)
        stage = by_name.get(stage["from"])
    base = subst(chain[-1]["from"])
    installs = []
    for st in chain:
        for line in st["lines"]:
            for url in _GIT_URL.findall(subst(line)):
                installs.append(scrub_url(url).rstrip("\\"))
    return {"base_image": base, "git_installs": installs}


def _split_git_url(url: str) -> tuple[str, str | None, str]:
    """(repository url, ref, distribution name guess) of a git+ install."""
    body = url.split("#", 1)[0]
    ref = None
    path_start = body.find("://") + 3
    at = body.rfind("@")
    if at > body.find("/", path_start):
        ref = body[at + 1:] or None
        body = body[:at]
    repo = body[len("git+"):] if body.startswith("git+") else body
    name = repo.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return repo, ref, name


# --- docker --------------------------------------------------------------

def _containers(notes: list[str]) -> list[dict]:
    rc, out = run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    names = out.split()
    if rc != 0:
        notes.append(f"docker ps failed (exit {rc}); services are listed as not running")
    if rc != 0 or not names:
        return []
    rc, out = run(["docker", "inspect", *names])
    data = _json(out) if rc == 0 else None
    return data if isinstance(data, list) else []


def _image(ref_or_id: str | None, cache: dict) -> dict | None:
    if not ref_or_id:
        return None
    if ref_or_id not in cache:
        rc, out = run(["docker", "image", "inspect", ref_or_id])
        data = _json(out) if rc == 0 else None
        cache[ref_or_id] = data[0] if isinstance(data, list) and data else None
    return cache[ref_or_id]


def _digest(info: dict | None, ref: str | None) -> str | None:
    digests = (info or {}).get("RepoDigests") or []
    if not digests:
        return None
    repo = (ref or "").rsplit(":", 1)[0] if ref and "@" not in ref else (ref or "").split("@")[0]
    for d in digests:
        if repo and d.startswith(repo + "@"):
            return d
    return digests[0]


def _installed_commit(container: str, dist: str) -> str | None:
    names = [dist, dist.replace("-", "_"), dist.replace("_", "-")]
    rc, out = run(["docker", "exec", container, "python3", "-c", _SNIPPET, *names])
    data = _json(out) if rc == 0 else None
    if isinstance(data, dict):
        return (data.get("vcs_info") or {}).get("commit_id")
    return None


def _names(value) -> list[str]:
    if isinstance(value, dict):
        return sorted(value)
    if isinstance(value, list):
        return sorted({str(x).split("=", 1)[0] for x in value})
    return []


def _env_files(value, base: Path) -> list[str]:
    out = []
    for item in value or []:
        path = item.get("path") if isinstance(item, dict) else item
        if path:
            out.append(str(Path(path) if os.path.isabs(path) else base / path))
    return out


# --- the record ----------------------------------------------------------

def build_service(stack: str, name: str, spec: dict, base_dir: Path,
                  containers: list[dict], project: str | None,
                  cache: dict, images: dict, exec_ok: bool = False) -> dict:
    container = None
    for c in containers:
        labels = (c.get("Config") or {}).get("Labels") or {}
        if (labels.get("com.docker.compose.service") == name
                and labels.get("com.docker.compose.project") == project):
            container = c
            break
        if spec.get("container_name") and c.get("Name", "").lstrip("/") == spec["container_name"]:
            container = c
            break
    build = spec.get("build")
    if isinstance(build, str):
        build = {"context": build}
    build = build or {}
    context = build.get("context")
    dockerfile = build.get("dockerfile") or ("Dockerfile" if context else None)
    dockerfile_path = None
    if context and dockerfile:
        ctx = Path(context) if os.path.isabs(context) else base_dir / context
        dockerfile_path = Path(dockerfile) if os.path.isabs(dockerfile) else ctx / dockerfile

    running = bool(container and (container.get("State") or {}).get("Running"))
    image_ref = spec.get("image") or (container or {}).get("Config", {}).get("Image")
    image_id = (container or {}).get("Image")
    info = _image(image_id or image_ref, images)
    if info is not None:
        image_id = info.get("Id") or image_id

    contexts, extra = [], []
    if context and "://" in str(context):
        contexts.append({"name": "context", "path": scrub_url(str(context)),
                         "commit": None, "dirty": None,
                         "note": "a URL context; its commit is not read"})
    elif context:
        ctx = Path(context) if os.path.isabs(context) else base_dir / context
        contexts.append({"name": "context", "path": str(ctx),
                         **(_public(git_state(ctx, cache)) or {"commit": None, "dirty": None})})
    for cname, cpath in sorted((build.get("additional_contexts") or {}).items()):
        if "://" in str(cpath) or str(cpath).startswith("docker-image:"):
            extra.append({"kind": "build_context", "name": cname, "source": scrub_url(str(cpath))})
            continue
        p = Path(cpath) if os.path.isabs(cpath) else base_dir / cpath
        extra.append({"kind": "build_context", "name": cname, "path": str(p),
                      **(_public(git_state(p, cache)) or {"commit": None, "dirty": None})})

    base_image = None
    if dockerfile_path is not None:
        try:
            parsed = parse_dockerfile(dockerfile_path.read_text(), build.get("args") or {},
                                      build.get("target"))
        except OSError:
            parsed = {"base_image": None, "git_installs": []}
        if parsed["base_image"]:
            binfo = _image(parsed["base_image"], images)
            base_image = {"reference": parsed["base_image"],
                          "digest": _digest(binfo, parsed["base_image"])}
        for url in parsed["git_installs"]:
            repo, ref, dist = _split_git_url(url)
            is_commit = bool(ref and _HEX.match(ref))
            entry = {"kind": "git_install", "repository": repo, "named_ref": ref,
                     "ref_is_commit": is_commit,
                     "installed_commit": ref if is_commit else None}
            if not is_commit:
                entry["note"] = "the build named a branch or tag, not a commit"
                if not exec_ok:
                    entry["note"] += "; run with --read-installed-commits to read which commit was installed"
                if running and exec_ok:
                    entry["installed_commit"] = _installed_commit(
                        (container.get("Name") or "").lstrip("/"), dist)
            extra.append(entry)

    mounts = []
    for vol in spec.get("volumes") or []:
        if not isinstance(vol, dict) or vol.get("type") != "bind":
            continue
        src = Path(vol.get("source", ""))
        st = git_state(src, cache) if src.exists() else None
        mounts.append({"host": str(src), "container": vol.get("target"),
                       "read_only": bool(vol.get("read_only")),
                       "commit": (st or {}).get("commit"),
                       "dirty": (st or {}).get("dirty"),
                       "tracked_changes": (st or {}).get("tracked_changes"),
                       "untracked": (st or {}).get("untracked")})

    return {
        "stack": stack,
        "service": name,
        "container": ((container or {}).get("Name") or spec.get("container_name") or "").lstrip("/") or None,
        "running": running,
        "image_id": image_id,
        "image_reference": image_ref,
        "image_digest": _digest(info, image_ref),
        "dockerfile": str(dockerfile_path) if dockerfile_path else None,
        "target": build.get("target"),
        "base_image": base_image,
        "contexts": contexts,
        "extra_sources": extra,
        "mounts": mounts,
        "env_var_names": _names(spec.get("environment")),
        "env_files": _env_files(spec.get("env_file"), base_dir),
    }


def _inspect_mounts(c: dict, cache: dict) -> list[dict]:
    out = []
    for m in c.get("Mounts") or []:
        if m.get("Type") != "bind":
            continue
        src = Path(m.get("Source", ""))
        st = git_state(src, cache) if src.exists() else None
        out.append({"host": str(src), "container": m.get("Destination"),
                    "read_only": not m.get("RW", True),
                    "commit": (st or {}).get("commit"),
                    "dirty": (st or {}).get("dirty"),
                    "tracked_changes": (st or {}).get("tracked_changes"),
                    "untracked": (st or {}).get("untracked")})
    return out


def container_only(name: str, c: dict, cache: dict, images: dict) -> dict:
    labels = (c.get("Config") or {}).get("Labels") or {}
    workdir = labels.get("com.docker.compose.project.working_dir")
    state = git_state(workdir, cache) if workdir and Path(workdir).exists() else None
    info = _image(c.get("Image"), images)
    ref = (c.get("Config") or {}).get("Image")
    return {
        "stack": labels.get("com.docker.compose.project") or "(container)",
        "service": labels.get("com.docker.compose.service") or name,
        "container": name,
        "running": bool((c.get("State") or {}).get("Running")),
        "image_id": (info or {}).get("Id") or c.get("Image"),
        "image_reference": ref,
        "image_digest": _digest(info, ref),
        "dockerfile": None, "target": None, "base_image": None,
        "contexts": [dict(_public(state) or {"commit": None, "dirty": None},
                          name="compose project directory")],
        "extra_sources": [], "mounts": _inspect_mounts(c, cache),
        "env_var_names": [], "env_files": [],
        "found_by": "container name; no compose file given for it",
    }


def identity_of(commit: str | None, services: list[dict]) -> dict:
    lines = sorted(f"{s['stack']}/{s['service']}={s['image_id'] or 'none'}" for s in services)
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    complete = bool(commit) and any(s["image_id"] for s in services)
    return {"hive_mind_commit": commit, "image_ids_hash": digest,
            "complete": complete,
            "string": f"{commit}+{digest}" if complete else None}


def build_record(root: Path, compose: list[Path], repos: list[Path],
                 extra_names: list[str], now: datetime.datetime | None = None,
                 exec_ok: bool = False) -> dict:
    root = Path(root).resolve()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    notes: list[str] = []
    cache: dict = {}
    images: dict = {}
    containers = _containers(notes)

    stacks_files: list[tuple[str, list[Path]]] = []
    main = root / "docker-compose.yml"
    if main.is_file():
        stacks_files.append(("main", _siblings(main)))
    else:
        notes.append(f"{main} not found; the main stack is not described")
    for f in compose:
        f = Path(f).resolve()
        stacks_files.append((f.parent.name or str(f), _siblings(f)))

    services, stacks, files_out = [], [], []
    differences = {"untracked_compose_files": [], "override_files": [],
                   "include_files": [], "env_files": [], "path_variables_outside_checkout": []}
    seen_files: set[Path] = set()
    matched: set[str] = set()

    def note_file(path: Path, role: str):
        if path in seen_files:
            return
        seen_files.add(path)
        tracked = _is_tracked(path) if path.exists() else False
        files_out.append({"path": str(path), "sha256": _sha256(path),
                          "tracked_by_git": tracked, "role": role})
        if not tracked:
            differences["untracked_compose_files"].append(str(path))
        if role == "override":
            differences["override_files"].append(str(path))
        if role == "include":
            differences["include_files"].append(str(path))

    differences["mind_settings_files"] = []
    for rt in sorted((root / "minds").glob("*/runtime.yaml")):
        differences["mind_settings_files"].append(
            {"path": str(rt), "tracked_by_git": _is_tracked(rt),
             "env_variable_names": _runtime_env_names(rt)})

    for label, files in stacks_files:
        config = _compose_config(files, notes)
        project = (config or {}).get("name")
        stacks.append({"stack": label, "project": project, "files": [str(f) for f in files]})
        for i, f in enumerate(files):
            note_file(f, "compose" if i == 0 else "override")
            for inc in _includes(f):
                note_file(inc.resolve() if inc.exists() else inc, "include")
        dotenv = files[0].parent / ".env"
        if dotenv.is_file():
            differences["env_files"].append({"path": str(dotenv), "variable_names": _env_keys(dotenv)})
            differences["path_variables_outside_checkout"] += _env_path_vars(dotenv, root)
        for name, spec in sorted(((config or {}).get("services") or {}).items()):
            svc = build_service(label, name, spec or {}, files[0].parent,
                                containers, project, cache, images, exec_ok)
            services.append(svc)
            matched.add(name)
            if svc["container"]:
                matched.add(svc["container"])
            for ef in svc["env_files"]:
                if not any(e["path"] == ef for e in differences["env_files"]):
                    differences["env_files"].append(
                        {"path": ef, "variable_names": _env_keys(Path(ef))})

    missing = []
    for name in list(EXPECTED_NAMES) + list(extra_names):
        if name in matched:
            continue
        found = next((c for c in containers if c.get("Name", "").lstrip("/") == name), None)
        if found:
            services.append(container_only(name, found, cache, images))
        else:
            missing.append(name)

    services.sort(key=lambda s: (s["stack"], s["service"]))
    hm = git_state(root, cache)
    record = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": now.replace(microsecond=0).isoformat(),
        "identity": identity_of((hm or {}).get("commit"), services),
        "information_only": {"branch": (hm or {}).get("_branch"),
                             "meaning": "a label for people, never an identity"},
        "hive_mind": _public(hm),
        "stacks": stacks,
        "compose_files": files_out,
        "local_differences": differences,
        "services": services,
        "repos": [_public(git_state(Path(r).resolve(), cache)) or {"path": str(r), "commit": None}
                  for r in repos],
        "outside_stack": OUTSIDE_STACK,
        "missing_expected": missing,
        "notes": notes,
    }
    if hm is None:
        notes.append(f"{root} is not a git work tree; no commit id")
    return record


def _inside_work_tree(directory: Path) -> bool:
    rc, out = run(["git", "-C", str(directory), "rev-parse", "--is-inside-work-tree"])
    return rc == 0 and out.strip() == "true"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", type=Path, default=_ROOT,
                        help="the hive-mind checkout (default: this one)")
    parser.add_argument("--compose", type=Path, action="append", default=[],
                        help="another compose file to describe (repeatable)")
    parser.add_argument("--repo", type=Path, action="append", default=[],
                        help="another checkout to record the commit of (repeatable)")
    parser.add_argument("--container", action="append", default=[],
                        help="another container name to look for (repeatable)")
    parser.add_argument("--read-installed-commits", action="store_true",
                        help="also run one fixed read-only python snippet inside each "
                             "running container to read which commit a branch-named "
                             "install fetched (docker exec; off by default)")
    parser.add_argument("--out", type=Path, default=None,
                        help="also write the record here (never inside a checkout)")
    args = parser.parse_args(argv)

    if args.out is not None:
        directory = args.out.resolve().parent
        if not directory.is_dir():
            print(f"release_record: {directory} does not exist", file=sys.stderr)
            return 2
        if _inside_work_tree(directory):
            print(f"release_record: refusing --out {args.out}: it is inside a git "
                  "work tree; keep records in the state directory under the install",
                  file=sys.stderr)
            return 2

    record = build_record(args.root, args.compose, args.repo, args.container,
                          exec_ok=args.read_installed_commits)
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.write_text(text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
