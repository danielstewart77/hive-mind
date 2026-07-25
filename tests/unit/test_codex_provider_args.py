"""codex_cli._provider_args: Ollama provider overrides, with optional auth.

When the configured Ollama base URL is really a metering proxy, the mind
authenticates with a bearer key in OPENAI_API_KEY. Codex only sends that key
when the provider config declares env_key, and bare Ollama installs need no
auth — so the declaration must appear exactly when a key is present.
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


def test_ollama_without_key_omits_env_key(codex, monkeypatch):
    monkeypatch.setattr(codex, "PROVIDER", "ollama")
    monkeypatch.setitem(codex.RUNTIME_ENV, "OLLAMA_BASE_URL", "http://ollama:11434/v1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    codex.RUNTIME_ENV.pop("OPENAI_API_KEY", None)
    values = _flag_values(codex._provider_args())
    assert any("base_url" in v for v in values)
    assert not any("env_key" in v for v in values)


def test_ollama_with_key_declares_env_key(codex, monkeypatch):
    monkeypatch.setattr(codex, "PROVIDER", "ollama")
    monkeypatch.setitem(codex.RUNTIME_ENV, "OLLAMA_BASE_URL", "http://proxy:8899/v1")
    monkeypatch.setitem(codex.RUNTIME_ENV, "OPENAI_API_KEY", "hmp-test")
    values = _flag_values(codex._provider_args())
    key = codex.NAME + "_ollama"
    assert f'model_providers.{key}.env_key="OPENAI_API_KEY"' in values
    assert f'model_providers.{key}.base_url="http://proxy:8899/v1"' in values
