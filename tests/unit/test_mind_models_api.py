"""What a mind reports it may run.

The picker and the permission have to be the same fact, so every test drives
``build_catalog`` with a faked HTTP boundary rather than stubbing the fetch:
*which credential goes on the wire, to which endpoint* is the entire point,
and a test that stubbed the fetch would pass whatever key was presented.
"""

from __future__ import annotations

import pytest
import yaml

from minds import models_api

CLAUDE_ROWS = {
    "data": [
        {"id": "claude-opus-5", "label": "Opus 5", "provider": "anthropic",
         "provider_label": "Anthropic"},
        {"id": "qwen35-131k", "label": "Qwen 3.5 9B (local, 131k)",
         "provider": "ollama", "provider_label": "Ollama"},
    ]
}
CODEX_ROWS = {
    "data": [
        {"id": "gpt-5.4", "label": "gpt-5.4", "provider": "openai",
         "provider_label": "OpenAI"},
    ]
}


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status = payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _Session:
    def __init__(self, routes, headers, seen):
        self._routes, self._headers, self._seen = routes, headers or {}, seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self._seen.append((url, self._headers.get("Authorization", "")))
        for suffix, outcome in self._routes.items():
            if url.endswith(suffix):
                if isinstance(outcome, Exception):
                    raise outcome
                return _Resp(outcome)
        return _Resp({}, 404)


@pytest.fixture
def wire(monkeypatch):
    seen: list[tuple[str, str]] = []

    def install(routes):
        def factory(*args, headers=None, **kwargs):
            return _Session(routes, headers, seen)

        monkeypatch.setattr(models_api.aiohttp, "ClientSession", factory)
        return seen

    return install


@pytest.fixture
def runtime_file(tmp_path, monkeypatch):
    def write(harness="claude_cli", provider="anthropic", env=None):
        path = tmp_path / "runtime.yaml"
        path.write_text(
            yaml.safe_dump(
                {"name": "m", "harness": harness, "provider": provider,
                 "default_model": "claude-opus-5", "env": env or {}}
            )
        )
        # A real mind's key arrives in its process environment; clear the
        # host's so a stray value cannot stand in for the mind's own.
        for var in ("INFERENCE_PROXY_URL", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
                    "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "MIND_PROXY_KEY"):
            monkeypatch.delenv(var, raising=False)
        return path

    return write


@pytest.mark.asyncio
async def test_the_listing_is_fetched_with_this_minds_own_proxy_key(wire, runtime_file):
    """The proxy filters by key, so the key decides what the picker offers."""
    seen = wire({"/v1/anthropic/models": CLAUDE_ROWS})
    path = runtime_file(env={
        "ANTHROPIC_BASE_URL": "http://proxy:8899",
        "ANTHROPIC_AUTH_TOKEN": "hmp-ada",
    })

    await models_api.build_catalog(path)

    assert seen == [("http://proxy:8899/v1/anthropic/models", "Bearer hmp-ada")]


@pytest.mark.asyncio
async def test_a_codex_mind_is_listed_the_shape_its_harness_speaks(wire, runtime_file):
    """The endpoint identifies the harness; a codex mind asks the other one."""
    seen = wire({"/v1/models": CODEX_ROWS})
    path = runtime_file(harness="codex_cli", env={
        "INFERENCE_PROXY_URL": "http://proxy:8899",
        "OPENAI_API_KEY": "hmp-nagatha",
    })

    rows = await models_api.build_catalog(path)

    assert seen[0][0] == "http://proxy:8899/v1/models"
    assert [r["name"] for r in rows] == ["gpt-5.4"]


@pytest.mark.asyncio
async def test_each_row_carries_the_label_and_the_provider_hosting_it(
    wire, runtime_file
):
    """A picker shows the label, saves the name, and groups by provider."""
    wire({"/v1/anthropic/models": CLAUDE_ROWS})
    path = runtime_file(env={
        "ANTHROPIC_BASE_URL": "http://proxy:8899",
        "ANTHROPIC_AUTH_TOKEN": "hmp-ada",
    })

    rows = {row["name"]: row for row in await models_api.build_catalog(path)}

    assert rows["claude-opus-5"]["label"] == "Opus 5"
    assert rows["claude-opus-5"]["provider"] == "anthropic"
    assert rows["qwen35-131k"]["provider"] == "ollama"
    assert rows["qwen35-131k"]["provider_label"] == "Ollama"


@pytest.mark.asyncio
async def test_an_unreachable_proxy_yields_nothing_rather_than_raising(
    wire, runtime_file
):
    """The console needs an empty list to report on, not a 502 to swallow."""
    wire({"/v1/anthropic/models": RuntimeError("connection refused")})
    path = runtime_file(env={
        "ANTHROPIC_BASE_URL": "http://proxy:8899",
        "ANTHROPIC_AUTH_TOKEN": "hmp-ada",
    })

    assert await models_api.build_catalog(path) == []
