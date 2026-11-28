"""Cutting a mind off: its key is refused on its next request, every other mind keeps working.

Protected behaviour 3 (the cut-off half): a mind that has been cut off is
refused on its very next request while every other mind keeps working.

Same setup as the replaced-key test: the real gateway and memory store over
ASGI against the suite's scratch Postgres; the key is one row in the record.
"""
import holder_credentials
import mind_key_files

from .test_replace_mind_key import ADA_ID, BOB, OLD, hive  # noqa: F401 - fixture


async def test_a_cut_off_mind_is_refused_on_its_next_request_and_bobs_keeps_working(hive):
    assert await hive.status(OLD) == {"comms": 200, "lucent": 200}

    result = await holder_credentials.cut_off_mind("Ada")

    assert result["ok"] is True
    assert [(s["service"], s["status"]) for s in result["services"]] == [("record", "cut off")]
    for service, code in (await hive.status(OLD)).items():
        assert code in (401, 403), service
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200}
    # The stored line stays: the page still says where the mind reads.
    assert mind_key_files.read("ada") == OLD


async def test_cutting_off_a_mind_already_cut_off_succeeds_again(hive):
    await holder_credentials.cut_off_mind("ada")

    again = await holder_credentials.cut_off_mind("ada")

    assert again["ok"] is True
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200}


async def test_pasting_a_key_for_a_cut_off_mind_brings_it_back_and_touches_no_other(hive):
    await holder_credentials.cut_off_mind("ada")
    new = "ada-back-key-0123456789"

    await holder_credentials.replace_mind_key("ada", "daniel", ADA_ID, new)

    assert await hive.status(new) == {"comms": 200, "lucent": 200}
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200}


async def test_a_replacement_a_cut_off_settles_while_recovery_waits_is_not_re_applied(hive):
    import asyncio
    record = holder_credentials.key_journal.begin("replace", "ada", ["comms"])
    await holder_credentials.cut_off_mind("ada")
    # The cut-off cleared it; put the same record back as recovery had already read it.
    holder_credentials.key_journal._write(record)
    async with holder_credentials._ROTATION_LOCK:
        recovery = asyncio.ensure_future(holder_credentials.recover_replacements("daniel", True))
        await asyncio.sleep(0.05)
        holder_credentials.key_journal.finish(record)

    handled = await recovery

    assert handled == []
    for service, code in (await hive.status(OLD)).items():
        assert code in (401, 403), service
