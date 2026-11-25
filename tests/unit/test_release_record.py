"""Tests for tools/stateless/release_record/release_record.py.

The tool's only door to docker and git is `release_record.run`; these tests
replace that one function with captured output (the process stops being ours
there) and call the real tool on a real temporary directory tree.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = (Path(__file__).resolve().parents[2] / "tools" / "stateless"
         / "release_record" / "release_record.py")
_spec = importlib.util.spec_from_file_location("release_record", _PATH)
rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rr)

TOKEN = "PLANTED-TOKEN-9f3a7c21d4"
PASSWORD = "PLANTED-PW-77aa31"
COMMIT = "a" * 40
SURFACES_COMMIT = "b" * 40
IMAGE_APP = "sha256:" + "1" * 64
IMAGE_DB = "sha256:" + "2" * 64


class Fake:
    """Captured docker and git output, keyed by the command's first words."""

    def __init__(self, root: Path, *, status: str = "", running: bool = True,
                 app_image: str = IMAGE_APP, config: dict | None = None):
        self.root = root
        self.status = status
        self.running = running
        self.app_image = app_image
        self.config = config
        self.calls: list[list[str]] = []
        self.git_ok = True

    def __call__(self, argv, cwd=None):
        self.calls.append(list(argv))
        prog = argv[0]
        if prog == "git":
            if not self.git_ok:
                return 128, ""
            sub = argv[argv.index("-C") + 2:]
            if sub[:2] == ["rev-parse", "--show-toplevel"]:
                return 0, str(self.root) + "\n"
            if sub[:2] == ["rev-parse", "HEAD"]:
                return 0, COMMIT + "\n"
            if sub[:2] == ["rev-parse", "--abbrev-ref"]:
                return 0, "some-branch\n"
            if sub[:1] == ["status"]:
                return 0, self.status
            if sub[:2] == ["remote", "get-url"]:
                return 0, f"https://user:{PASSWORD}@example.com/org/hive-mind.git\n"
            if sub[:1] == ["ls-files"]:
                return (0, "") if sub[-1] == "docker-compose.example.yml" else (1, "")
            if sub[:2] == ["rev-parse", "--is-inside-work-tree"]:
                return 0, "true\n"
        if prog == "docker":
            if argv[1:3] == ["compose", "-f"]:
                return 0, json.dumps(self.config)
            if argv[1:3] == ["ps", "-a"]:
                return 0, "hive-app\nhive-db\n"
            if argv[1] == "inspect":
                return 0, json.dumps(self._containers())
            if argv[1:3] == ["image", "inspect"]:
                ref = argv[3]
                digest = {"pgvector/pgvector:pg17": "pgvector/pgvector@sha256:" + "d" * 64}.get(ref)
                return 0, json.dumps([{"Id": ref if ref.startswith("sha256:") else IMAGE_DB,
                                       "RepoDigests": [digest] if digest else []}])
            if argv[1] == "exec":
                return 0, json.dumps({"url": "https://github.com/org/hive-surfaces",
                                      "vcs_info": {"vcs": "git", "commit_id": SURFACES_COMMIT}})
        return 1, ""

    def _containers(self):
        labels = {"com.docker.compose.project": "hive", "com.docker.compose.service": "app"}
        out = [{"Name": "/hive-app", "Image": self.app_image,
                "State": {"Running": self.running},
                "Config": {"Image": "hive-app", "Labels": labels}}]
        return out


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "hive"
    root.mkdir(exist_ok=True)
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / ".env").write_text(f"API_KEY={TOKEN}\nHOST_DEV_DIR=/srv/elsewhere/dev\nPLAIN=1\n")
    (root / "Dockerfile").write_text(
        "ARG BASE=python:3.12-slim\n"
        "FROM ${BASE} AS base\n"
        f"RUN pip install git+https://user:{PASSWORD}@github.com/org/hive-surfaces.git@main\n"
        "FROM base AS mind\n")
    (tmp_path / "src").mkdir(exist_ok=True)
    return root


def _config(root: Path, src: Path) -> dict:
    return {
        "name": "hive",
        "services": {
            "app": {
                "container_name": "hive-app",
                "build": {"context": str(root), "dockerfile": "Dockerfile", "target": "mind",
                          "args": {"TOKEN_ARG": TOKEN}},
                "environment": {"API_KEY": TOKEN, "OTHER": "x"},
                "env_file": [{"path": str(root / ".env")}],
                "volumes": [{"type": "bind", "source": str(src), "target": "/usr/src/app",
                             "read_only": True},
                            {"type": "volume", "source": "data", "target": "/data"}],
            },
            "db": {"container_name": "hive-db", "image": "pgvector/pgvector:pg17"},
        },
    }


def _record(tmp_path, monkeypatch, exec_ok=True, **kw):
    root = _tree(tmp_path)
    fake = Fake(root, config=_config(root, tmp_path / "src"), **kw)
    monkeypatch.setattr(rr, "run", fake)
    return root, rr.build_record(root, [], [], [], exec_ok=exec_ok), fake


def _service(record, name):
    return next(s for s in record["services"] if s["service"] == name)


def test_no_secret_value_reaches_the_record(tmp_path, monkeypatch, capsys):
    root = _tree(tmp_path)
    fake = Fake(root, config=_config(root, tmp_path / "src"))
    monkeypatch.setattr(rr, "run", fake)
    assert rr.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert PASSWORD not in out
    record = json.loads(out)
    # names are kept: the planted variable is named, never valued
    assert "API_KEY" in _service(record, "app")["env_var_names"]


def test_mount_records_host_container_mode_and_host_commit(tmp_path, monkeypatch):
    _, record, _ = _record(tmp_path, monkeypatch, status=" M a.py\n?? b.py\n")
    mounts = _service(record, "app")["mounts"]
    assert mounts == [{"host": str(tmp_path / "src"), "container": "/usr/src/app",
                       "read_only": True, "commit": COMMIT, "dirty": True,
                       "tracked_changes": 1, "untracked": 1}]


def test_clean_work_tree_is_not_flagged_dirty(tmp_path, monkeypatch):
    _, record, _ = _record(tmp_path, monkeypatch, status="")
    assert record["hive_mind"]["dirty"] is False
    assert _service(record, "app")["contexts"][0]["dirty"] is False


def test_context_carries_commit_and_dirty_state_and_dockerfile_target(tmp_path, monkeypatch):
    root, record, _ = _record(tmp_path, monkeypatch, status="?? new.txt\n")
    app = _service(record, "app")
    ctx = app["contexts"][0]
    assert (ctx["path"], ctx["commit"], ctx["dirty"], ctx["untracked"]) == (
        str(root), COMMIT, True, 1)
    assert app["dockerfile"] == str(root / "Dockerfile")
    assert app["target"] == "mind"
    assert app["base_image"]["reference"] == "python:3.12-slim"


def test_branch_install_is_recorded_as_the_commit_actually_installed(tmp_path, monkeypatch):
    _, record, _ = _record(tmp_path, monkeypatch)
    install = _service(record, "app")["extra_sources"][0]
    assert install["kind"] == "git_install"
    assert install["named_ref"] == "main"
    assert install["ref_is_commit"] is False
    assert "branch" in install["note"]
    assert install["installed_commit"] == SURFACES_COMMIT


def test_service_declared_but_not_running_is_listed_as_not_running(tmp_path, monkeypatch):
    _, up, _ = _record(tmp_path, monkeypatch, running=True)
    assert _service(up, "app")["running"] is True
    _, record, _ = _record(tmp_path, monkeypatch, running=False)
    assert _service(record, "app")["running"] is False
    assert _service(record, "db")["running"] is False


def test_image_reference_resolves_to_its_digest(tmp_path, monkeypatch):
    _, record, _ = _record(tmp_path, monkeypatch)
    assert _service(record, "db")["image_digest"] == "pgvector/pgvector@sha256:" + "d" * 64


def test_expected_services_found_nowhere_are_named_as_missing(tmp_path, monkeypatch):
    _, record, _ = _record(tmp_path, monkeypatch)
    assert record["missing_expected"] == [
        "voice-server", "voice-server-kokoro", "hive-site", "terminal-site", "hive-tools"]


def test_untracked_compose_file_and_outside_path_variable_are_local_differences(tmp_path, monkeypatch):
    root, record, _ = _record(tmp_path, monkeypatch)
    diff = record["local_differences"]
    assert str(root / "docker-compose.yml") in diff["untracked_compose_files"]
    assert {"variable": "HOST_DEV_DIR", "path": "/srv/elsewhere/dev",
            "env_file": str(root / ".env")} in diff["path_variables_outside_checkout"]
    env = next(e for e in diff["env_files"] if e["path"] == str(root / ".env"))
    assert env["variable_names"] == ["API_KEY", "HOST_DEV_DIR", "PLAIN"]


def test_identity_is_commit_plus_image_hash_and_ignores_the_branch(tmp_path, monkeypatch):
    _, first, _ = _record(tmp_path, monkeypatch)
    ident = first["identity"]
    assert ident["hive_mind_commit"] == COMMIT
    assert ident["string"] == f"{COMMIT}+{ident['image_ids_hash']}"
    assert first["information_only"]["branch"] == "some-branch"


def test_identity_changes_when_a_service_runs_a_different_image(tmp_path, monkeypatch):
    _, first, _ = _record(tmp_path, monkeypatch)
    _, second, _ = _record(tmp_path, monkeypatch, app_image="sha256:" + "9" * 64)
    assert first["identity"]["image_ids_hash"] != second["identity"]["image_ids_hash"]


def test_out_inside_a_git_work_tree_is_refused_and_nothing_is_written(tmp_path, monkeypatch, capsys):
    root = _tree(tmp_path)
    monkeypatch.setattr(rr, "run", Fake(root, config=_config(root, tmp_path / "src")))
    target = tmp_path / "src" / "record.json"
    assert rr.main(["--root", str(root), "--out", str(target)]) == 2
    assert not target.exists()


def test_identity_is_not_a_release_when_nothing_could_be_read(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    monkeypatch.setattr(rr, "run", lambda argv, cwd=None: (127, ""))
    record = rr.build_record(root, [], [], [])
    assert record["identity"]["complete"] is False
    assert record["identity"]["string"] is None


def test_identity_ignores_the_branch_name(tmp_path, monkeypatch):
    _, first, _ = _record(tmp_path, monkeypatch)
    renamed = Fake.__call__

    def other_branch(self, argv, cwd=None):
        if argv[0] == "git" and "--abbrev-ref" in argv:
            return 0, "another-branch\n"
        return renamed(self, argv, cwd)

    monkeypatch.setattr(Fake, "__call__", other_branch)
    _, second, _ = _record(tmp_path, monkeypatch)
    assert second["information_only"]["branch"] == "another-branch"
    assert second["identity"] == first["identity"]


def test_url_password_with_at_sign_or_in_a_url_context_is_removed():
    assert rr.scrub_url("https://user:p@ss@host/org/repo@main") == "https://host/org/repo@main"
    assert PASSWORD not in rr.scrub_url(f"git+https://u:{PASSWORD}@h/x.git@main")


def test_mind_settings_file_is_named_with_variable_names_only(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    mind = root / "minds" / "ada"
    mind.mkdir(parents=True)
    (mind / "runtime.yaml").write_text(f"env:\n  ADA_PROXY_KEY: {TOKEN}\n  OTHER: 1\nname: ada\n")
    monkeypatch.setattr(rr, "run", Fake(root, config=_config(root, tmp_path / "src")))
    record = rr.build_record(root, [], [], [])
    assert record["local_differences"]["mind_settings_files"] == [
        {"path": str(mind / "runtime.yaml"), "tracked_by_git": False,
         "env_variable_names": ["ADA_PROXY_KEY", "OTHER"]}]
    assert TOKEN not in json.dumps(record)


def test_out_writes_the_record_when_outside_a_work_tree(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    fake = Fake(root, config=_config(root, tmp_path / "src"))

    def not_a_tree(argv, cwd=None):
        if argv[0] == "git" and "--is-inside-work-tree" in argv:
            return 128, ""
        return fake(argv, cwd)

    monkeypatch.setattr(rr, "run", not_a_tree)
    target = tmp_path / "state.json"
    assert rr.main(["--root", str(root), "--out", str(target)]) == 0
    assert json.loads(target.read_text())["identity"]["hive_mind_commit"] == COMMIT


def test_installed_commit_is_not_read_from_a_container_unless_asked(tmp_path, monkeypatch):
    _, record, fake = _record(tmp_path, monkeypatch, exec_ok=False)
    assert _service(record, "app")["extra_sources"][0]["installed_commit"] is None
    assert not any(c[:2] == ["docker", "exec"] for c in fake.calls)
