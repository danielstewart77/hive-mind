"""Every codex the mind starts runs its hooks without interactive trust.

Codex runs a configured hook only once its definition has been trusted in a
TUI and recorded in config.toml. A mind's codex never has anyone there to
trust it, so without `--dangerously-bypass-hook-trust` the memory, rotation
and per-turn context hooks are skipped without a word.
"""

from __future__ import annotations

import importlib

import pytest

FLAG = "--dangerously-bypass-hook-trust"


@pytest.fixture()
def codex(monkeypatch):
    monkeypatch.setenv("MIND_NAME", "bilby")
    return importlib.import_module("minds.harness.codex_cli")


def test_a_first_chat_turn_runs_the_minds_hooks_untrusted(codex):
    cmd = codex._exec_argv("gpt-5.6-luna", None)
    assert cmd[:2] == ["codex", "exec"]
    assert FLAG in cmd
    assert cmd[-1] == "-"


def test_a_resumed_chat_turn_runs_the_minds_hooks_untrusted(codex):
    cmd = codex._exec_argv("gpt-5.6-luna", "thread-1", "low")
    assert FLAG in cmd
    assert cmd[-3:] == ["resume", "thread-1", "-"]


def test_the_terminal_pane_runs_the_minds_hooks_untrusted(codex):
    assert FLAG in codex._terminal_argv("gpt-5.6-luna", "thread-1")
