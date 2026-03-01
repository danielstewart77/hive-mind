"""
Hive Mind — Model registry.

Resolves model names to providers with per-process env overrides.
Static aliases (sonnet, opus, haiku) map to Anthropic.
Ollama models are auto-discovered from the Ollama API.
"""

import time
from dataclasses import dataclass, field

import aiohttp


@dataclass
class Provider:
    name: str
    env_overrides: dict[str, str] = field(default_factory=dict)
    api_base: str | None = None  # for model discovery (Ollama)


class ModelRegistry:
    def __init__(
        self,
        providers: dict[str, Provider],
        static_models: dict[str, str],
    ):
        self._providers = providers
        self._static = static_models  # {"sonnet": "anthropic", ...}
        self._ollama_cache: list[str] = []
        self._ollama_cache_ts: float = 0

    def get_provider(self, model: str) -> Provider:
        """Resolve model name -> provider. Static aliases first, then Ollama."""
        if model in self._static:
            return self._providers[self._static[model]]
        if "ollama" in self._providers:
            return self._providers["ollama"]
        raise ValueError(f"Unknown model: {model}")

    async def list_models(self) -> list[dict]:
        """Merge static aliases + live Ollama models into one flat list."""
        result = []
        for alias, provider_name in self._static.items():
            result.append({"name": alias, "provider": provider_name})
        if "ollama" in self._providers:
            models = await self._fetch_ollama_models()
            for m in models:
                result.append({"name": m, "provider": "ollama"})
        return result

    async def _fetch_ollama_models(self) -> list[str]:
        """GET /api/tags from Ollama server, return model names. Cached 60s."""
        if time.time() - self._ollama_cache_ts < 60:
            return self._ollama_cache
        api_base = self._providers["ollama"].api_base
        if not api_base:
            return []
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{api_base}/api/tags", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    self._ollama_cache = [m["name"] for m in data.get("models", [])]
                    self._ollama_cache_ts = time.time()
        except Exception:
            pass  # Return stale cache or empty list
        return self._ollama_cache
