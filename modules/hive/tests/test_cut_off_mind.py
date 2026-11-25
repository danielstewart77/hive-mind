"""Cutting a mind off: its key is refused on its next request, every other mind keeps working.

Protected behaviour 3 (the cut-off half): a mind that has been cut off is
refused on its very next request while every other mind keeps working.

Same setup as the replaced-key test: the real gateway and memory store over
ASGI against the suite's scratch Postgres, the tools at the transport (their
own refusal is guarded by the tools' suite).
"""
import httpx
import pytest

import credentials_data
import holder_credentials
from hive_site import testing

from .test_replace_mind_key import ADA_ID, BOB, OLD, hive  # noqa: F401 - fixture


async def test_a_cut_off_mind_is_refused_by_all_three_services_on_its_next_request_and_bobs_keeps_working(hive):
    assert await hive.status(OLD) == {"comms": 200, "lucent": 200, "tools": 404}

    result = await holder_credentials.cut_off_mind("Ada")

    assert result["ok"] is True
    assert [(s["service"], s["status"]) for s in result["services"]] == [
        ("comms", "cut off"), ("lucent", "cut off"), ("tools", "cut off")]
    for service, code in (await hive.status(OLD)).items():
        assert code in (401, 403), service
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200, "tools": 404}
    # The stored line stays: the page still says where the mind reads.
    assert credentials_data._read_env_value(hive.env, "MIND_KEY_ADA") == OLD


async def test_cutting_off_a_mind_already_cut_off_succeeds_again(hive):
    await holder_credentials.cut_off_mind("ada")

    again = await holder_credentials.cut_off_mind("ada")

    assert again["ok"] is True
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200, "tools": 404}


async def test_with_the_tools_unreachable_the_gateway_and_memory_store_are_cut_off_and_the_answer_names_the_tools(hive):
    hive.tools.down = True

    result = await holder_credentials.cut_off_mind("ada")

    assert result["ok"] is False
    assert {s["service"]: s["status"] for s in result["services"]} == {
        "comms": "cut off", "lucent": "cut off", "tools": "unreachable"}
    hive.tools.down = False
    answers = await hive.status(OLD)
    assert answers["comms"] in (401, 403) and answers["lucent"] in (401, 403)
    assert answers["tools"] == 404  # the tools still accept it
    retry = await holder_credentials.cut_off_mind("ada")
    assert retry["ok"] is True
    assert (await hive.status(OLD))["tools"] in (401, 403)
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200, "tools": 404}


async def test_pasting_a_key_for_a_cut_off_mind_brings_it_back_and_touches_no_other(hive):
    await holder_credentials.cut_off_mind("ada")
    new = "ada-back-key-0123456789"

    await holder_credentials.replace_mind_key("ada", "daniel", ADA_ID, new)

    assert await hive.status(new) == {"comms": 200, "lucent": 200, "tools": 404}
    assert await hive.status(BOB) == {"comms": 200, "lucent": 200, "tools": 404}


async def test_a_service_that_refuses_the_cut_off_is_reported_as_still_accepting(hive):
    hive.tools.refuse_delete = True

    result = await holder_credentials.cut_off_mind("ada")

    assert result["ok"] is False
    assert {s["service"]: s["status"] for s in result["services"]} == {
        "comms": "cut off", "lucent": "cut off", "tools": "still accepting"}


async def test_a_mind_the_tools_hold_no_key_for_is_not_reported_as_cut_off_there(hive):
    hive.tools.values.pop("ada")  # a mind still on the shared tools token

    result = await holder_credentials.cut_off_mind("ada")

    assert result["ok"] is False
    assert {s["service"]: s["status"] for s in result["services"]}["tools"] == "no key here"


async def test_a_mind_the_tools_know_only_by_the_shared_token_is_not_reported_as_cut_off(hive):
    result = await holder_credentials.cut_off_mind("skippy")

    assert result["ok"] is False
    assert {s["service"]: s["status"] for s in result["services"]}["tools"] == "shared token"
