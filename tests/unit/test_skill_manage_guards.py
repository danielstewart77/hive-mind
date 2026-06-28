"""Step 2 — validators + guards.

Ported guards: name regex/length, description length, content size, file-path
allowlist + traversal, supporting-file byte limit, frontmatter required-fields,
and atomic write. Assert observable behavior: error strings on bad input, None
on valid, on-disk roundtrip with no leftover temp files.
"""

import importlib.util
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = str(_PROJECT_ROOT / "tools/stateless/skill_manage/skill_manage.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("skill_manage_under_test", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_name_regex_rejects_bad():
    mod = _load_module()
    bad = ["Upper", ".lead", "-lead", "has/slash", "has space", "a" * 65]
    for name in bad:
        assert mod._validate_name(name) is not None, name
    good = ["demo", "my-skill", "my_skill", "v1.2.3", "a" * 64]
    for name in good:
        assert mod._validate_name(name) is None, name


def test_description_over_1024_rejected():
    mod = _load_module()
    desc = "x" * 1025
    content = f"---\nname: demo\ndescription: {desc}\n---\nBody.\n"
    assert mod._validate_frontmatter(content) is not None
    ok = f"---\nname: demo\ndescription: {'x' * 1024}\n---\nBody.\n"
    assert mod._validate_frontmatter(ok) is None


def test_content_over_100k_rejected():
    mod = _load_module()
    content = "x" * 100_001
    assert mod._validate_content_size(content) is not None
    assert mod._validate_content_size("x" * 100_000) is None


def test_file_path_traversal_rejected():
    mod = _load_module()
    assert mod._validate_file_path("../x") is not None
    assert mod._validate_file_path("references/../../x") is not None


def test_file_path_subdir_allowlist():
    mod = _load_module()
    assert mod._validate_file_path("references/a.md") is None
    assert mod._validate_file_path("secret/a.md") is not None
    assert mod._validate_file_path("references") is not None  # bare dir, no filename


def test_supporting_file_over_1mib_rejected():
    mod = _load_module()
    too_big = "x" * (mod.MAX_SKILL_FILE_BYTES + 1)
    assert mod._validate_file_bytes(too_big) is not None
    assert mod._validate_file_bytes("x" * mod.MAX_SKILL_FILE_BYTES) is None


def test_frontmatter_missing_required_rejected():
    mod = _load_module()
    assert mod._validate_frontmatter("no frontmatter here") is not None
    assert mod._validate_frontmatter("---\nname: demo\nBody with no close") is not None
    assert mod._validate_frontmatter("---\ndescription: d\n---\nBody.\n") is not None  # no name
    assert mod._validate_frontmatter("---\nname: demo\n---\nBody.\n") is not None  # no description
    assert mod._validate_frontmatter("---\nname: demo\ndescription: d\n---\n") is not None  # empty body
    assert mod._validate_frontmatter("---\nname: demo\ndescription: d\n---\nBody.\n") is None


def test_atomic_write_roundtrip(tmp_path):
    mod = _load_module()
    target = tmp_path / "sub" / "file.md"
    mod._atomic_write_text(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"
    leftovers = [p for p in (tmp_path / "sub").iterdir() if p.name != "file.md"]
    assert leftovers == []
