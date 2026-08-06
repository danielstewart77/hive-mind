"""What a mind reports it may run.

The picker and the permission have to be the same fact. Every test here
drives ``build_catalog`` with a faked HTTP boundary rather than stubbing
the fetch helpers, because *which credential is presented to the proxy* is
the entire point — a test that stubbed the fetch would pass no matter whose
key went on the wire.
"""

from __future__ import annotations

import pytest
import yaml

from minds import models_api
from minds.models_api import _bare_tag

ADA_ROWS = {"data": [{"id": "claude-opus-5"}, {"id": "claude-sonnet-5"}]}
ADMIN_ROWS = {"data": [{"id": "claude-opus-5"}, {"id": "claude-fable-5"}]}
OPENAI_ROWS = {"data": [{"id": "gpt-5.4"}, {"id": "gemma4-131k"}]}
OLLAMA_TAGS = {"models": [{"name": "gemma4-131k:latest"}, {"name": "qwen35-131k"}]}

ALIASES = {"opus": "anthropic", "sonnet": "anthropic", "haiku": "anthropic"}


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
    def write(provider="anthropic", env=None):
        path = tmp_path / "runtime.yaml"
        path.write_text(
            yaml.safe_dump(
                {"name": "m", "harness": "claude_cli", "provider": provider,
                 "default_model": "opus", "env": env or {}}
            )
        )
        # A real mind's key arrives in its process environment; clear the
        # host's so a stray value cannot stand in for the mind's own.
        for var in ("INFERENCE_PROXY_URL", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                    "OPENAI_API_KEY", "MIND_PROXY_KEY", "OLLAMA_BASE_URL", "OLLAMA_HOST"):
            monkeypatch.delenv(var, raising=False)
        return path

    return write


def _names(rows, provider):
    return {r["name"] for r in rows if r["provider"] == provider}


@pytest.mark.asyncio
async def test_the_catalog_is_fetched_with_this_minds_own_proxy_key(
    wire, runtime_file, monkeypatch
):
    """Requirement 1: the picker is filtered by what this mind may request.

    The proxy hides admin-only deployments from an unprivileged client, so
    presenting the mind's own key is what makes an unavailable model absent
    rather than merely unselected.
    """
    path = runtime_file()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.test:8899")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "hmp-ada-key")
    seen = wire({"/v1/anthropic/models": ADA_ROWS, "/v1/models": OPENAI_ROWS})

    rows = await models_api.build_catalog(path, static_aliases=ALIASES)

    assert _names(rows, "anthropic") >= {"claude-opus-5", "claude-sonnet-5"}
    assert {auth for _, auth in seen} == {"Bearer hmp-ada-key"}


@pytest.mark.asyncio
async def test_a_privileged_key_sees_what_an_unprivileged_one_does_not(
    wire, runtime_file, monkeypatch
):
    """Requirement 2: two minds asking get two different answers.

    This is the behaviour a shared catalog key destroys — it would report
    Fable to every mind, including the ones the proxy would refuse.
    """
    path = runtime_file()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.test:8899")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "hmp-admin-key")
    wire({"/v1/anthropic/models": ADMIN_ROWS, "/v1/models": OPENAI_ROWS})

    rows = await models_api.build_catalog(path, static_aliases=ALIASES)

    assert "claude-fable-5" in _names(rows, "anthropic")


@pytest.mark.asyncio
async def test_a_codex_mind_reads_its_key_from_the_openai_variable(
    wire, runtime_file, monkeypatch
):
    """Requirement 3: a codex mind carries OPENAI_API_KEY, not the Anthropic one.

    It also has no base-URL variable at all — its provider lives in
    CODEX_HOME's config.toml — so the explicit proxy URL is what it has.
    """
    path = runtime_file()
    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.test:8899")
    monkeypatch.setenv("OPENAI_API_KEY", "hmp-nagatha-key")
    seen = wire({"/v1/anthropic/models": ADA_ROWS, "/v1/models": OPENAI_ROWS})

    rows = await models_api.build_catalog(path, static_aliases=ALIASES)

    assert "gpt-5.4" in _names(rows, "openai")
    assert {auth for _, auth in seen} == {"Bearer hmp-nagatha-key"}


@pytest.mark.asyncio
async def test_an_ollama_mind_also_reports_what_is_pulled_on_its_box(
    wire, runtime_file, monkeypatch
):
    """Requirement 4: Bob runs a local tag and must be able to pick one."""
    path = runtime_file(provider="ollama")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.test:8899")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "hmp-bob-key")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434")
    wire({"/v1/anthropic/models": ADA_ROWS, "/v1/models": OPENAI_ROWS,
          "/api/tags": OLLAMA_TAGS})

    rows = await models_api.build_catalog(path, static_aliases=ALIASES)

    assert _names(rows, "ollama") == {"gemma4-131k", "qwen35-131k"}


@pytest.mark.asyncio
async def test_the_short_aliases_survive_alongside_the_real_names(
    wire, runtime_file, monkeypatch
):
    """Requirement 5: Ada's row says `opus`.

    Dropping the alias would leave her current model absent from her own
    picker, which reads as a mind set to something unavailable.
    """
    path = runtime_file()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.test:8899")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "hmp-ada-key")
    wire({"/v1/anthropic/models": ADA_ROWS, "/v1/models": OPENAI_ROWS})

    rows = await models_api.build_catalog(path, static_aliases=ALIASES)

    assert {"opus", "sonnet", "haiku"} <= _names(rows, "anthropic")


@pytest.mark.asyncio
async def test_an_unreachable_proxy_still_yields_the_local_catalog(
    wire, runtime_file, monkeypatch
):
    """Requirement 6: a degraded picker beats an empty one.

    An empty list does not read as "the proxy is down" to whoever is
    looking at it; it reads as "this mind has no models".
    """
    path = runtime_file(provider="ollama")
    monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.test:8899")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "hmp-bob-key")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434")
    wire({"/api/tags": OLLAMA_TAGS,
          "/v1/anthropic/models": ConnectionError("down"),
          "/v1/models": ConnectionError("down")})

    rows = await models_api.build_catalog(path, static_aliases=ALIASES)

    assert _names(rows, "ollama") == {"gemma4-131k", "qwen35-131k"}
    assert {"opus", "sonnet", "haiku"} <= _names(rows, "anthropic")


@pytest.mark.asyncio
async def test_a_proxy_row_served_by_local_ollama_is_listed_once(
    wire, runtime_file, monkeypatch
):
    """Requirement 7: `gemma4-131k` is both metered and pulled.

    Ollama reports an untagged pull as `gemma4-131k:latest` while the proxy
    row is the bare name, so the two only collapse if the tag is normalised
    first — otherwise Bob's own model appears twice in his own picker.
    """
    path = runtime_file(provider="ollama")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.test:8899")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "hmp-bob-key")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434")
    wire({"/v1/anthropic/models": ADA_ROWS, "/v1/models": OPENAI_ROWS,
          "/api/tags": OLLAMA_TAGS})

    rows = await models_api.build_catalog(path, static_aliases=ALIASES)

    matches = [r for r in rows if _bare_tag(r["name"]) == "gemma4-131k"]
    assert [r["provider"] for r in matches] == ["ollama"]
