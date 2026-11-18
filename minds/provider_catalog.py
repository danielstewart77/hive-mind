"""A Mind's direct-provider model list; all authentication is its own."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path

import aiohttp


def _native_metadata(kind: str) -> dict:
    tree = Path(os.environ.get("DSH_BIN", "/opt/dsh/apps/cli/lib/bin.js")).resolve().parents[3]
    directory = tree / "packages/llm/llm-pi-ai"
    script = "import {getBuiltinModels} from '@earendil-works/pi-ai/providers/all'; console.log(JSON.stringify(getBuiltinModels(process.argv[1])))"
    try:
        result = subprocess.run(["node", "--input-type=module", "-e", script, kind], cwd=directory, capture_output=True, text=True, timeout=5, check=True)
        return {row["id"]: row for row in json.loads(result.stdout) if isinstance(row, dict) and row.get("id")}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


async def fetch(connection: dict, env: dict) -> list[dict]:
    api = connection.get("api_format", "openai")
    base = str(connection.get("base_url") or "").rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    v1 = base if base.endswith("/v1") else base + "/v1"
    token = env.get("HIVE_PROVIDER_TOKEN") or env.get("DSH_API_KEY") or env.get("OPENAI_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN") or ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if api == "anthropic":
        headers = {"anthropic-version": "2023-06-01", **({"x-api-key": token} if token else {})}
    timeout = aiohttp.ClientTimeout(total=10)
    rows, params = [], {}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            for _ in range(20):
                url = root + "/api/tags" if api == "ollama" else v1 + "/models"
                if params:
                    from urllib.parse import urlencode
                    url += "?" + urlencode(params)
                async with session.get(url, timeout=timeout, allow_redirects=False) as response:
                    if response.status != 200:
                        return []
                    payload = await response.json()
                items = payload.get("models" if api == "ollama" else "data", [])
                if not isinstance(items, list):
                    return []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name") if api == "ollama" else item.get("id")
                    if not isinstance(name, str) or not name:
                        continue
                    row = dict(item, id=name, provider=connection.get("kind"), provider_label=connection.get("name"))
                    if api == "ollama":
                        async with session.post(root + "/api/show", json={"model": name}, timeout=timeout, allow_redirects=False) as shown:
                            if shown.status == 200:
                                details = await shown.json()
                                configured = re.search(r"(?m)^num_ctx\s+(\d+)", str(details.get("parameters") or ""))
                                values = [v for k, v in (details.get("model_info") or {}).items() if k.endswith(".context_length") and isinstance(v, int)]
                                row["context_window"] = int(configured[1]) if configured else min(values) if values else None
                    rows.append(row)
                if api != "anthropic" or not payload.get("has_more"):
                    break
                last = payload.get("last_id")
                if not last or params.get("after_id") == last:
                    return []
                params["after_id"] = last
            else:
                return []
    except Exception:
        return []
    if connection.get("kind") in {"openai", "anthropic"}:
        metadata = await asyncio.to_thread(_native_metadata, connection["kind"])
        for row in rows:
            known = metadata.get(row["id"], {})
            if not row.get("context_window"):
                row["context_window"] = known.get("contextWindow")
            if not isinstance(row.get("accepts_images"), bool) and known:
                row["accepts_images"] = "image" in known.get("input", [])
    return rows
