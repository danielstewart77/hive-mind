"""A picker is used once, and a tap is never silent.

Requirements 8 through 15.

On 2026-09-17 two `New session` taps arrived 75 seconds apart — queued at
Telegram's end while polling was down, then acted on back to back. The first
ended the conversation the chat held and created one; the second ended *that*
one and created another. Neither reply had arrived to say the first had
worked, so tapping again was the only reasonable thing to do.

Two defences, tested here. The picker stops working after one tap, so a stale
tap cannot act at all; and every tap produces a sentence, retried and then
queued rather than dropped.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_callback(data: str = "new", message_id: int = 99, chat_id: int = 456):
    """A tapped inline button."""
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.message_id = message_id
    update.callback_query.message.text = "Your conversations:"
    update.callback_query.edit_message_reply_markup = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return update, context


@pytest.fixture(autouse=True)
def _patch_config():
    with patch("hive_surfaces.telegram_bot.config") as mock_config:
        mock_config.telegram_allowed_users = {123}
        yield mock_config


@pytest.fixture(autouse=True)
def _clean_pickers(tmp_path, monkeypatch):
    """Every test gets its own claim store.

    The claims are persisted so they survive a restart, which means a test that
    takes one writes a real file — and without this the next test inherits it
    and a fresh picker reads as already used.
    """
    monkeypatch.setenv("PICKER_STATE_PATH", str(tmp_path / "spent_pickers.json"))
    from hive_surfaces import bot_utils

    bot_utils._reset_pickers()
    yield
    bot_utils._reset_pickers()


class TestPickerClaim:
    def test_a_picker_can_be_claimed_once(self) -> None:
        """Requirement 8: the second tap on one picker does not get to act."""
        from hive_surfaces.bot_utils import claim_picker

        assert claim_picker(456, 99) is True
        assert claim_picker(456, 99) is False

    def test_a_fresh_picker_claims_independently(self) -> None:
        """Requirement 9: sending /sessions again gives a working list."""
        from hive_surfaces.bot_utils import claim_picker

        assert claim_picker(456, 99) is True
        assert claim_picker(456, 100) is True
        assert claim_picker(456, 99) is False

    def test_pickers_in_different_chats_do_not_collide(self) -> None:
        """Requirement 8: a claim is about one message, not one message number."""
        from hive_surfaces.bot_utils import claim_picker

        assert claim_picker(456, 99) is True
        assert claim_picker(789, 99) is True


@pytest.mark.asyncio
class TestTapBehaviour:
    async def test_tap_removes_the_keyboard(self) -> None:
        """Requirement 8: the buttons go away once one of them is used."""
        from hive_surfaces.telegram_bot import on_session_button

        update, context = _make_callback()
        with patch("hive_surfaces.telegram_bot._run_session_button", AsyncMock(return_value=(True, "New session: abcd1234"))):
            await on_session_button(update, context)

        update.callback_query.edit_message_reply_markup.assert_awaited_once()
        assert update.callback_query.edit_message_reply_markup.call_args.kwargs[
            "reply_markup"
        ] is None

    async def test_second_tap_never_reaches_the_gateway(self) -> None:
        """Requirement 12: a spent picker refuses out loud and does nothing.

        This is the test that stands for the conversation lost on 2026-09-17.
        The assertion that matters is not the wording — it is that the action
        did not run.
        """
        from hive_surfaces.telegram_bot import on_session_button, SPENT_PICKER_TEXT

        run = AsyncMock(return_value=(True, "New session: abcd1234"))
        with patch("hive_surfaces.telegram_bot._run_session_button", run):
            update, context = _make_callback()
            await on_session_button(update, context)
            again, context2 = _make_callback()  # same chat, same message id
            await on_session_button(again, context2)

        assert run.await_count == 1
        context2.bot.send_message.assert_awaited()
        # Not `SPENT_PICKER_TEXT in ...`, which is vacuously true when the
        # constant is empty — and an empty `text` is a 400 from Telegram, i.e.
        # the silent tap requirement 12 exists to prevent.
        refusal = context2.bot.send_message.call_args.kwargs["text"]
        assert refusal.strip(), "a spent picker refused silently"
        assert refusal == SPENT_PICKER_TEXT

    async def test_tap_names_the_outcome_on_the_picker(self) -> None:
        """Requirement 10: scrollback says what the tap chose."""
        from hive_surfaces.telegram_bot import on_session_button

        update, context = _make_callback(data="sw:sess-1")
        with patch("hive_surfaces.telegram_bot._run_session_button", AsyncMock(return_value=(True, 'Resumed "Dragoman"'))):
            await on_session_button(update, context)

        update.callback_query.edit_message_text.assert_awaited_once()
        assert "Dragoman" in update.callback_query.edit_message_text.call_args[0][0]

    async def test_successful_tap_delivers_the_outcome(self) -> None:
        """Requirement 11: a tap that worked says what it did."""
        from hive_surfaces.telegram_bot import on_session_button

        update, context = _make_callback()
        with patch("hive_surfaces.telegram_bot._run_session_button", AsyncMock(return_value=(True, "New session: abcd1234"))):
            await on_session_button(update, context)

        context.bot.send_message.assert_awaited()
        assert "abcd1234" in context.bot.send_message.call_args.kwargs["text"]

    async def test_failing_tap_still_delivers_a_sentence(self) -> None:
        """Requirement 11: there is no tap that produces silence."""
        from hive_surfaces.telegram_bot import on_session_button

        update, context = _make_callback()
        with patch("hive_surfaces.telegram_bot._run_session_button", AsyncMock(side_effect=RuntimeError("gateway down"))):
            await on_session_button(update, context)

        context.bot.send_message.assert_awaited()
        assert context.bot.send_message.call_args.kwargs["text"].strip()

    async def test_tap_is_acknowledged_before_the_work_runs(self) -> None:
        """Requirement 14: the tap is visibly received even if the answer is not.

        Order matters and is the whole requirement: acknowledging after the
        work means a slow or hanging action leaves the button spinning with
        nothing on screen, which is the failure being fixed.
        """
        from hive_surfaces.telegram_bot import on_session_button

        order: list[str] = []
        update, context = _make_callback()
        update.callback_query.answer = AsyncMock(side_effect=lambda *a, **k: order.append("ack"))

        async def _work(*_a, **_k):
            order.append("work")
            return True, "New session: abcd1234"

        with patch("hive_surfaces.telegram_bot._run_session_button", _work):
            await on_session_button(update, context)

        assert order == ["ack", "work"]


@pytest.mark.asyncio
class TestDeliveryIsNeverDropped:
    async def test_undeliverable_text_lands_on_the_proactive_queue(self) -> None:
        """Requirement 13: an answer arrives late rather than never."""
        from hive_surfaces import proactive
        from hive_surfaces.telegram_bot import _deliver

        proactive._reset()
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("Bad Gateway"))

        with patch("hive_surfaces.telegram_bot._DELIVER_BACKOFF_S", 0):
            delivered = await _deliver(bot, 456, 'Resumed "Dragoman"')

        assert delivered is False
        # Bounded: with the enqueue dropped, a bare `await proactive.get()`
        # blocks forever and the file reads as a hung runner rather than a
        # failing assertion.
        chat_id, text, attempts = await asyncio.wait_for(proactive.get(), timeout=2)
        assert chat_id == 456
        assert text == 'Resumed "Dragoman"'
        assert attempts == 0

    async def test_delivery_retries_before_giving_up(self) -> None:
        """Requirement 13: a blip costs a retry, not the message."""
        from hive_surfaces.telegram_bot import _deliver

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[RuntimeError("Bad Gateway"), None])

        with patch("hive_surfaces.telegram_bot._DELIVER_BACKOFF_S", 0):
            delivered = await _deliver(bot, 456, "ok")

        assert delivered is True
        assert bot.send_message.await_count == 2

    async def test_delivery_failure_is_a_warning(self, caplog) -> None:
        """Requirement 15: not swallowed at INFO, where nobody reads it."""
        import logging

        from hive_surfaces import proactive
        from hive_surfaces.telegram_bot import _deliver

        proactive._reset()
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("Bad Gateway"))

        with patch("hive_surfaces.telegram_bot._DELIVER_BACKOFF_S", 0), \
                caplog.at_level(logging.WARNING):
            await _deliver(bot, 456, 'Resumed "Dragoman"')

        assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
class TestConcurrentTaps:
    """Two taps genuinely in flight at once — the 2026-09-17 shape.

    Every other test here drives taps sequentially, so none of them exercises
    the property `claim_picker`'s docstring actually promises: that the claim
    is taken before any `await`, so two taps racing are ordered by the dict
    rather than by whichever network call returns first. A mutation moving the
    claim after the acknowledgement and the keyboard edit passed all of them.
    """

    async def test_two_simultaneous_taps_run_the_action_once(self) -> None:
        """Requirement 8: a race on one picker still ends one conversation."""
        from hive_surfaces.telegram_bot import on_session_button

        started = asyncio.Event()
        release = asyncio.Event()
        runs = 0

        async def _slow_work(*_a, **_k):
            nonlocal runs
            runs += 1
            started.set()
            await release.wait()
            return True, "New session: abcd1234"

        # Both taps carry the same picker message id, as a double-tap does.
        first, ctx1 = _make_callback()
        second, ctx2 = _make_callback()

        with patch("hive_surfaces.telegram_bot._run_session_button", _slow_work):
            task1 = asyncio.create_task(on_session_button(first, ctx1))
            await started.wait()
            # The second tap arrives while the first is still inside the work.
            task2 = asyncio.create_task(on_session_button(second, ctx2))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(task1, task2)

        assert runs == 1
        ctx2.bot.send_message.assert_awaited()

    async def test_a_tap_with_no_identifiable_picker_is_refused(self) -> None:
        """Requirement 8: the claim fails closed, not open.

        A callback whose message Telegram omitted has no id to claim against.
        Skipping the claim for it lets exactly the tap the claim cannot cover
        be the one that runs unbounded.
        """
        from hive_surfaces.telegram_bot import on_session_button

        update, context = _make_callback()
        update.callback_query.message = None
        run = AsyncMock(return_value=(True, "New session: abcd1234"))

        with patch("hive_surfaces.telegram_bot._run_session_button", run):
            await on_session_button(update, context)

        run.assert_not_awaited()
        context.bot.send_message.assert_awaited()


@pytest.mark.asyncio
class TestAcknowledgement:
    async def test_the_acknowledgement_says_something(self) -> None:
        """Requirement 14: an empty toast acknowledges nothing."""
        from hive_surfaces.telegram_bot import ACK_TEXT, on_session_button

        update, context = _make_callback()
        with patch("hive_surfaces.telegram_bot._run_session_button", AsyncMock(return_value=(True, "ok"))):
            await on_session_button(update, context)

        assert ACK_TEXT.strip()
        assert update.callback_query.answer.call_args[0][0] == ACK_TEXT

    async def test_an_expired_callback_is_acknowledged_in_the_chat(self) -> None:
        """Requirement 14: the one case the acknowledgement was written for.

        A tap queued at Telegram's end for hours arrives with an expired query
        id, so the toast is refused — which is precisely when the operator has
        been staring at nothing the longest. The chat carries it instead.
        """
        from hive_surfaces.telegram_bot import ACK_TEXT, on_session_button

        update, context = _make_callback()
        update.callback_query.answer = AsyncMock(side_effect=RuntimeError("query is too old"))

        with patch("hive_surfaces.telegram_bot._run_session_button", AsyncMock(return_value=(True, "ok"))):
            await on_session_button(update, context)

        sent = [c.kwargs["text"] for c in context.bot.send_message.await_args_list]
        assert ACK_TEXT in sent


@pytest.mark.asyncio
class TestTheMarkTellsTheTruth:
    async def test_a_failed_tap_is_not_marked_done(self) -> None:
        """Requirement 10: the picker must not read "done" over a failure.

        An unconditional tick left the message permanently saying the tap
        succeeded, in scrollback, long after the operator could check.
        """
        from hive_surfaces.telegram_bot import on_session_button

        update, context = _make_callback()
        with patch("hive_surfaces.telegram_bot._run_session_button", AsyncMock(side_effect=RuntimeError("down"))):
            await on_session_button(update, context)

        annotated = update.callback_query.edit_message_text.call_args[0][0]
        assert "✅" not in annotated

    async def test_a_gateway_error_string_is_not_marked_done(self) -> None:
        """Requirement 10: a refusal reported in the string is still a failure.

        `_handle_server_command` reports a refusing gateway by returning
        "Error: ..." rather than raising, so a mark keyed only on the exception
        would tick a failure.
        """
        from hive_surfaces.telegram_bot import on_session_button

        update, context = _make_callback()
        with patch("hive_surfaces.telegram_bot._run_session_button",
                   AsyncMock(return_value=(False, "Error: session not found"))):
            await on_session_button(update, context)

        annotated = update.callback_query.edit_message_text.call_args[0][0]
        assert "✅" not in annotated

    async def test_a_successful_tap_is_marked_done(self) -> None:
        """Requirement 10: and the mark still means something when it is there."""
        from hive_surfaces.telegram_bot import on_session_button

        update, context = _make_callback()
        with patch("hive_surfaces.telegram_bot._run_session_button", AsyncMock(return_value=(True, 'Resumed "Dragoman"'))):
            await on_session_button(update, context)

        assert "✅" in update.callback_query.edit_message_text.call_args[0][0]


class TestChunkingMatchesTelegramsCount:
    """Telegram counts UTF-16 code units; Python's `len` counts code points."""

    def test_a_chunk_of_astral_characters_fits_telegrams_limit(self) -> None:
        """Requirement 13: a message that can never be accepted is not retried.

        An emoji outside the BMP is one Python character and two of Telegram's.
        Splitting on `len` produced chunks Telegram rejects every time — which,
        behind a retrying delivery path, is minutes of retrying a rejection
        that cannot succeed, ending in a discarded answer.
        """
        from hive_surfaces.telegram_bot import TELEGRAM_MSG_LIMIT, _chunk_message, _utf16_len

        text = "\U0001f916" * 4000  # 4000 chars, 8000 UTF-16 units
        chunks = _chunk_message(text)

        assert "".join(chunks) == text
        for chunk in chunks:
            assert _utf16_len(chunk) <= TELEGRAM_MSG_LIMIT

    def test_plain_text_still_splits_at_the_limit(self) -> None:
        """Requirement 13: the common case is unchanged."""
        from hive_surfaces.telegram_bot import TELEGRAM_MSG_LIMIT, _chunk_message

        text = "x" * (TELEGRAM_MSG_LIMIT + 100)
        chunks = _chunk_message(text)

        assert len(chunks) == 2
        assert len(chunks[0]) == TELEGRAM_MSG_LIMIT
        assert "".join(chunks) == text


@pytest.mark.asyncio
class TestARetryDoesNotRepeatWhatLanded:
    async def test_a_retry_resumes_rather_than_restarting(self) -> None:
        """Requirement 13: a partial send is finished, not sent again.

        Restarting the chunk loop delivered a three-part answer as 1, 2, 1, 2,
        3 and reported success — a duplicated wall of prose mid-answer with
        nothing in the log to explain it.
        """
        from hive_surfaces.telegram_bot import TELEGRAM_MSG_LIMIT, _deliver

        text = "A" * TELEGRAM_MSG_LIMIT + "B" * TELEGRAM_MSG_LIMIT + "C" * 100
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[None, RuntimeError("Bad Gateway"), None, None])

        with patch("hive_surfaces.telegram_bot._DELIVER_BACKOFF_S", 0):
            delivered = await _deliver(bot, 456, text)

        assert delivered is True
        texts = [c.kwargs["text"] for c in bot.send_message.await_args_list]
        # The first chunk landed and is never offered again. The second appears
        # twice because its first attempt raised, and a send that raised cannot
        # be known to have arrived — that one is unavoidable; re-sending the
        # first is not.
        assert texts.count("A" * TELEGRAM_MSG_LIMIT) == 1
        assert texts == [
            "A" * TELEGRAM_MSG_LIMIT,
            "B" * TELEGRAM_MSG_LIMIT,
            "B" * TELEGRAM_MSG_LIMIT,
            "C" * 100,
        ]

    async def test_only_the_undelivered_remainder_is_queued(self) -> None:
        """Requirement 13: what the operator already has is not queued again."""
        from hive_surfaces import proactive
        from hive_surfaces.telegram_bot import TELEGRAM_MSG_LIMIT, _deliver

        proactive._reset()
        text = "A" * TELEGRAM_MSG_LIMIT + "B" * 50
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[None] + [RuntimeError("down")] * 10)

        with patch("hive_surfaces.telegram_bot._DELIVER_BACKOFF_S", 0):
            assert await _deliver(bot, 456, text) is False

        _chat, queued, _attempts = await asyncio.wait_for(proactive.get(), timeout=2)
        assert queued == "B" * 50


class TestTheClaimSurvivesARestart:
    """Requirement 8, through the restart the incident actually ran through.

    Polling goes down, taps queue at Telegram's end, and the usual way polling
    comes back is the service restarting — whereupon `getUpdates` redelivers
    every queued tap to a fresh process. A claim held only in memory lets both
    taps through, and the second ends the conversation the first created.
    """

    def test_a_claim_taken_before_a_restart_still_holds_after_one(self) -> None:
        from hive_surfaces import bot_utils

        assert bot_utils.claim_picker(456, 99) is True

        # A fresh process holds nothing in memory and has not read the store
        # yet — so the claim can only come back off disk.
        bot_utils._spent_pickers.clear()
        bot_utils._loaded = False

        assert bot_utils.claim_picker(456, 99) is False
        assert bot_utils.claim_picker(456, 100) is True

    def test_an_unreadable_store_does_not_stop_the_bot_claiming(self, tmp_path, monkeypatch) -> None:
        """A picker not remembered across a restart is the old behaviour.

        A bot that will not start is worse than either, so corruption degrades
        to in-memory rather than raising.
        """
        from hive_surfaces import bot_utils

        broken = tmp_path / "broken" / "spent_pickers.json"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("{not json at all", encoding="utf-8")
        monkeypatch.setenv("PICKER_STATE_PATH", str(broken))

        bot_utils._spent_pickers.clear()
        bot_utils._loaded = False
        assert bot_utils.claim_picker(456, 99) is True
        assert bot_utils.claim_picker(456, 99) is False


@pytest.mark.asyncio
class TestARefusalIsNeverReportedAsSuccess:
    """Requirement 10, at the layer where the lie was actually told.

    `GatewayClient.server_command` returns the parsed body whatever the status
    was, and FastAPI reports its own rejections as `detail`, not `error` — a
    401 on a rotated bearer, a 404 on a swept session, a 422 on a body it will
    not parse. Reading only `error` meant a switch that never happened answered
    `Resumed "..."` with a tick on it, and the operator's next message went to
    the conversation they were already in.
    """

    async def test_a_fastapi_rejection_is_reported_as_an_error(self) -> None:
        from hive_surfaces.telegram_bot import _handle_server_command

        gw = AsyncMock()
        gw.server_command = AsyncMock(return_value={"detail": "Not authenticated"})

        with patch("hive_surfaces.telegram_bot.gateway", gw):
            msg = await _handle_server_command("/switch abc", 123, 456)

        assert msg.startswith("Error:")
        assert "Not authenticated" in msg

    async def test_a_comms_error_is_still_reported_as_an_error(self) -> None:
        """The original shape must keep working — this added a key, not swapped one."""
        from hive_surfaces.telegram_bot import _handle_server_command

        gw = AsyncMock()
        gw.server_command = AsyncMock(return_value={"error": "session not found"})

        with patch("hive_surfaces.telegram_bot.gateway", gw):
            msg = await _handle_server_command("/switch abc", 123, 456)

        assert msg.startswith("Error:")

    async def test_a_refused_switch_is_not_marked_done(self) -> None:
        """Requirement 10: and the refusal reaches the picker as a failure."""
        from hive_surfaces.telegram_bot import on_session_button

        gw = AsyncMock()
        gw.server_command = AsyncMock(return_value={"detail": "Not authenticated"})
        update, context = _make_callback(data="sw:sess-1")

        with patch("hive_surfaces.telegram_bot.gateway", gw):
            await on_session_button(update, context)

        annotated = update.callback_query.edit_message_text.call_args[0][0]
        assert "✅" not in annotated

    async def test_a_failed_new_does_not_claim_the_conversation_was_ended(self) -> None:
        """Requirement 10: the sentence under the mark must be true too.

        The "your conversation was ended" line was appended on whether one
        existed, not on whether ending it worked — so a failed /new told the
        operator to rebuild from scratch over a conversation still sitting
        there.
        """
        from hive_surfaces.telegram_bot import _run_session_button

        gw = AsyncMock()
        gw.find_active_session = AsyncMock(return_value="sess-1")
        gw.server_command = AsyncMock(return_value={"error": "comms unreachable"})

        with patch("hive_surfaces.telegram_bot.gateway", gw):
            worked, msg = await _run_session_button("new", "", 123, 456)

        assert worked is False
        assert "ended" not in msg

    async def test_a_tap_that_resolves_to_nothing_is_a_failure(self) -> None:
        """Requirement 10: the third failure shape, which reads like success.

        A refusal that happens not to begin with "Error:" was ticked, because
        the flag was sniffed from the prose instead of returned.
        """
        from hive_surfaces.telegram_bot import _run_session_button

        worked, msg = await _run_session_button("sw", "", 123, 456)

        assert worked is False
        assert msg.strip()


@pytest.mark.asyncio
class TestTheQueueKeepsMoving:
    async def test_an_item_in_flight_is_put_back_when_the_consumer_is_cancelled(self) -> None:
        """Requirement 13: a restart mid-send must not swallow the answer.

        The item is taken off the queue and lives only in the consumer's frame,
        so a cancellation there drops it — and the shutdown drain cannot see
        what is no longer in the queue.
        """
        from hive_surfaces import proactive
        from hive_surfaces.telegram_bot import _proactive_consumer

        proactive._reset()
        blocked = asyncio.Event()

        async def _hang(**_kwargs):
            blocked.set()
            await asyncio.Event().wait()

        app = MagicMock()
        app.bot.send_message = _hang
        proactive.enqueue(4242, "the answer you never saw")

        task = asyncio.create_task(_proactive_consumer(app))
        await blocked.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert proactive.drain() == [(4242, "the answer you never saw", 0)]

    async def test_one_undeliverable_item_does_not_delay_the_others(self) -> None:
        """Requirement 13: the retry wait rides on the item, not the consumer.

        Sleeping in the consumer made the interval global — one message with
        nowhere to go delayed every other answer by the full retry interval,
        and a queue of N items shed one attempt per interval rather than one
        per item.
        """
        from hive_surfaces import proactive
        from hive_surfaces.telegram_bot import _proactive_consumer

        proactive._reset()
        delivered: list[str] = []

        async def _send(chat_id=None, text=None, **_kwargs):
            if chat_id == 1:
                raise RuntimeError("blocked forever")
            delivered.append(text)

        app = MagicMock()
        app.bot.send_message = _send

        with patch("hive_surfaces.telegram_bot._PROACTIVE_RETRY_S", 3600):
            proactive.enqueue(1, "poison")
            for n in range(5):
                proactive.enqueue(2, f"good {n}")

            task = asyncio.create_task(_proactive_consumer(app))
            for _ in range(200):
                await asyncio.sleep(0)
                if len(delivered) == 5:
                    break
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # All five landed without waiting out the poison item's hour.
        assert delivered == [f"good {n}" for n in range(5)]


@pytest.mark.asyncio
class TestAFailedTapLeavesTheListUsable:
    """Requirement 3 — a refused tap costs one tap, not the whole picker.

    The claim that makes a picker single-use was taken before the action ran,
    and the keyboard was cleared first, so a tap that failed spent the list and
    removed its buttons — every remaining row and `New session` with them. The
    claims are persisted, so a restart did not clear it either.

    The list is *not* un-spent to fix this. Un-spending erases the row from
    the claim file, which is the only defence against a tap Telegram
    redelivers after a restart; a `/new` whose response was lost has already
    killed the conversation comms kills before creating its replacement, so a
    failed tap does not mean nothing happened; and a keyboard put back into
    scrollback stays tappable for months. The operator gets a *fresh* picker
    instead.
    """

    async def test_a_refused_action_redraws_the_list(self) -> None:
        """Breaks if the redraw is dropped: the operator is left with a spent
        picker and has to know to type /sessions."""
        from hive_surfaces.telegram_bot import on_session_button

        redraw = AsyncMock()
        run = AsyncMock(return_value=(False, "Error: Session abc is closed"))
        with patch("hive_surfaces.telegram_bot._run_session_button", run), \
                patch("hive_surfaces.telegram_bot._send_session_picker", redraw):
            update, context = _make_callback(data="sw:abc")
            await on_session_button(update, context)

        redraw.assert_awaited_once()
        assert redraw.await_args[0][1:] == (123, 456)

    async def test_an_action_that_raised_redraws_the_list(self) -> None:
        """The other failure shape: a raise leaves the tap looking like it did
        nothing at all, so the operator still needs a usable list."""
        from hive_surfaces.telegram_bot import on_session_button

        redraw = AsyncMock()
        with patch("hive_surfaces.telegram_bot._run_session_button",
                   AsyncMock(side_effect=RuntimeError("gateway down"))), \
                patch("hive_surfaces.telegram_bot._send_session_picker", redraw):
            update, context = _make_callback(data="sw:abc")
            await on_session_button(update, context)

        redraw.assert_awaited_once()

    async def test_a_successful_action_does_not_redraw(self) -> None:
        """A tap that worked has changed which conversation the chat is in.
        Drawing a picker under that is an invitation to undo it."""
        from hive_surfaces.telegram_bot import on_session_button

        redraw = AsyncMock()
        with patch("hive_surfaces.telegram_bot._run_session_button",
                   AsyncMock(return_value=(True, 'Resumed "Taxes"'))), \
                patch("hive_surfaces.telegram_bot._send_session_picker", redraw):
            update, context = _make_callback(data="sw:abc")
            await on_session_button(update, context)

        redraw.assert_not_awaited()

    async def test_a_failed_tap_never_un_spends_its_picker(self) -> None:
        """Requirement 4, at the point the 2026-09-17 incident re-enters.

        comms kills the chat's conversation *before* creating a replacement,
        so a `/new` that reports failure may already have destroyed one. A
        picker handed back after that is a second tap away from destroying the
        replacement too. Breaks the moment anything releases the claim.
        """
        from hive_surfaces.telegram_bot import on_session_button
        from hive_surfaces.bot_utils import claim_picker

        run = AsyncMock(return_value=(False, "Error: comms unreachable"))
        with patch("hive_surfaces.telegram_bot._run_session_button", run), \
                patch("hive_surfaces.telegram_bot._send_session_picker", AsyncMock()):
            update, context = _make_callback(data="new")
            await on_session_button(update, context)

        # The picker's own chat and message id: still claimed, so the action
        # cannot run twice.
        assert claim_picker(456, 99) is False

    async def test_a_failed_tap_stays_spent_across_a_restart(self) -> None:
        """The claim is on disk because the incident runs through a restart:
        polling down, taps queued at Telegram's end, service restarts,
        `getUpdates` redelivers. Breaks if a failure path ever writes the
        store back without the row it just used.
        """
        from hive_surfaces.telegram_bot import on_session_button
        from hive_surfaces import bot_utils

        with patch("hive_surfaces.telegram_bot._run_session_button",
                   AsyncMock(return_value=(False, "Error: comms unreachable"))), \
                patch("hive_surfaces.telegram_bot._send_session_picker", AsyncMock()):
            update, context = _make_callback(data="new")
            await on_session_button(update, context)

        # A fresh process: forget everything held in memory and read the file
        # back, exactly as a restarted bot does.
        bot_utils._reset_pickers()
        bot_utils._loaded = False
        assert bot_utils.claim_picker(456, 99) is False

    async def test_an_action_that_worked_keeps_the_picker_though_the_reply_failed(self) -> None:
        """Requirement 5, and the one that must not regress.

        `/new` succeeded on 2026-09-17, the reply never arrived, and the
        operator — with no way to know it had worked — tapped again and
        destroyed the conversation the first tap had just created. The claim
        follows whether the action ran, never whether the sentence about it
        got through.
        """
        from hive_surfaces.telegram_bot import on_session_button

        run = AsyncMock(return_value=(True, "New session: abcd1234"))
        with patch("hive_surfaces.telegram_bot._run_session_button", run), \
                patch("hive_surfaces.telegram_bot._send_session_picker", AsyncMock()):
            update, context = _make_callback(data="new")
            context.bot.send_message = AsyncMock(side_effect=RuntimeError("no route to host"))
            update.callback_query.edit_message_text = AsyncMock(
                side_effect=RuntimeError("no route to host")
            )
            await on_session_button(update, context)

            again, context2 = _make_callback(data="new")
            await on_session_button(again, context2)

        assert run.await_count == 1
