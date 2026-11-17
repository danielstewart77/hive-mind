"""The stateless provider_pressure tool: the dashboard's allowance figures, for Stash."""

import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = _PROJECT_ROOT / "tools/stateless/provider_pressure/provider_pressure.py"

_spec = importlib.util.spec_from_file_location("provider_pressure", SCRIPT)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

NOW = 1_800_000_000.0
LATER = NOW + 3600
EARLIER = NOW - 3600


def _provider(name, host, windows, reporting=True, stale=False):
    return {"name": name, "label": name, "host": host, "reporting": reporting,
            "windows": windows, "observed_at": NOW - 10, "age_seconds": 10.0,
            "stale": stale}


def _win(label, used, resets_at=LATER):
    return {"label": label, "used_percent": used, "resets_at": resets_at,
            "window_minutes": 300}


def _band_of(used):
    report = pp.summarise(
        {"providers": [_provider("anthropic", "api.anthropic.com", [_win("5h", used)])]},
        NOW)
    return report["providers"][0]["windows"][0]["band"]


def test_bands_split_at_the_dashboards_sixty_and_eighty_five():
    assert _band_of(59.9) == "ok"
    assert _band_of(60) == "warn"
    assert _band_of(85) == "warn"
    assert _band_of(85.1) == "hot"


def test_a_window_whose_reset_has_passed_neither_bands_the_provider_nor_approaches():
    report = pp.summarise({"providers": [_provider("openai", "chatgpt.com", [
        _win("5h", 100.0, resets_at=EARLIER), _win("7d", 16.0)])]}, NOW)
    openai = report["providers"][0]
    assert [w["reset_passed"] for w in openai["windows"]] == [True, False]
    assert openai["band"] == "ok"
    assert report["approaching"] == []
    assert report["worst"] == "ok"


def test_a_stale_reading_whose_window_has_not_reset_still_counts():
    report = pp.summarise({"providers": [
        _provider("openai", "chatgpt.com", [_win("7d", 90.0)], stale=True)]}, NOW)
    assert report["providers"][0]["band"] == "hot"
    assert report["approaching"] == [{"provider": "openai", "window": "7d",
                                      "used_percent": 90.0, "band": "hot",
                                      "resets_at": LATER, "stale": True,
                                      "age_seconds": 10.0}]


def test_a_provider_that_has_not_reported_is_unknown_not_headroom():
    report = pp.summarise({"providers": [
        _provider("anthropic", "api.anthropic.com", [], reporting=False)]}, NOW)
    assert report["providers"][0]["band"] == "unknown"
    assert report["worst"] == "unknown"


def test_an_unreported_provider_is_named_even_when_the_other_is_known():
    report = pp.summarise({"providers": [
        _provider("anthropic", "api.anthropic.com", [_win("7d", 1.0)]),
        _provider("openai", "chatgpt.com", [], reporting=False)]}, NOW)
    assert report["worst"] == "ok"
    assert report["unknown"] == ["openai"]


def test_a_reading_older_than_its_window_with_no_reset_time_has_reset():
    row = _provider("openai", "chatgpt.com", [_win("5h", 95.0, resets_at=None)])
    row["age_seconds"] = 300 * 60 + 1
    report = pp.summarise({"providers": [row]}, NOW)
    assert report["providers"][0]["windows"][0]["reset_passed"] is True
    assert report["providers"][0]["band"] == "unknown"


def test_a_window_with_no_reset_time_still_counts_and_says_it_does_not_know():
    report = pp.summarise({"providers": [_provider(
        "openai", "chatgpt.com", [_win("7d", 90.0, resets_at=None)])]}, NOW)
    assert report["providers"][0]["windows"][0]["reset_passed"] is None
    assert report["providers"][0]["band"] == "hot"


def test_malformed_rows_are_dropped_rather_than_crashing():
    report = pp.summarise({"providers": [
        "junk",
        _provider("anthropic", "api.anthropic.com", {"5h": 50}),
        _provider("openai", "chatgpt.com", [_win("5h", None), _win("7d", "70")]),
    ]}, NOW)
    assert [(p["name"], p["band"], p["windows"]) for p in report["providers"]] == [
        ("anthropic", "unknown", []), ("openai", "unknown", [])]


def test_worst_is_the_hottest_hosted_provider_and_local_ones_are_dropped():
    report = pp.summarise({"providers": [
        _provider("anthropic", "api.anthropic.com", [_win("7d", 70.0)]),
        _provider("openai", "chatgpt.com", [_win("7d", 10.0)]),
        _provider("ollama", "127.0.0.1", [_win("5h", 99.0)]),
        _provider("lan", "http://192.168.1.20:11434", [_win("5h", 99.0)]),
    ]}, NOW)
    assert [p["name"] for p in report["providers"]] == ["anthropic", "openai"]
    assert report["worst"] == "warn"
    assert [a["provider"] for a in report["approaching"]] == ["anthropic"]


def test_hot_outranks_warn_across_windows_and_across_providers():
    report = pp.summarise({"providers": [
        _provider("anthropic", "api.anthropic.com", [_win("5h", 10.0), _win("7d", 90.0)]),
        _provider("openai", "chatgpt.com", [_win("7d", 70.0)]),
    ]}, NOW)
    assert [p["band"] for p in report["providers"]] == ["hot", "warn"]
    assert report["worst"] == "hot"


def test_a_provider_not_reporting_is_unknown_even_if_windows_came_with_it():
    report = pp.summarise({"providers": [_provider(
        "openai", "chatgpt.com", [_win("7d", 95.0)], reporting=False)]}, NOW)
    assert report["providers"][0]["band"] == "unknown"
    assert report["approaching"] == []


def test_providers_named_by_a_local_hostname_are_dropped():
    report = pp.summarise({"providers": [
        _provider("a", "localhost:11434", [_win("5h", 99.0)]),
        _provider("b", "[::1]:11434", [_win("5h", 99.0)]),
        _provider("c", "http://gpu-box.local/v1", [_win("5h", 99.0)]),
        _provider("openai", "chatgpt.com", [_win("5h", 1.0)]),
    ]}, NOW)
    assert [p["name"] for p in report["providers"]] == ["openai"]


def test_a_window_resetting_exactly_now_has_reset():
    report = pp.summarise({"providers": [_provider(
        "openai", "chatgpt.com", [_win("5h", 100.0, resets_at=NOW)])]}, NOW)
    assert report["providers"][0]["windows"][0]["reset_passed"] is True
    assert report["providers"][0]["band"] == "unknown"


def test_non_numbers_posing_as_percentages_are_dropped():
    report = pp.summarise({"providers": [_provider("openai", "chatgpt.com", [
        _win("a", True), _win("b", float("nan")), _win("c", float("inf")),
        _win("d", 10 ** 400)])]}, NOW)
    assert report["providers"][0]["windows"] == []


def test_a_reply_without_a_providers_list_is_refused():
    with pytest.raises(pp.Failed) as caught:
        pp.summarise({"detail": "nope"}, NOW)
    assert caught.value.kind == "BadReply"


class _Proxy:
    """A real HTTP server standing in for the proxy: the transport is the edge."""

    def __init__(self, status=200, body=None, headers=None):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append(("GET", self.path,
                                       self.headers.get("Authorization")))
                payload = json.dumps(body if body is not None else {"providers": []})
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload.encode())

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def _run(env):
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True,
                            text=True, timeout=20, env={"PATH": "/usr/bin:/bin", **env})
    return result.returncode, json.loads(result.stdout)


def test_it_gets_the_limits_route_with_the_minds_proxy_key_under_v1_base():
    proxy = _Proxy(body={"providers": [
        _provider("anthropic", "api.anthropic.com", [_win("5h", 88.0, resets_at=4e9)])]})
    try:
        code, out = _run({"OPENAI_BASE_URL": proxy.url + "/v1",
                          "HIVE_PROXY_KEY": "hmp-test"})
    finally:
        proxy.close()
    assert code == 0
    assert proxy.requests == [("GET", "/v1/providers/limits", "Bearer hmp-test")]
    assert out["worst"] == "hot"


def test_a_refused_key_exits_one_with_a_json_error():
    proxy = _Proxy(status=401, body={"detail": "Invalid API key"})
    try:
        code, out = _run({"INFERENCE_PROXY_URL": proxy.url,
                          "HIVE_PROXY_KEY": "wrong"})
    finally:
        proxy.close()
    assert code == 1
    assert out["kind"] == "Unauthorized"


def test_no_key_in_the_environment_exits_one_without_calling_anything():
    proxy = _Proxy()
    try:
        code, out = _run({"INFERENCE_PROXY_URL": proxy.url})
    finally:
        proxy.close()
    assert code == 1
    assert out["kind"] == "NotConfigured"
    assert proxy.requests == []


def test_a_garbled_http_reply_is_a_json_error_not_a_traceback():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def answer():
        conn, _ = listener.accept()
        conn.recv(4096)
        conn.sendall(b"garbage\r\n\r\n")
        conn.close()

    threading.Thread(target=answer, daemon=True).start()
    try:
        code, out = _run({"INFERENCE_PROXY_URL": f"http://127.0.0.1:{listener.getsockname()[1]}",
                          "HIVE_PROXY_KEY": "k"})
    finally:
        listener.close()
    assert code == 1
    assert out["kind"] == "Unreachable"


def test_a_redirect_is_refused_and_the_key_never_follows_it():
    elsewhere = _Proxy()
    proxy = _Proxy(status=302, headers={"Location": elsewhere.url + "/v1/providers/limits"})
    try:
        code, out = _run({"INFERENCE_PROXY_URL": proxy.url, "HIVE_PROXY_KEY": "k"})
    finally:
        proxy.close()
        elsewhere.close()
    assert code == 1
    assert out["kind"] == "HTTPError"
    assert elsewhere.requests == []


def test_a_proxy_trickling_its_reply_is_cut_off_at_the_timeout():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def trickle():
        conn, _ = listener.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
        try:
            for _ in range(60):
                conn.sendall(b" ")
                time.sleep(0.5)
        except OSError:
            pass
        conn.close()

    threading.Thread(target=trickle, daemon=True).start()
    started = time.monotonic()
    try:
        code, out = _run({"INFERENCE_PROXY_URL": f"http://127.0.0.1:{listener.getsockname()[1]}",
                          "HIVE_PROXY_KEY": "k"})
    finally:
        listener.close()
    assert code == 1
    assert out["kind"] == "Unreachable"
    assert time.monotonic() - started < pp.TIMEOUT_SECONDS + 5


def test_a_key_the_request_cannot_carry_is_a_json_error():
    code, out = _run({"INFERENCE_PROXY_URL": "http://127.0.0.1:9", "HIVE_PROXY_KEY": "a\nb"})
    assert code == 1
    assert out["kind"] == "NotConfigured"
