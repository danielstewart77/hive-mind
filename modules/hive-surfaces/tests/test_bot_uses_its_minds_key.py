"""B16: a mind's bot reaches the gateway with that mind's key, read from the
mind's key folder at each request; a replaced key is the one presented next."""
import asyncio

from hive_surfaces.gateway_client import GatewayClient


class _Resp:
    status = 200

    async def json(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self):
        self.seen = []

    def get(self, url, **kwargs):
        self.seen.append((kwargs.get("headers") or {}).get("Authorization"))
        return _Resp()


def test_the_bot_presents_the_key_in_its_minds_key_folder_and_the_next_one_after_a_replacement(
    tmp_path, monkeypatch
):
    key_file = tmp_path / "key"
    key_file.write_text("first-key-0001\n")
    monkeypatch.setenv("MIND_KEY_FILE", str(key_file))
    monkeypatch.setenv("MIND_KEY", "an-old-environment-key")
    monkeypatch.setenv("COMMS_BEARER_TOKEN", "a-service-credential")
    http = _Http()
    client = GatewayClient(http, "http://gw.test", "telegram", mind_id="m")

    asyncio.run(client.find_active_session(1, "c"))
    key_file.write_text("second-key-0002\n")
    import os
    os.utime(key_file, ns=(1, 2_000_000_000))
    asyncio.run(client.find_active_session(1, "c"))

    assert http.seen == ["Bearer first-key-0001", "Bearer second-key-0002"]
