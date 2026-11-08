"""The columns these operations are written against, declared.

Two uses, and the second is why it is a declaration rather than a comment.

Every read names its columns from here instead of selecting everything. A
`SELECT *` on `core.sessions` hands back `carry_forward` and
`resend_content`, which are a composed prompt and a queued message - and the
listings that read sessions answer to the service token every surface bot in
the hive holds, so one careless read publishes a conversation's context to
every bot.

And the pool checks the applied database against this when it opens. A
column named here that the database does not have otherwise arrives as a
missing key in a dict, which the first consumer reads as a value:
`session.get("rotation_armed") or 0` is "nothing armed" on the one column
whose three states decide whether a permanent session row is kept or retired.
"""

from __future__ import annotations

from typing import Any

#: Every column of every `core` table these operations touch, as the applied
#: schema has it after the column-shape story.
COLUMNS: dict[str, tuple[str, ...]] = {
    "active_sessions": ("client_type", "client_ref", "session_id"),
    "auth_settings": ("key", "value"),
    "context_overhead": (
        "mind_id", "harness", "model", "surface", "tokens", "observed_at"),
    "conversations": ("id", "created_at"),
    "credentials": (
        "holder", "username", "role", "token_sha", "created_at", "revoked_at"),
    "group_members": ("group_name", "user_id"),
    "groups": ("name", "created_at"),
    "messages": (
        "id", "conversation_id", "from_mind", "to_mind", "message_number",
        "content", "rolling_summary", "metadata", "status",
        "recipient_session_id", "response_error", "timestamp"),
    "minds": (
        "mind_id", "name", "gateway_url", "model", "harness", "registered_at",
        "last_seen", "session_token", "voice", "voice_engine", "owner"),
    "secret_scopes": ("mind_name", "secret_key", "granted_at"),
    "session_memory": (
        "id", "mind_id", "mind_name", "client_ref", "conversation_id", "body",
        "created_at"),
    "session_turns": ("id", "session_id", "role", "content", "created_at"),
    "sessions": (
        "id", "conversation_id", "surface", "surface_id", "opening_line",
        "model", "created_at", "last_active", "status", "mind_id",
        "rotation_armed", "harness_sid", "carry_forward", "carry_forward_sid",
        "carry_forward_at", "context_tokens", "context_threshold",
        "context_window", "context_observed_at", "name", "color", "effort",
        "harness", "terminal_held", "context_kind", "context_has_images",
        "context_model", "resend_content", "resend_images",
        "resend_goal_rounds", "resend_dispatched_at",
        "conversation_started_at", "context_opening_tokens",
        "context_opening_surface", "carry_forward_prompt_cap"),
    "settings": ("key", "value", "updated_at"),
    "sso_bindings": ("provider", "subject", "username", "created_at"),
    "surface_claims": (
        "mind_id", "surface", "claimed_ref", "claimed_at", "phrase_hash",
        "phrase_issued_at", "phrase_expires_at", "bot_ref", "phrase_attempts",
        "released_at"),
    "users": (
        "id", "username", "password_hash", "created_at", "disabled_at",
        "must_change", "email", "domain_account", "session_version"),
    "memories": (
        "id", "mind_id", "content", "embedding", "content_search", "tags",
        "source", "data_class", "tier", "as_of", "expires_at", "superseded",
        "recurring", "codebase_ref", "created_at"),
    # Keyed `schema.table`, which the bare keys above are not. `tools` has a
    # `users` table and so does `core`, and they are different registries —
    # the console's people and hive-tools' own. A bare `"users"` was already
    # spoken for, so this is the day the comment below stopped being a
    # warning and became the rule for anything outside `core` and `memory`.
    "tools.api_tokens": (
        "id", "name", "token_hash", "created_at", "last_used_at",
        "revoked_at"),
    "tools.hitl_requests": (
        "id", "tool_name", "caller_mind", "params_json", "status",
        "created_at", "expires_at", "decided_at", "tool_method", "tool_path",
        "tool_body"),
    "tools.sudo_grants": (
        "id", "tool_name", "caller_mind", "granted_at", "expires_at"),
    "tools.tool_hitl_settings": ("tool_name", "mode", "sudo_timeout_minutes"),
    "tools.users": (
        "id", "username", "password_hash", "is_admin", "created_at",
        "disabled_at"),
    # Dotted for the same reason `tools` is: `board` has a `requirements`
    # column on two of its tables and the console's own people live in
    # `core.users`, not here. Nothing in this schema may claim a bare name.
    "board.capability_node_settings": ("node_id", "endpoint", "updated_at"),
    "board.design_notices": (
        "id", "design_id", "kind", "page", "body", "created_at", "claimed_at",
        "delivered_at"),
    "board.design_sessions": (
        "id", "title", "conversation_id", "surface", "mind_id", "repos",
        "requirements", "tests", "functions", "accepted", "stale", "revision",
        "created_at", "updated_at"),
    "board.edge_mind_settings": (
        "name", "profile", "role", "deployment", "updated_at"),
    "board.operation_requesters": ("request_id", "username", "created_at"),
    "board.work_items": (
        "id", "kind", "parent_id", "title", "status", "position",
        "description", "requirements", "acceptance_criteria", "qa_tests",
        "subtype"),
    #: Lucent's half. Qualified keys, not bare ones, and that is the point:
    #: `credentials` exists in both `core` and `memory`, the 12 holders that
    #: appear in both share no `token_sha`, and a bare name could only ever
    #: mean one of them.
    "memory.nodes": (
        "id", "mind_id", "type", "name", "first_name", "last_name",
        "properties", "data_class", "tier", "source", "as_of", "created_at",
        "updated_at", "owner"),
    "memory.edges": (
        "id", "mind_id", "source_id", "target_id", "type", "as_of", "source",
        "data_class", "tier", "created_at", "properties", "owner"),
    "memory.soul_changes": (
        "id", "node_id", "mind_name", "mind_id", "actor", "reason",
        "before_json", "after_json", "at", "created_at"),
    "memory.schema_types": ("name", "kind", "property_schema", "created_at"),
    "memory.schema_edges": (
        "name", "source_type", "target_type", "attr_schema", "symmetric",
        "created_at"),
    "memory.credentials": (
        "holder", "username", "role", "mind_id", "token_sha", "created_at",
        "revoked_at"),
}

#: The Postgres schema a declared table lives in. Absent means `core`, which
#: is where all but one of them are.
#:
#: A map rather than qualified keys in `COLUMNS`, because the bare name is
#: what every operation passes and what every error message reads better
#: with. A bare `"credentials"` here means the `core` one, and they are NOT
#: the same registry as lucent's - 12 holders appear in both and none of the
#: 12 share a `token_sha`. Lucent's tables are therefore declared in
#: `COLUMNS` under qualified keys (`memory.credentials` and the rest), which
#: `schema_of` reads directly and this map never sees.
SCHEMAS: dict[str, str] = {
    "memories": "memory",
}


def schema_of(table: str) -> str:
    """The Postgres schema a declared table lives in."""
    if "." in table:
        return table.split(".", 1)[0]
    return SCHEMAS.get(table, "core")


def bare(table: str) -> str:
    """A declared key without the schema it may carry.

    `information_schema` answers in two columns, so every lookup against the
    applied database needs the table name on its own. A dotted key is the
    only form that can name two tables of the same name in two schemas, and
    the bare name is still what Postgres calls the table.
    """
    return table.split(".", 1)[1] if "." in table else table


def qualified(table: str) -> str:
    """A table's name as a statement has to spell it."""
    return f'{schema_of(table)}."{bare(table)}"'

#: Columns a caller must ask for by name. A session listing answers to the
#: service token every surface bot holds, and these two are a composed system
#: prompt and a queued user message.
PRIVATE: dict[str, frozenset[str]] = {
    #: The credential the gateway authenticates to a mind with. The edge
    #: design says in as many words that it is stripped from every mind
    #: listing, because those listings answer to the service token every
    #: surface bot in the hive holds. A listing that merely *includes* the
    #: key is how that gets missed: nothing looks wrong.
    "minds": frozenset({"session_token"}),
    #: The bearer digest. Reading it is how one holder authenticates as
    #: another.
    "credentials": frozenset({"token_sha"}),
    "users": frozenset({"password_hash"}),
    #: The same two reasons as above, in hive-tools' own registries: the
    #: bearer digest a caller authenticates with, and a password hash.
    "tools.api_tokens": frozenset({"token_hash"}),
    "tools.users": frozenset({"password_hash"}),
    #: What a gated call was actually called with. The approvals queue is a
    #: listing, and these two are the captured request body of every tool
    #: call a person was asked to approve — on live data the newest fifty
    #: rows carry three megabytes of it, some of it holding credentials. The
    #: page this listing is modelled on selects neither. The replay path,
    #: which genuinely needs the body, names it.
    "tools.hitl_requests": frozenset({"params_json", "tool_body"}),
    #: The 4096 floats. Private because no caller reading memories wants
    #: them: lucent has never returned an embedding, and a listing that
    #: included one hands back 4096 numbers per row, as a 50,000-character
    #: literal over the wire, on a route called every turn. A caller that
    #: genuinely needs the vector asks for it by name.
    #:
    #: The searchable form of the text, not the text. It is a tsvector, it
    #: is the size of the content again, and asyncpg has no codec for it, so
    #: a listing that selected it would hand every caller a large string of
    #: lexeme positions nobody asked for. Declared so the drift check sees
    #: it; private so no read returns it.
    "memories": frozenset({"content_search", "embedding"}),
    "sessions": frozenset({
        "carry_forward", "carry_forward_sid", "carry_forward_at",
        "carry_forward_prompt_cap", "resend_content", "resend_images",
        "resend_goal_rounds", "resend_dispatched_at",
    }),
}

#: Columns the database generates. An insert naming one is refused: Postgres
#: accepts a value on a `GENERATED BY DEFAULT` column and does not advance
#: the sequence, so the row lands and the next insert collides.
GENERATED: dict[str, frozenset[str]] = {
    "session_memory": frozenset({"id"}),
    "session_turns": frozenset({"id"}),
    "users": frozenset({"id"}),
    "tools.api_tokens": frozenset({"id"}),
    "tools.sudo_grants": frozenset({"id"}),
    "tools.users": frozenset({"id"}),
    "board.design_notices": frozenset({"id"}),
    #: `content_search` is GENERATED ALWAYS, not BY DEFAULT: Postgres
    #: refuses a value for it outright. Named here so the refusal happens
    #: before the statement is built, with a sentence saying why, rather
    #: than arriving from the server as a syntax-shaped error.
    "memories": frozenset({"id", "content_search"}),
    "memory.nodes": frozenset({"id"}),
    "memory.edges": frozenset({"id"}),
    "memory.soul_changes": frozenset({"id"}),
}


#: Columns whose *type* is checked at startup, not only their name.
#:
#: Names alone are not enough for these two. A `memory.memories.embedding`
#: left as `bytea` passes a name check, and then asyncpg — with the vector
#: codec registered but the column not actually a vector — hands the row
#: back as raw bytes, or, the other way round, hands an unregistered vector
#: back as a 50KB `str`. Neither raises. Both make every similarity number
#: wrong in a way that reads as a working search, and a dimension check
#: written the obvious way measures characters instead of dimensions.
#:
#: `udt_name` as Postgres spells it, which is why it is `tsvector` rather
#: than `TSVECTOR` and `vector` rather than `vector(4096)`.
TYPED: dict[tuple[str, str], str] = {
    ("memories", "embedding"): "vector",
    ("memories", "content_search"): "tsvector",
}


def key(table: str) -> tuple[str, str]:
    """A declared table as `information_schema` keys it."""
    return (schema_of(table), bare(table))


def qualified_name(table: str) -> str:
    """A declared table as a sentence names it, unquoted."""
    return f"{schema_of(table)}.{bare(table)}"


def public(table: str) -> tuple[str, ...]:
    """Every column of a table except the ones a caller must name."""
    private = PRIVATE.get(table, frozenset())
    return tuple(c for c in COLUMNS[table] if c not in private)


def select_list(table: str, columns: tuple[str, ...] | None = None) -> str:
    """A column list for a SELECT, qualified and quoted."""
    chosen = columns if columns is not None else public(table)
    unknown = [c for c in chosen if c not in COLUMNS[table]]
    if unknown:
        raise KeyError(
            f"{qualified_name(table)} has no column(s) {unknown}. The declared "
            "columns "
            "are the ones these operations are written against; a name not "
            "among them is a typo, not a column.")
    return ", ".join(f'"{c}"' for c in chosen)


#: The one schema every install has. A deployment can decline to create any
#: of the others — comms runs `core` and nothing else, and the health context
#: is the one a corporate install deliberately does not create — but a
#: database with no `core` is not this hive's database.
REQUIRED_SCHEMA = "core"


async def _present_schemas(connection: Any) -> set[str]:
    """Which of the declared schemas this database actually has.

    An absent schema is a context this deployment does not run, not a
    mismatch. The distinction matters in both directions: declaring a
    `memory` table must not stop comms — which uses none of them — from
    opening a pool at all, and a `memory` schema that *is* there still has
    every column and type checked.
    """
    wanted = sorted({schema_of(table) for table in COLUMNS})
    rows = await connection.fetch(
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name = ANY($1)", wanted)
    return {row["schema_name"] for row in rows}


async def _sequences_behind(connection: Any,
                            declared: set[str]) -> list[str]:
    """Identity sequences whose next value a row already occupies.

    Checked at startup for the same reason the column names are: the failure
    it prevents arrives at a call site as something else entirely, and is
    unrecoverable there.
    """
    out: list[str] = []
    for table, generated in GENERATED.items():
        # `declared` is the tables this database actually has, so a context
        # it does not run — or one whose schema exists with no tables in it
        # — is skipped. `pg_get_serial_sequence` raises on a table that is
        # not there, which would make the probe the failure.
        if table not in declared:
            continue
        for column in sorted(generated):
            where = schema_of(table)
            # The sequence is asked for on its own, before anything reads the
            # column. `GENERATED` holds every column the database fills in,
            # and not all of them are identities — `memories.content_search`
            # is a generated tsvector, and `max()` of one is not a function
            # that exists. Asking in one statement made that a hard failure
            # at startup for every consumer.
            sequence = await connection.fetchval(
                "SELECT pg_get_serial_sequence($1, $2)",
                f"{where}.{bare(table)}", column)
            if sequence is None:
                continue
            row = await connection.fetchrow(
                f'SELECT max("{column}") AS "top" FROM {qualified(table)}')
            if row is None or row["top"] is None:
                continue
            # Read off the sequence relation itself. `pg_sequences` carries
            # `last_value` but not `is_called`, and the difference is
            # exactly one: a sequence that has never been called will
            # issue `last_value`, one that has will issue the next.
            state = await connection.fetchrow(
                f"SELECT last_value, is_called FROM {sequence}")
            if state is None:
                continue
            nextval = int(state["last_value"]) + (
                1 if state["is_called"] else 0)
            if nextval <= int(row["top"]):
                out.append(
                    f"{where}.{bare(table)}.{column} would next issue {nextval} "
                    f"and row {int(row['top'])} already exists")
    return out


async def verify(connection: Any) -> None:
    """Check the applied schema against what is declared here.

    Run when the pool opens, so a mismatch is a process that refuses to
    start rather than a value that quietly reads as absent on the first
    conversation that needs it.

    Extra columns in the database are not a mismatch: a column added for
    something else is none of this package's business. A column declared
    here and missing there is, because every read names it.
    """
    from .errors import SchemaMismatch, SequenceBehind

    present = await _present_schemas(connection)
    if REQUIRED_SCHEMA not in present:
        raise SchemaMismatch(
            f"the database has no {REQUIRED_SCHEMA} schema. Every operation "
            "in this package but the memory ones is written against it, so "
            "this is either an empty database or the wrong one — not a "
            "deployment that declined to run a context.")
    declared = {t for t in COLUMNS if schema_of(t) in present}
    rows = await connection.fetch(
        "SELECT table_schema, table_name, column_name, data_type, udt_name "
        "FROM information_schema.columns WHERE table_schema = ANY($1)",
        sorted(present))
    applied: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        applied.setdefault(
            (row["table_schema"], row["table_name"]), {}
        )[row["column_name"]] = row["udt_name"]

    # A schema that exists and holds none of its declared tables is a
    # context nobody created — `CREATE SCHEMA` on its own is not one — and
    # not drift. The distinction is load-bearing: every service in the hive
    # shares this database, so a half-run migration that made the schema and
    # no tables would otherwise stop comms, lucent and every mind from
    # opening a pool over a context none of them use. A schema holding
    # *some* of its tables is drift, and still refuses.
    empty = {
        schema for schema in present
        if not any(key(table) in applied
                   for table in declared if schema_of(table) == schema)
    } - {REQUIRED_SCHEMA}
    declared = {t for t in declared if schema_of(t) not in empty}
    missing_tables = sorted(
        qualified_name(table)
        for table in declared
        if key(table) not in applied)
    missing_columns = sorted(
        f"{qualified_name(table)}.{column}"
        for table, columns in COLUMNS.items()
        if table in declared and key(table) in applied
        for column in columns
        if column not in applied[key(table)]
    )
    wrong_types = sorted(
        f"{qualified_name(table)}.{column} is "
        f"{applied[key(table)][column]} where these "
        f"operations are written against {expected}"
        for (table, column), expected in TYPED.items()
        if table in declared and key(table) in applied
        and column in applied[key(table)]
        and applied[key(table)][column] != expected
    )
    # Reported before the sequence probe below, and the order is the finding
    # rather than a tidiness preference. `pg_get_serial_sequence` raises on a
    # table or column that is not there, so a `memories` or `users` missing a
    # declared column used to come back as the driver's own sentence about
    # that one column - and anything else that had drifted went unmentioned.
    # The operator fixed the column they were told about, restarted, and got
    # the rest of the drift on the next boot.
    if missing_tables or missing_columns or wrong_types:
        _refuse(missing_tables, missing_columns, wrong_types)

    behind = await _sequences_behind(connection, declared)
    if behind:
        raise SequenceBehind(
            "identity sequences are behind the rows already in their "
            "tables: " + "; ".join(behind) + ". A bulk load that inserted "
            "explicit ids does this — Postgres takes them on a GENERATED BY "
            "DEFAULT column without advancing the sequence — and the next "
            "insert then collides on a key that is already there. It "
            "collides as a uniqueness conflict, which is the one outcome a "
            "caller is told is benign, so a ledger would record nothing "
            "while reporting a harmless duplicate. Run setval on each.")

    return None


def _refuse(missing_tables: list[str], missing_columns: list[str],
            wrong_types: list[str]) -> None:
    """Refuse to open, naming everything that has drifted at once."""
    from .errors import SchemaMismatch

    parts = []
    if missing_tables:
        parts.append("tables absent: " + ", ".join(missing_tables))
    if missing_columns:
        parts.append("columns absent: " + ", ".join(missing_columns))
    if wrong_types:
        parts.append("columns of the wrong type: " + "; ".join(wrong_types))
    raise SchemaMismatch(
        "the applied schema is not the one these operations were written "
        "against — " + "; ".join(parts) + ". Refusing at startup rather "
        "than at the call site: a column this package names and the "
        "database does not have arrives as a missing dict key, and a "
        "missing key reads as a value.")
