"""P2: the real DSH loader and inference adapter consume the generated profile.

Run with DSH_TEST_TREE naming a built tree, in a disposable container with
no live credentials or network access. The only provider is loopback.
"""
import json
import os
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from minds.harness import dsh_profile
from minds.provider_catalog import _native_metadata


@pytest.mark.parametrize("kind,api,token", [("anthropic", "anthropic", "mind-owned-test-token"), ("custom", "openai", "mind-owned-test-token"), ("custom", "openai", ""), ("custom", "anthropic", "")])
def test_dsh_uses_selected_provider_protocol_and_minds_token(tmp_path, monkeypatch, kind, api, token):
    tree = Path(os.environ["DSH_TEST_TREE"])
    monkeypatch.setenv("DSH_BIN", str(tree / "apps/cli/lib/bin.js"))
    models = _native_metadata("anthropic") if kind == "anthropic" else {}
    model = next(iter(models)) if models else "custom-model"
    reached = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            supplied = self.headers.get("x-api-key", "") if api == "anthropic" else self.headers.get("Authorization", "").removeprefix("Bearer ")
            expected = "/v1/messages" if api == "anthropic" else "/v1/chat/completions"
            reached.append({"path": self.path, "model": body.get("model"), "token": supplied})
            if self.path != expected or body.get("model") != model or supplied != token:
                self.send_response(403); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if api == "anthropic":
                events = [
                    ("message_start", {"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 0}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "provider-connected"}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}}),
                    ("message_stop", {"type": "message_stop"}),
                ]
                for event, data in events:
                    self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
            else:
                for delta, finish in [({"role": "assistant", "content": "provider-connected"}, None), ({}, "stop")]:
                    data = {"id": "chat-test", "object": "chat.completion.chunk", "created": 1, "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                    self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = f"http://127.0.0.1:{server.server_port}"
        folder = tmp_path / "direct"
        folder.mkdir()
        (folder / "runtime.yaml").write_text(yaml.safe_dump({"provider_connection": {"kind": kind, "api_format": api, "base_url": root, "name": "Direct test provider"}}))
        dsh_profile.write_profile("direct", tmp_path, dsh_root=str(tree))
        task = tmp_path / "task.txt"
        task.write_text("Say provider-connected. Do not use any tools.")
        env = dict(os.environ, DSH_HOME=str(folder / ".dsh"), DSH_MODEL=model,
                   DSH_PROXY_BASE_URL=root if api == "anthropic" else root + "/v1",
                   HIVE_PROVIDER_TOKEN=token, DSH_MODEL_CONTEXT_WINDOW="200000", DSH_HOOKS_CONFIG="")
        result = subprocess.run([str(tree / "apps/cli/lib/bin.js"), "--profile", "hive", "--session-id", str(uuid.uuid4()), "--task-file", str(task)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=45)
        assert reached, result.stderr[-3000:] + result.stdout[-3000:]
        assert result.returncode == 0, result.stderr[-3000:] + result.stdout[-3000:]
        assert json.loads(result.stdout.strip().splitlines()[-1])["outcome"] == "completed"
        assert all(request["token"] == token for request in reached)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
