"""B4: a refused or interrupted request is never shown as a completed answer.

The real GatewayClient reads a faked transport (the gateway's SSE body or
status), and the real Telegram and Discord stream functions and handlers draw
what the operator sees. Asserted: what the screen finally says, and whether
anything was handed to text-to-speech.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import hive_surfaces.discord_bot as db
import hive_surfaces.telegram_bot as tb
from hive_surfaces.config import SurfaceConfig, configure
from hive_surfaces.gateway_client import GatewayClient


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def _text(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _client(body: bytes = b"", status: int = 200, error_body=None) -> GatewayClient:
    """A real client whose only fake is the HTTP transport."""
    async def iter_any():
        yield body

    resp = MagicMock()
    resp.status = status
    resp.content.iter_any = iter_any
    resp.json = AsyncMock(return_value=error_body)
    resp.text = AsyncMock(return_value="")
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    client = GatewayClient(
        http=MagicMock(), server_url="http://gw", surface="test", mind_id="m",
    )
    client.ensure_session = AsyncMock(return_value="s1")
    client.http.post = MagicMock(return_value=ctx)
    return client


# What the gateway really sends in each case that must not look like an answer.
CASES = {
    "error result after partial text": dict(
        body=_sse(_text("Half an answ"),
                  {"type": "result", "is_error": True, "errors": ["roles refused this"]}),
        reason="roles refused this", partial="Half an answ"),
    "error result with no text": dict(
        body=_sse({"type": "result", "subtype": "error", "is_error": True,
                   "result": "Mind 'm' refused this gateway's credential."}),
        reason="refused this gateway's credential", partial=""),
    "stream ends with no finishing event": dict(
        body=_sse(_text("Half an answ")), reason="without a finishing event",
        partial="Half an answ"),
    "dsh turn stopped by an interrupt": dict(
        body=_sse(_text("Half an answ"),
                  {"type": "result", "stop_reason": "stopped", "is_error": False}),
        reason="stopped before it finished", partial="Half an answ"),
    "gateway refuses the message": dict(
        body=b"", status=403, error_body={"detail": "not allowed here"},
        reason="not allowed here", partial=""),
}


class _TgMessage:
    def __init__(self, bubbles, text):
        self.bubbles, self.edits = bubbles, [text]
        bubbles.append(self)

    async def edit_text(self, text):
        self.edits.append(text)

    async def reply_text(self, text):
        return _TgMessage(self.bubbles, text)


@pytest.mark.parametrize("name", CASES)
@pytest.mark.asyncio
async def test_telegram_marks_the_turn_and_does_not_speak_it(name, monkeypatch):
    case = CASES[name]
    client = _client(case["body"], case.get("status", 200), case.get("error_body"))
    spoken = []
    monkeypatch.setattr(tb, "gateway", client)
    monkeypatch.setattr(tb, "_tts", AsyncMock(side_effect=lambda t: spoken.append(t) or b"x"))
    bubbles: list = []
    sent = _TgMessage(bubbles, "\u2026")
    chat = MagicMock(send_voice=AsyncMock())

    try:
        await tb._stream_to_message(sent, 1, 2, "hi", edit_interval=0, voice=True, chat=chat)
    except RuntimeError:
        pass  # a refused message raises, as before; the screen is what matters
    await __import__("asyncio").sleep(0)

    shown = "\n".join(b.edits[-1] for b in bubbles)
    assert case["reason"] in shown
    assert any(w in shown for w in ("Refused", "Interrupted", "Error"))
    assert spoken == [] and chat.send_voice.await_count == 0


@pytest.mark.asyncio
async def test_telegram_still_speaks_a_finished_turn(monkeypatch):
    client = _client(_sse(_text("All done."), {"type": "result", "result": "All done."}))
    spoken = []
    monkeypatch.setattr(tb, "gateway", client)
    monkeypatch.setattr(tb, "_tts", AsyncMock(side_effect=lambda t: spoken.append(t) or b"x"))
    bubbles: list = []
    chat = MagicMock(send_voice=AsyncMock())

    await tb._stream_to_message(_TgMessage(bubbles, "\u2026"), 1, 2, "hi",
                                edit_interval=0, voice=True, chat=chat)
    await __import__("asyncio").sleep(0.01)

    assert bubbles[0].edits[-1] == "All done."
    assert spoken == ["All done."]


def _interaction():
    interaction = MagicMock()
    interaction.user.id = 7
    interaction.channel_id = 2
    interaction.response.defer = AsyncMock()
    sent = MagicMock()
    sent.edit = AsyncMock()
    sent.channel.send = AsyncMock()
    sent.channel.typing.return_value.__aenter__ = AsyncMock()
    sent.channel.typing.return_value.__aexit__ = AsyncMock(return_value=False)
    interaction.followup.send = AsyncMock(return_value=sent)
    return interaction, sent


async def _run_discord_skill(client):
    interaction, sent = _interaction()
    configure(SurfaceConfig(discord_allowed_users=[7]))
    speak, play = MagicMock(), AsyncMock()
    with patch.object(db, "gateway", client), \
            patch.object(db, "speak_reply", speak), \
            patch.object(db, "_play_tts_for_member", play):
        try:
            await db.bot.tree.get_command("skill").callback(interaction, name="x", args=None)
        except (RuntimeError, ConnectionError):
            pass
    return sent, speak, play


@pytest.mark.parametrize("name", CASES)
@pytest.mark.asyncio
async def test_discord_marks_the_turn_and_does_not_speak_it(name):
    case = CASES[name]
    client = _client(case["body"], case.get("status", 200), case.get("error_body"))

    sent, speak, play = await _run_discord_skill(client)

    shown = sent.edit.await_args.kwargs["content"]
    assert case["reason"] in shown
    assert any(w in shown for w in ("Refused", "Interrupted", "Error"))
    assert speak.call_count == 0 and play.await_count == 0


@pytest.mark.asyncio
async def test_discord_still_speaks_a_finished_turn():
    client = _client(_sse(_text("All done."), {"type": "result", "result": "All done."}))

    sent, speak, play = await _run_discord_skill(client)

    assert sent.edit.await_args.kwargs["content"] == "All done."
    speak.assert_called_once()
    assert speak.call_args.args[1] == "All done."


@pytest.mark.asyncio
async def test_query_raises_for_a_turn_that_did_not_finish():
    from hive_surfaces.gateway_client import GatewayTurnError

    client = _client(_sse(_text("Half"), {"type": "result", "is_error": True, "errors": ["boom"]}))

    with pytest.raises(GatewayTurnError, match="boom"):
        await client.query(1, 2, "hi")


@pytest.mark.asyncio
async def test_a_refused_message_is_shown_as_refused_not_interrupted():
    client = _client(b"", 403, {"detail": "not allowed here"})

    sent, _speak, _play = await _run_discord_skill(client)

    shown = sent.edit.await_args.kwargs["content"]
    assert "Refused: " in shown and "Interrupted" not in shown


@pytest.mark.asyncio
async def test_discord_long_partial_answer_cut_off_shows_the_mark_in_a_message():
    async def iter_any():
        yield _sse(_text("x" * 2500))
        raise ConnectionError("reset")

    client = _client(b"")
    ctx = client.http.post.return_value
    resp = await ctx.__aenter__()
    resp.content.iter_any = iter_any

    sent, speak, play = await _run_discord_skill(client)

    texts = [c.kwargs["content"] for c in sent.edit.await_args_list]
    texts += [c.args[0] for c in sent.channel.send.await_args_list]
    assert any("Interrupted" in t and "reset" in t for t in texts)
    assert speak.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, said", [
    ({"error": "Session not found"}, "Could not interrupt: Session not found"),
    ({"detail": "no"}, "Could not interrupt: no"),
    ({"ok": False}, "Could not interrupt"),
    (None, "Could not interrupt: unexpected answer"),
    ({"ok": True, "message": "nothing_running"}, "Nothing running."),
    ({"ok": True}, "Interrupted."),
])
async def test_telegram_stop_words_each_outcome(answer, said):
    update = MagicMock()
    update.effective_user.id, update.effective_chat.id = 123, 456
    update.message.reply_text = AsyncMock()
    gw = MagicMock(find_active_session=AsyncMock(return_value="s1"),
                   interrupt_session=AsyncMock(return_value=answer))
    configure(SurfaceConfig(telegram_allowed_users=[123]))
    with patch.object(tb, "gateway", gw), \
            patch.object(tb, "_auth_check", AsyncMock(return_value=True)):
        await tb.cmd_stop(update, MagicMock())

    assert said in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, said", [
    ({"error": "Session not found"}, "Could not interrupt: Session not found"),
    ({"detail": "no"}, "Could not interrupt: no"),
    ({"ok": False}, "Could not interrupt"),
    (None, "Could not interrupt: unexpected answer"),
    ({"ok": True, "message": "nothing_running"}, "Nothing running."),
    ({"ok": True}, "Interrupted."),
])
async def test_discord_stop_words_each_outcome(answer, said):
    interaction, _ = _interaction()
    interaction.followup.send = AsyncMock()
    configure(SurfaceConfig(discord_allowed_users=[7]))
    gw = MagicMock(find_active_session=AsyncMock(return_value="s1"),
                   interrupt_session=AsyncMock(return_value=answer))
    with patch.object(db, "gateway", gw):
        await db.bot.tree.get_command("stop").callback(interaction)

    assert said in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["/new", "/clear", "/switch 3"])
async def test_discord_refused_command_is_not_reported_as_success(cmd):
    gw = MagicMock(server_command=AsyncMock(return_value={"detail": "Not authenticated"}))
    with patch.object(db, "gateway", gw):
        out = await db._handle_server_command(cmd, 7, 2)

    assert out == "Error: Not authenticated"


@pytest.mark.asyncio
async def test_discord_sessions_listing_still_works():
    gw = MagicMock(server_command=AsyncMock(return_value=[]))
    with patch.object(db, "gateway", gw):
        out = await db._handle_server_command("/sessions", 7, 2)

    assert not out.startswith("Error")


@pytest.mark.asyncio
async def test_discord_stop_reports_a_gateway_that_could_not_be_reached():
    interaction, _ = _interaction()
    interaction.followup.send = AsyncMock()
    configure(SurfaceConfig(discord_allowed_users=[7]))
    gw = MagicMock(find_active_session=AsyncMock(side_effect=ConnectionError("down")))
    with patch.object(db, "gateway", gw):
        await db.bot.tree.get_command("stop").callback(interaction)

    assert "Could not interrupt" in interaction.followup.send.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_stop_reports_a_gateway_that_could_not_be_reached():
    update = MagicMock()
    update.effective_user.id, update.effective_chat.id = 123, 456
    update.message.reply_text = AsyncMock()
    gw = MagicMock(find_active_session=AsyncMock(side_effect=ConnectionError("down")))
    configure(SurfaceConfig(telegram_allowed_users=[123]))
    with patch.object(tb, "gateway", gw), \
            patch.object(tb, "_auth_check", AsyncMock(return_value=True)):
        await tb.cmd_stop(update, MagicMock())

    assert "Could not interrupt" in update.message.reply_text.await_args.args[0]
