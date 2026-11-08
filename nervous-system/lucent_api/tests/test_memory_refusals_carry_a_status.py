"""A store that refused says so with a status code, not with a two hundred.

Every memory function used to wrap its body in `except Exception` and return
`{"error": str(e)}` — at two hundred. Against a local file that was
survivable, because the only way to get one was a bug. Against a database
over a socket, `Unreachable`, `TimedOut`, `NoConnectionAvailable`,
`TransactionAborted` and `Conflict` are all routine, and the callers are
detached `bash + jq + curl` hooks that check nothing. A memory that was
never written then arrives in the log as a turn that was remembered.

What is pinned here is the translation table: which refusal is worth which
code, and that the sentence `hive_data` wrote — the one naming the rule and
the remedy — survives into the body. The exceptions are constructed; the
code and the message come from the router.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import hive_data
from lucent_api.routers.memory import _answered


async def _raising(exc: BaseException):
    raise exc


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,expected", [
    (hive_data.Unreachable("nothing is listening on 5432"), 503),
    (hive_data.NoConnectionAvailable("every connection was busy"), 503),
    (hive_data.TimedOut("the statement did not finish"), 504),
    (hive_data.TransactionAborted("the unit of work is already aborted"), 503),
    (hive_data.Conflict("that row already exists"), 409),
    (hive_data.ValueRefused("a recall was given an empty embedding"), 400),
])
async def test_a_refusal_arrives_as_its_own_status_code(exc, expected):
    with pytest.raises(HTTPException) as caught:
        await _answered(_raising(exc))

    assert caught.value.status_code == expected
    # The sentence, not a label: it is the only thing in the log when a
    # nightly write stops landing.
    assert caught.value.detail == str(exc)


@pytest.mark.asyncio
async def test_an_answer_that_was_not_refused_passes_straight_through():
    async def _fine():
        return {"memories": [], "count": 0}

    assert await _answered(_fine()) == {"memories": [], "count": 0}
