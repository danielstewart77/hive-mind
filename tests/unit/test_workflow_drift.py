"""The drift script run against real temp trees: a skills folder, the
as-generated copies, their records and the documents the records name.

The script's own entry point is called with argv; what is asserted is what it
printed and the status it returned.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = (Path(__file__).resolve().parents[2]
           / "tools/stateless/workflow_drift/workflow_drift.py")
_spec = importlib.util.spec_from_file_location("workflow_drift_tool", _SCRIPT)
drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drift)

STAGE = "docs/stage.md"
TOP = "docs/top.md"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def tree(tmp_path):
    """A checkout with two documents and two skills generated from them."""
    root = tmp_path / "checkout"
    (root / "docs").mkdir(parents=True)
    (root / STAGE).write_bytes(b"stage v1")
    (root / TOP).write_bytes(b"top v1")
    skills = root / "install" / "workflow-skills"

    def make(name, document=STAGE):
        for base in (skills / name, skills / ".as-generated" / name):
            (base / "references").mkdir(parents=True)
            (base / "SKILL.md").write_text(f"# {name}\n")
            (base / "references" / "notes.md").write_text("notes\n")
        record = {"workflow": "w", "document": document,
                  "document_version": _sha((root / document).read_bytes()),
                  "top_document": TOP, "top_version": _sha(b"top v1")}
        (skills / ".as-generated" / name / ".generated.json").write_text(
            json.dumps(record))

    make("alpha")
    make("beta")
    return root, skills


def run(root, skills, capsys):
    code = drift.main(["--root", str(root), "--skills", str(skills)])
    return code, capsys.readouterr().out.splitlines()


def test_nothing_is_printed_when_every_skill_matches_its_copy(tree, capsys):
    root, skills = tree
    assert run(root, skills, capsys) == (0, [])


def test_an_install_with_no_skills_folder_or_an_empty_one_prints_nothing(
        tmp_path, capsys):
    assert run(tmp_path, tmp_path / "absent", capsys) == (0, [])
    (tmp_path / "empty").mkdir()
    assert run(tmp_path, tmp_path / "empty", capsys) == (0, [])


def test_an_edited_file_names_that_skill_as_tuned_and_no_other(tree, capsys):
    root, skills = tree
    (skills / "beta" / "references" / "notes.md").write_text("tuned\n")
    code, lines = run(root, skills, capsys)
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("beta: tuned")


def test_a_file_added_to_a_skill_makes_it_tuned(tree, capsys):
    root, skills = tree
    (skills / "alpha" / "extra.md").write_text("new\n")
    code, lines = run(root, skills, capsys)
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("alpha: tuned")


def test_a_file_removed_from_a_skill_makes_it_tuned(tree, capsys):
    root, skills = tree
    (skills / "alpha" / "references" / "notes.md").unlink()
    code, lines = run(root, skills, capsys)
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("alpha: tuned")


def test_the_record_file_is_not_part_of_the_comparison(tree, capsys):
    root, skills = tree
    # a live skill that happens to carry a file of that name is tuned
    (skills / "alpha" / ".generated.json").write_text("{}")
    code, lines = run(root, skills, capsys)
    assert code == 1 and lines[0].startswith("alpha: tuned")


def test_a_changed_stage_document_names_only_skills_made_from_it(
        tree, capsys):
    root, skills = tree
    other = root / "docs" / "other.md"
    other.write_bytes(b"other v1")
    record = skills / ".as-generated" / "beta" / ".generated.json"
    data = json.loads(record.read_text())
    data.update(document="docs/other.md", document_version=_sha(b"other v1"))
    record.write_text(json.dumps(data))
    (root / STAGE).write_bytes(b"stage v2")
    code, lines = run(root, skills, capsys)
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("alpha: stale")


def test_a_changed_top_document_names_every_skill(tree, capsys):
    root, skills = tree
    (root / TOP).write_bytes(b"top v2")
    code, lines = run(root, skills, capsys)
    assert code == 1
    assert [l.split(":")[0] for l in lines] == ["alpha", "beta"]
    assert all(": stale" in l for l in lines)


def test_a_skill_that_is_tuned_and_stale_gets_a_line_for_each(tree, capsys):
    root, skills = tree
    (skills / "alpha" / "SKILL.md").write_text("tuned\n")
    (root / STAGE).write_bytes(b"stage v2")
    code, lines = run(root, skills, capsys)
    assert [l.split(":")[1].strip() for l in lines
            if l.startswith("alpha")] == ["tuned", "stale"]


def test_lines_come_out_sorted_by_skill_name(tree, capsys):
    root, skills = tree
    names = ["zulu", "mike", "echo", "tango", "alpha", "kilo", "beta", "yankee"]
    for name in names:
        (skills / name).mkdir(exist_ok=True)
        (skills / name / "SKILL.md").write_text("tuned\n")
        copy = skills / ".as-generated" / name
        copy.mkdir(exist_ok=True)
        (copy / "SKILL.md").write_text("generated\n")
        (copy / ".generated.json").write_text(
            (skills / ".as-generated" / "alpha" / ".generated.json").read_text()
            if name != "alpha" else (copy / ".generated.json").read_text())
    _, lines = run(root, skills, capsys)
    tuned = [l.split(":")[0] for l in lines if ": tuned" in l]
    assert tuned == sorted(names)


def test_a_record_that_cannot_be_parsed_or_names_a_path_with_a_nul_is_a_problem(
        tree, capsys):
    root, skills = tree
    record = skills / ".as-generated" / "alpha" / ".generated.json"
    record.write_text("[" * 200000)
    data = json.loads((skills / ".as-generated" / "beta" / ".generated.json").read_text())
    beta = skills / ".as-generated" / "beta" / ".generated.json"
    data["document"] = "docs/s\u0000x"
    beta.write_text(json.dumps(data))
    code, lines = run(root, skills, capsys)
    assert code == 1
    assert [l.split(":")[0] for l in lines] == ["alpha", "beta"]
    assert all(": problem" in l for l in lines)


def test_a_skill_with_no_as_generated_copy_is_a_problem(tree, capsys):
    root, skills = tree
    (skills / "gamma").mkdir()
    (skills / "gamma" / "SKILL.md").write_text("x")
    code, lines = run(root, skills, capsys)
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("gamma: problem")


def test_a_copy_whose_skill_is_gone_is_a_problem(tree, capsys):
    root, skills = tree
    import shutil
    shutil.rmtree(skills / "beta")
    code, lines = run(root, skills, capsys)
    assert code == 1
    assert len(lines) == 1 and lines[0].startswith("beta: problem")


def test_a_dot_folder_is_not_a_skill(tree, capsys):
    root, skills = tree
    (skills / ".cache").mkdir()
    (skills / ".cache" / "x").write_text("x")
    assert run(root, skills, capsys) == (0, [])


@pytest.mark.parametrize("damage", [
    "missing", "unreadable", "array", "no_key", "absolute", "parent_dir",
    "stage_removed", "top_removed"])
def test_a_bad_record_or_vanished_document_is_a_problem(
        tree, capsys, damage):
    root, skills = tree
    record = skills / ".as-generated" / "alpha" / ".generated.json"
    data = json.loads(record.read_text())
    if damage == "missing":
        record.unlink()
    elif damage == "unreadable":
        record.write_text("{not json")
    elif damage == "array":
        record.write_text("[]")
    elif damage == "no_key":
        del data["top_version"]
        record.write_text(json.dumps(data))
    elif damage == "absolute":
        data["document"] = str(root / STAGE)
        record.write_text(json.dumps(data))
    elif damage == "parent_dir":
        data["top_document"] = "docs/../docs/top.md"
        record.write_text(json.dumps(data))
    elif damage == "stage_removed":
        (root / STAGE).rename(root / "docs" / "renamed.md")
        # beta shares the stage document, so it reports it too
    elif damage == "top_removed":
        (root / TOP).unlink()
    code, lines = run(root, skills, capsys)
    assert code == 1
    assert any(l.startswith("alpha: problem") for l in lines)
    assert not any("stale" in l for l in lines)
