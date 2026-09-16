"""The gateway fetches a mind's soul by body, and still finds it.

``_fetch_soul`` swallows every transport failure into an empty string, so a
soul that stops arriving does not raise — it composes a system prompt with no
``<soul>`` block and the mind answers as a slightly different person. That is
how the last missing-Mind-node incident presented, and it took a while to
find, which is why this is tested rather than eyeballed.

Requirements under test:
  R7  the session bootstrap asks through the body route and still gets the soul
  R1  the address it calls carries no query string, and the name is on the wire
      capitalised the way the Mind node is stored
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

NS = os.path.join(os.path.dirname(__file__), "..", "..", "nervous-system")
sys.path.insert(0, os.path.abspath(NS))


class _Lucent:
    def __init__(self, soul_values: list[str]):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"])).decode()
                )
                outer.requests.append({"path": self.path, "body": body})
                name = body["names"][0]
                payload = json.dumps({
                    "results": [{
                        "entity": name,
                        "found": True,
                        "count": 1,
                        "matches": [{
                            "properties": {
                                "name": name,
                                "type": "Mind",
                                "soul_values": soul_values,
                            },
                            "connections": [],
                        }],
                    }]
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                # The deleted form. Answering it would let unmigrated code pass.
                self.send_error(405)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_r7_the_bootstrap_still_finds_the_soul_through_the_body_route(monkeypatch):
    from comms import bootstrap_loader

    with _Lucent(["I am Skippy the Magnificent", "I tolerate humans"]) as lucent:
        monkeypatch.setattr(bootstrap_loader, "LUCENT_URL", lucent.url)
        block = bootstrap_loader._fetch_soul("some-uuid", "skippy")

    assert block == (
        "<soul>\nI am Skippy the Magnificent\nI tolerate humans\n</soul>"
    )


def test_r1_the_mind_name_is_capitalised_in_the_body_and_absent_from_the_address(
    monkeypatch,
):
    from comms import bootstrap_loader

    with _Lucent(["a value"]) as lucent:
        monkeypatch.setattr(bootstrap_loader, "LUCENT_URL", lucent.url)
        bootstrap_loader._fetch_soul("some-uuid", "skippy")

    assert lucent.requests[0]["path"] == "/graph/query"
    assert lucent.requests[0]["body"]["names"] == ["Skippy"]
