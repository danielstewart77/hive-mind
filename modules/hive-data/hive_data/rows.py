"""What an operation hands back.

A row is a plain `dict`. Nothing a caller imports, nothing with methods,
nothing that stops being JSON when it crosses an HTTP boundary - asyncpg's
own `Record` is none of those things, and a service that returned them would
hand its web framework an object its encoder refuses.

This constrains the shape of a *row*, not of every return value. A read that
matches nothing returns `None`, which is neither a dict nor a list and is the
only honest answer: an empty dict would be a row with no columns.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .errors import HiveDataError


class _Unset:
    """A parameter nobody passed, as distinct from one passed as null.

    Comms clears columns deliberately - `harness_sid = NULL` when a failed
    turn must not resume a broken thread - and it also skips columns it has
    no reading for, because zero is a model nobody measured rather than a
    model with no room left. An operation whose unset parameters defaulted to
    `None` could express only one of those: skipping every `None` never
    clears `harness_sid`, and writing every `None` blanks the context window
    to zero, which the surrounding code draws as a conversation over its
    threshold.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSET"

    def __bool__(self) -> bool:
        raise TypeError(
            "UNSET has no truth value. Test it with `is UNSET`, because the "
            "whole point of it is to be distinguishable from a falsy value "
            "somebody meant to write.")


#: The sentinel itself. One instance, compared by identity.
UNSET = _Unset()


def as_row(record: Any) -> dict[str, Any] | None:
    """One record as a plain dict, or None if there was no record."""
    if record is None:
        return None
    return dict(record)


def as_rows(records: Iterable[Any]) -> list[dict[str, Any]]:
    """Records as a list of plain dicts."""
    return [dict(r) for r in records]


def written(rows: int, row: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The result of a write: how many rows it changed, and what came back.

    A write reports its row count because the first consumer arbitrates races
    on exactly that. The contest between two people sending a bot's claim
    phrase at once is an `UPDATE ... WHERE claimed_ref IS NULL` followed by
    `if rowcount != 1` - that statement *is* the arbitration - and the same
    shape decides whether a broker message was newly inserted, and whether a
    context report names a session that still exists. A write that could only
    answer with a list of rows would answer `[]` for all of them, and `[]`
    reads as success: two confirmed owners for one bot, and an `ok` for a
    session that is gone.
    """
    out: dict[str, Any] = {"rows": rows}
    if row is not None:
        out["row"] = dict(row)
    return out


def row_count(status: str) -> int:
    """The row count out of an asyncpg command status.

    asyncpg reports `"UPDATE 0"` rather than a number, and the tag varies by
    statement (`INSERT 0 1` carries an oid first). The count is the last
    field in every one of them.

    A status this cannot read raises rather than returning zero. The count is
    the arbitration — one caller is told it won a race and the others are
    told they lost — so a zero nobody measured is a caller told it lost for
    no reason, which is the one answer this must never guess.
    """
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (AttributeError, ValueError):
        raise HiveDataError(
            f"could not read a row count out of the command status "
            f"{status!r}. The count decides which caller won, so it is not "
            "defaulted to zero.") from None
