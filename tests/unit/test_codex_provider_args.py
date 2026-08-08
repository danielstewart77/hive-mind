"""codex_cli._provider_args: the Ollama provider overrides codex is given.

Ollama is reached through the inference proxy, which meters per mind and
answers 401 without a bearer key, so env_key is declared unconditionally.
What still varies — and so is what gets tested — is whether the mind is on
Ollama at all, and which base URL its runtime config names.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def codex(monkeypatch):
    monkeypatch.setenv("MIND_NAME", "bilby")
    module = importlib.import_module("minds.harness.codex_cli")
    return module


def _flag_values(args: list[str]) -> list[str]:
    # args alternate: "-c", "key=value", ...
    return [a for a in args if a != "-c"]


def test_non_ollama_provider_yields_no_args(codex, monkeypatch):
    monkeypatch.setattr(codex, "PROVIDER", "anthropic")
    assert codex._provider_args() == []


def test_ollama_base_url_comes_from_the_minds_runtime_config(codex, monkeypatch):
    monkeypatch.setattr(codex, "PROVIDER", "ollama")
    monkeypatch.setitem(codex.RUNTIME_ENV, "OLLAMA_BASE_URL", "http://proxy:8899/v1")
    values = _flag_values(codex._provider_args())
    key = codex.NAME + "_ollama"
    assert f'model_providers.{key}.base_url="http://proxy:8899/v1"' in values
