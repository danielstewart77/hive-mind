"""One in-memory graph schema for every test that needs one.

Five test files each carried their own copy of the `nodes` DDL, and each
copy drifted from `lucent._init_schema` independently: a column added to
the real schema left every one of them testing a table the store does not
have, which is how a suite stays green over a store that would reject the
same write. The schema comes from the module that owns it.
"""
from __future__ import annotations

import sqlite3


def make_graph_conn() -> sqlite3.Connection:
    """An in-memory database carrying the real graph schema."""
    from lucent_api.lucent import _init_schema

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _init_schema(conn)
    return conn
