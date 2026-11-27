"""A bot's claim resolver presents its mind's own key, since the bot holds no credential of its own."""
import asyncio

from hive_surfaces import own_key, surface_claim
from surfaces import claims


def test_the_claim_resolver_asks_with_the_key_in_the_minds_key_file(tmp_path, monkeypatch):
    key_file = tmp_path / "key"
    key_file.write_text("mind-key-one-1234\n")
    monkeypatch.setenv("MIND_KEY_FILE", str(key_file))
    monkeypatch.delenv("COMMS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("COMMS_URL", "http://comms:8424")
    monkeypatch.setenv("MIND_ID", "m1")
    own_key._cache.clear()

    resolver = claims.install_claim_resolver("telegram")
    try:
        assert resolver._headers() == {"Authorization": "Bearer mind-key-one-1234"}
        key_file.write_text("mind-key-two-5678\n")
        import os
        os.utime(key_file, (1, 2))
        assert resolver._headers() == {"Authorization": "Bearer mind-key-two-5678"}
    finally:
        surface_claim.install(None, "telegram")
