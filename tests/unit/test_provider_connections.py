"""P2: direct connections are consumed by the real Mind runtime."""
import pytest
import yaml

from minds import models_api
from minds.harness import dsh_cli


def test_dsh_direct_connection_can_use_native_catalog_without_proxy_metadata(monkeypatch):
    monkeypatch.setattr(dsh_cli, "RUNTIME", {"provider_connection": {"kind": "openai", "api_format": "openai"}})
    monkeypatch.setattr(dsh_cli, "RUNTIME_ENV", {"DSH_PROXY_BASE_URL": "https://api.openai.com/v1", "DSH_API_KEY": "mind-key"})
    # The adapter reads the process's own environment beside the mind's
    # block, and a suite run from a mind's session carries that mind's key.
    for name in dsh_cli._PROXY_KEY_SOURCES:
        monkeypatch.delenv(name, raising=False)
    env = dsh_cli._model_env("gpt-4.1", None)
    assert env["HIVE_PROVIDER_TOKEN"] == "mind-key"
    assert "DSH_MODEL_CONTEXT_WINDOW" not in env


@pytest.mark.asyncio
async def test_direct_anthropic_listing_uses_correct_endpoint_and_mind_key(tmp_path, monkeypatch):
    source = tmp_path / "runtime.yaml"
    source.write_text(yaml.safe_dump({"provider_connection": {"kind": "anthropic", "api_format": "anthropic", "base_url": "https://direct.example"}, "env": {"HIVE_PROVIDER_TOKEN": "mind-key"}}))

    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self): return {"data": [{"id": "chosen", "context_window": 200000}]}

    class Session:
        def __init__(self, *, headers):
            self.headers = headers
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def get(self, url, **kwargs):
            response = Response()
            if url != "https://direct.example/v1/models" or self.headers.get("x-api-key") != "mind-key":
                response.status = 403
            return response

    monkeypatch.setattr(models_api.aiohttp, "ClientSession", Session)
    rows = await models_api.build_catalog(source, harness="dsh")
    assert [row["name"] for row in rows] == ["chosen"]
    assert rows[0]["context_window"] == 200000
