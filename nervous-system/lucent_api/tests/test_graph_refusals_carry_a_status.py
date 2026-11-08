"""A graph refusal arrives as a status, including the ones with no own class.

The graph table is the memory table's sibling and it had no floor. Thirteen
named kinds were listed and the base of them was not, so a `WriteRejected`
`hive_data` raises for every Postgres error it has no narrower class for —
`permission denied for table memory.nodes` on a wrong database role, a
read-only standby, a detected deadlock, a serialization failure, `stack
depth limit exceeded` — fell past the table into `internal_error` at HTTP
200. On a cutover the wrong database role is the single most likely failure,
and a 200 is the one answer nothing notices.

The exceptions are constructed. The code, the status and the sentence all
come from `lucent_graph` and from the router that decodes what it produced.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import hive_data
from lucent_api.lucent_graph import _failed
from lucent_api.routers.graph import _decode


@pytest.mark.parametrize("exc,code,status", [
    # The narrower kinds still win over the floor, which is why the floor
    # is last in the table rather than first.
    (hive_data.Conflict("that row already exists"), "conflict", 409),
    (hive_data.MissingReference("no such mind"), "missing_reference", 409),
    (hive_data.ValueRefused("text cannot hold a NUL"), "value_refused", 400),
    (hive_data.TimedOut("the statement ran out of time"), "store_timeout", 503),
    # The floor itself: a bare rejected write, and anything else at all.
    (hive_data.WriteRejected("permission denied for table memory.nodes"),
     "store_refused_the_write", 400),
    (hive_data.SequenceBehind("the sequence is behind the rows"),
     "store_sequence_behind", 500),
])
def test_a_graph_refusal_names_its_rule_and_carries_its_status(
        exc, code, status):
    body = json.loads(_failed(exc, doing="node-write"))

    assert body["code"] == code
    assert body["http_status"] == status
    assert str(exc) in body["detail"]


def test_an_unclassed_store_error_is_still_a_refusal_not_a_two_hundred():
    """`HiveDataError` is the second floor, under `WriteRejected`.

    A kind added to `hive_data` later, or one of its non-write failures,
    must not be the one thing that reaches a caller as 200.
    """
    body = json.loads(_failed(hive_data.HiveDataError("something new"),
                              doing="node-write"))

    assert body["code"] == "store_error"
    assert body["http_status"] == 503


@pytest.mark.parametrize("exc", [
    hive_data.WriteRejected("permission denied for table memory.nodes"),
    hive_data.HiveDataError("something new"),
])
def test_the_router_raises_rather_than_answering_the_refusal_body(exc):
    """The body carrying a status is only half of it; the router has to lift
    it out, because a refusal in a 200 body is a refusal nobody notices."""
    with pytest.raises(HTTPException) as caught:
        _decode(_failed(exc, doing="node-write"))

    assert caught.value.status_code >= 400
    assert str(exc) in str(caught.value.detail)
