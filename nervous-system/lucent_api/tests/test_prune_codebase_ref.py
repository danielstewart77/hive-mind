"""_codebase_ref_exists — PRUNE_PROJECT_ROOT resolution and the keep-on-unverifiable rule.

The pruner deletes a current-state entry when its codebase_ref no longer
resolves. That verdict is only meaningful when the project root is
actually visible to this process — inside the lucent container it usually
isn't — so an unset or missing root must read as "unverifiable, keep",
never as "gone, delete".
"""
from __future__ import annotations

from lucent_api.prune_memory import _codebase_ref_exists


class TestUnverifiableRootKeeps:
    def test_unset_root_keeps_the_entry(self, monkeypatch):
        monkeypatch.delenv("PRUNE_PROJECT_ROOT", raising=False)
        assert _codebase_ref_exists("some/module.py") is True

    def test_missing_root_dir_keeps_the_entry(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PRUNE_PROJECT_ROOT", str(tmp_path / "nope"))
        assert _codebase_ref_exists("some/module.py") is True

    def test_empty_ref_is_always_valid(self, monkeypatch):
        monkeypatch.delenv("PRUNE_PROJECT_ROOT", raising=False)
        assert _codebase_ref_exists("") is True


class TestResolutionAgainstRoot:
    def test_relative_path_resolves(self, monkeypatch, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "module.py").write_text("x = 1\n")
        monkeypatch.setenv("PRUNE_PROJECT_ROOT", str(tmp_path))
        assert _codebase_ref_exists("pkg/module.py") is True

    def test_symbol_resolves_via_grep(self, monkeypatch, tmp_path):
        (tmp_path / "module.py").write_text("def my_special_symbol():\n    pass\n")
        monkeypatch.setenv("PRUNE_PROJECT_ROOT", str(tmp_path))
        assert _codebase_ref_exists("my_special_symbol") is True

    def test_unresolvable_token_fails(self, monkeypatch, tmp_path):
        (tmp_path / "module.py").write_text("x = 1\n")
        monkeypatch.setenv("PRUNE_PROJECT_ROOT", str(tmp_path))
        assert _codebase_ref_exists("no_such_symbol_anywhere") is False

    def test_one_dead_token_fails_the_whole_ref(self, monkeypatch, tmp_path):
        (tmp_path / "module.py").write_text("live_symbol = 1\n")
        monkeypatch.setenv("PRUNE_PROJECT_ROOT", str(tmp_path))
        assert _codebase_ref_exists("module.py, no_such_symbol_anywhere") is False
