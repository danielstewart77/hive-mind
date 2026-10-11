"""The container minds' own rotation checks: Ada and Bob on Claude, Nagatha and
Bilby on Codex.

Each mind owns its copy of the check, untracked like every deployment mind's
folder, so these tests import each copy by path and skip a mind this host
does not carry. What they guard is the measurement every copy shares: a size
from this turn's last call or an estimate past it, the window the proxy lists
for the turn's own model, a threshold that is the one the mind server spawned
the process with and none when it named none, a report that leaves an unknown
window or threshold out, an overflowed turn left to the gateway, and a
missing-count notice raised only for a final reply that carried no count.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

MINDS = Path(__file__).resolve().parents[2] / "minds"
COPIES = {
    "ada": ("claude", MINDS / "ada" / ".claude" / "hooks" / "rotation_check.py"),
    "bob": ("claude", MINDS / "bob" / ".claude" / "hooks" / "rotation_check.py"),
    "nagatha": ("codex", MINDS / "nagatha" / ".codex" / "hooks" / "rotation_check.py"),
    "bilby": ("codex", MINDS / "bilby" / ".codex" / "hooks" / "rotation_check.py"),
}


def _minds(harness: str | None = None) -> list:
    return [
        pytest.param(name, marks=pytest.mark.skipif(
            not path.is_file(), reason=f"{name}'s hooks live only on the host that runs it"))
        for name, (h, path) in COPIES.items() if harness in (None, h)
    ]


class _Proxy:
    """The proxy's model listing and the gateway's context route, at urlopen."""

    def __init__(self, windows: dict[str, int]):
        self.windows = windows
        self.listings: list[str] = []
        self.reports: list[dict] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        if "/v1/models" in url:
            self.listings.append(url)
            body = {"data": [{"id": m, "context_window": w} for m, w in self.windows.items()]}
        elif url.endswith("/sessions/context"):
            self.reports.append(json.loads(req.data))
            body = {"ok": True}
        else:
            raise AssertionError(f"unexpected request {url}")
        payload = json.dumps(body).encode()

        class _Resp:
            status = 200

            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp()


@pytest.fixture
def hook(tmp_path, monkeypatch):
    """Load one mind's copy with a fake proxy and no spawned threshold."""

    def load(name: str, windows: dict[str, int]):
        _, path = COPIES[name]
        spec = importlib.util.spec_from_file_location(f"{name}_rotation_check", path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        root = tmp_path / name
        root.mkdir()
        (root / "runtime.yaml").write_text(f"name: {name}\n")
        monkeypatch.setattr(module, "MIND_ROOT", root)
        monkeypatch.setattr(module, "LOG_DIR", tmp_path / "log")
        monkeypatch.setattr(module, "NOTICES_FILE", tmp_path / "log" / "notices.json")
        if hasattr(module, "_SPAWN_MODEL_ARG"):
            monkeypatch.setattr(module, "_SPAWN_MODEL_ARG", "")
        monkeypatch.setenv("INFERENCE_PROXY_URL", "http://proxy.test")
        monkeypatch.setenv("MIND_PROXY_KEY", "hmp-test")
        monkeypatch.delenv("HIVE_ROTATION_THRESHOLD_TOKENS", raising=False)
        proxy = _Proxy(windows)
        monkeypatch.setattr(module.urllib.request, "urlopen", proxy)
        return module, proxy

    return load


def _claude_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path


def _claude_reply(model: str, usage: dict, text: str = "ok") -> dict:
    return {"type": "assistant", "message": {
        "role": "assistant", "model": model, "usage": usage,
        "content": [{"type": "text", "text": text}]}}


def _codex_rollout(path: Path, model: str, total: int, after: list[dict] | None = None) -> Path:
    lines = [
        {"type": "turn_context", "payload": {"model": model}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "ok"}]}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": total - 100, "output_tokens": 100,
                                 "total_tokens": total},
            "total_token_usage": {"total_tokens": 19_000_000}}}},
        *(after or []),
    ]
    path.write_text("\n".join(json.dumps(e) for e in lines) + "\n")
    return path


@pytest.mark.parametrize("name", _minds("claude"))
def test_a_claude_count_followed_by_more_content_is_an_estimate_past_it(hook, tmp_path, name):
    """A count is the conversation's size only when nothing came after it: a
    tool result written since is in the context too."""
    check, _ = hook(name, {})
    log = _claude_transcript(tmp_path / "t.jsonl", [
        _claude_reply("claude-opus-5", {"input_tokens": 100_000, "output_tokens": 10}),
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "x" * 40_000}]}},
    ])

    measured = check._measure(log, 4)

    assert measured["kind"] == "estimated"
    assert measured["tokens"] >= 100_010 + 10_000


@pytest.mark.parametrize("name", _minds("codex"))
def test_a_codex_turn_is_sized_by_its_last_call_not_the_sessions_running_total(
    hook, tmp_path, name,
):
    check, _ = hook(name, {})
    log = _codex_rollout(tmp_path / "rollout.jsonl", "gpt-5.6-terra", 64_000)

    measured = check._measure(log, 4)

    assert (measured["tokens"], measured["kind"]) == (64_000, "usage")


def _notices(check, monkeypatch) -> list[str]:
    """What the hook's missing-count notice sent, caught at the notify tool."""
    sent: list[str] = []
    monkeypatch.setattr(check, "_notify", lambda message: sent.append(message) or True)
    return sent


def _notice(check, log: Path) -> None:
    check._notice_missing_count(conversation_id="conv-1234abcd", model="m",
                                measured=check._measure(log, 4))


def _claude_uncounted_turn(path: Path) -> Path:
    return _claude_transcript(path, [
        _claude_reply("m", {"input_tokens": 100_000, "output_tokens": 10}),
        {"type": "user", "message": {"role": "user", "content": "and then?"}},
        _claude_reply("m", {"input_tokens": 0, "output_tokens": 0}),
    ])


def _claude_counted_then_more(path: Path) -> Path:
    return _claude_transcript(path, [
        _claude_reply("m", {"input_tokens": 100_000, "output_tokens": 10}),
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "x" * 4_000}]}},
    ])


@pytest.mark.parametrize("name", _minds("claude"))
def test_a_claude_final_reply_that_was_counted_is_not_reported_missing_for_what_followed_it(
    hook, tmp_path, monkeypatch, name,
):
    """The count is there; a tool result after it makes the size an estimate,
    not the model's count a missing one."""
    check, _ = hook(name, {})
    sent = _notices(check, monkeypatch)

    _notice(check, _claude_counted_then_more(tmp_path / "t.jsonl"))

    assert sent == []


@pytest.mark.parametrize("name", _minds("claude"))
def test_a_claude_final_reply_with_no_count_is_reported_again_after_a_counted_one_rearms(
    hook, tmp_path, monkeypatch, name,
):
    check, _ = hook(name, {})
    sent = _notices(check, monkeypatch)
    log = tmp_path / "t.jsonl"

    _notice(check, _claude_uncounted_turn(log))
    _notice(check, _claude_counted_then_more(log))
    _notice(check, _claude_uncounted_turn(log))

    assert len(sent) == 2
    assert "gave no token count for conversation conv-123" in sent[1]


def _codex_item() -> dict:
    return {"type": "response_item", "payload": {
        "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "x" * 4_000}]}}


def _codex_uncounted_turn(path: Path) -> Path:
    return _codex_rollout(path, "m", 64_000, after=[
        {"type": "turn_context", "payload": {"model": "m"}},
        _codex_item(),
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"total_tokens": 0}}}},
    ])


@pytest.mark.parametrize("name", _minds("codex"))
def test_a_codex_turn_whose_last_call_was_counted_is_not_reported_missing_for_what_followed_it(
    hook, tmp_path, monkeypatch, name,
):
    check, _ = hook(name, {})
    sent = _notices(check, monkeypatch)

    _notice(check, _codex_rollout(tmp_path / "r.jsonl", "m", 64_000, after=[_codex_item()]))

    assert sent == []


@pytest.mark.parametrize("name", _minds("codex"))
def test_a_codex_turn_whose_last_call_had_no_count_is_reported_again_after_a_counted_one_rearms(
    hook, tmp_path, monkeypatch, name,
):
    check, _ = hook(name, {})
    sent = _notices(check, monkeypatch)
    log = tmp_path / "r.jsonl"

    _notice(check, _codex_uncounted_turn(log))
    _notice(check, _codex_rollout(log, "m", 64_000, after=[_codex_item()]))
    _notice(check, _codex_uncounted_turn(log))

    assert len(sent) == 2
    assert "gave no token count for conversation conv-123" in sent[1]


@pytest.mark.parametrize("name", _minds())
def test_the_threshold_is_the_spawned_one_against_the_turns_own_models_window(
    hook, tmp_path, name, monkeypatch,
):
    """The share belongs to the model, so the mind server reads it off the
    proxy's row and spawns the process with the product. The window is the
    listed one for the model the transcript says the turn ran on."""
    harness, _ = COPIES[name]
    if harness == "claude":
        check, proxy = hook(name, {"claude-sonnet-5": 200_000})
        log = _claude_transcript(tmp_path / "t.jsonl", [
            _claude_reply("claude-sonnet-5", {"input_tokens": 50_000, "output_tokens": 10})])
    else:
        check, proxy = hook(name, {"gpt-5.6-terra": 400_000})
        log = _codex_rollout(tmp_path / "rollout.jsonl", "gpt-5.6-terra", 50_000)
    monkeypatch.setenv("HIVE_ROTATION_THRESHOLD_TOKENS", "90000")

    assessed = check._assess(check._cfg(), {"session_id": "conv-1"}, log)

    assert (assessed["window"], assessed["threshold"]) == (
        proxy.windows[assessed["model"]], 90_000)
    assert proxy.listings[-1].endswith(f"harness={harness}")


@pytest.mark.parametrize("name", _minds())
def test_a_process_spawned_with_no_threshold_does_not_rotate_and_reports_none(
    hook, tmp_path, name,
):
    """The proxy listed the model's window but declared no share of it. No
    percentage stands in for the missing one: the window is reported, the
    threshold is left out, and the turn does not rotate however full it is."""
    harness, _ = COPIES[name]
    check, proxy = hook(name, {"glm-5": 128_000})
    if harness == "claude":
        log = _claude_transcript(tmp_path / "t.jsonl", [
            _claude_reply("glm-5", {"input_tokens": 127_000, "output_tokens": 10})])
    else:
        log = _codex_rollout(tmp_path / "rollout.jsonl", "glm-5", 127_000)
    cfg = check._cfg()
    assessed = check._assess(cfg, {"session_id": "conv-1"}, log)

    check._report_context(cfg, client_type="telegram", client_ref="chat-1",
                          conversation_id="conv-1", assessment=assessed)

    report = proxy.reports[-1]
    assert assessed["rotate"] is False
    assert report["window"] == 128_000
    assert "threshold" not in report


@pytest.mark.parametrize("name", _minds())
def test_an_unknown_window_is_left_out_of_the_report_not_sent_as_zero(
    hook, tmp_path, name, monkeypatch,
):
    harness, _ = COPIES[name]
    check, proxy = hook(name, {})
    if harness == "claude":
        log = _claude_transcript(tmp_path / "t.jsonl", [
            _claude_reply("mystery", {"input_tokens": 50_000, "output_tokens": 10})])
    else:
        log = _codex_rollout(tmp_path / "rollout.jsonl", "mystery", 50_000)
    cfg = check._cfg()

    check._report_context(cfg, client_type="telegram", client_ref="chat-1",
                          conversation_id="conv-1",
                          assessment=check._assess(cfg, {"session_id": "conv-1"}, log))

    report = proxy.reports[-1]
    assert report["conversation_id"] == "conv-1" and report["tokens"] > 0
    assert "window" not in report and "threshold" not in report


@pytest.mark.parametrize("name", _minds())
def test_a_turn_that_failed_on_overflow_is_left_to_the_gateway(hook, tmp_path, name):
    """The gateway rotates it at once and re-sends the message; a rotation
    composed here as well would race it."""
    check, _ = hook(name, {})
    event = {"session_id": "conv-1", "hook_event_name": "StopFailure",
             "error": "Prompt is too long"}

    assert check._assess(check._cfg(), event, tmp_path / "absent.jsonl") is None
