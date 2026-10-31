"""The named operations over the `memory` schema: storing and recalling.

What is here and what is deliberately not. lucent owns the memory service:
it turns text into an embedding, decides the data class and the tier, guards
the standing tier, holds the knowledge graph and prunes. None of that moves
here. What moves here is the part that was SQLite-specific - an FTS5 virtual
table for keywords and a scan that pulled every row's 4096 floats into numpy
to score cosine in Python - because neither survives the move to Postgres.

So these operations take an embedding as given and never generate one. The
model that produces it, and the credential for reaching that model, belong
behind lucent's API; a data-access layer that called out to an inference
service would be a layer every consumer has to configure an inference
service for.

The blend in `recall` is lucent's own, ported with its constants and its
bucket arithmetic unchanged. That is deliberate and it is the whole point of
the story: the only thing that changes is where the arithmetic runs. Whether
a better ranking should replace it is a separate decision with its own
evidence, and its own story.
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Mapping, Sequence

from . import _sql
from .core import _execute, _execute_returning, _fetch, _fetch_one
from .errors import HiveDataError, ValueRefused
from .rows import UNSET
from .schema import public as schema_public
from .schema import qualified, select_list
from .unit import Unit

log = logging.getLogger("hive-data")

#: lucent's own, unchanged. A memory's score decays with a 30-day half-life
#: and never falls below half, so recency is a bounded multiplier on
#: similarity rather than a second ranking.
RECENCY_HALF_LIFE_DAYS = 30
RECENCY_FLOOR = 0.5
VECTOR_POOL = 20
KEYWORD_POOL = 10
DEFAULT_K = 5

#: How many keyword hits are asked for before the provenance filter is
#: applied. lucent asks FTS5 for `KEYWORD_POOL * 4` across the whole store
#: and only then keeps the ones belonging to the minds being read, so a mind
#: holding few rows can end up with fewer than `KEYWORD_POOL` candidates.
#: Carried across exactly, because this story ports the blend rather than
#: improving it - and improving it changes which memories arrive in every
#: mind's context, which is a judgement call that belongs with the evidence.
KEYWORD_SCAN = KEYWORD_POOL * 4

#: Postgres has no BM25, and `ts_rank` is not a rescaled BM25 - it is a
#: different formula with a different shape. `ts_rank` is bounded below 1 and
#: `bm25()` is not bounded at all, so no single multiplier can map one onto
#: the other for every query. Measured over the live corpus with the ten real
#: queries in `tools/memory_search_parity.py`, which reports the ratio per
#: query so this number can be checked rather than believed: it runs from
#: roughly 90 to roughly 540, and the median is what is used here.
#:
#: An approximation is still worth having, because the alternative is not
#: "no scaling" - it is a silent change of magnitude in two places. The
#: backfill ranking below adds `0.05 * keyword` to `cosine * recency`, which
#: at FTS5's scale is real weight against a combined score of 0.2 to 0.8 and
#: at `ts_rank`'s raw scale is 0.003, which is nothing. And the figure is
#: rendered as `bm=` into every mind's injected context every turn by the
#: contextual-retrieval hook. Being within a factor of two of the magnitude
#: those were written against is the goal; being exact is not available.
#:
#: Monotonic, so nothing about the ordering within the keyword bucket moves.
KEYWORD_SCALE = 264.0

#: The text search configuration the searchable column is built with. Named
#: here as the schema declares it, because a query has to be parsed with the
#: same configuration the document was, or the two sides stem differently
#: and a word matches itself and nothing else.
TEXT_CONFIG = "memory.hive_text"

#: lucent's stoplist, verbatim. It filters the *query*; the index keeps
#: every token, which is what FTS5 does. The two are not interchangeable: a
#: configuration that dropped stopwords from the documents as well would
#: silently stop matching query words this list happens to keep.
STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "of", "in", "on", "at",
    "to", "for", "from", "by", "with", "as", "is", "are", "was", "were",
    "be", "been", "being", "do", "does", "did", "have", "has", "had",
    "this", "that", "these", "those", "it", "its", "i", "you", "we",
    "they", "he", "she", "me", "my", "our", "your", "their", "them",
    "us", "what", "when", "where", "why", "how", "who", "which",
    "can", "could", "would", "should", "will", "shall", "may", "might",
    "just", "really", "very", "so", "also", "too", "any", "some", "all",
    "no", "not", "yes", "ok", "okay",
})

#: What a read hands back. `content_search` and `embedding` are both declared
#: private, so neither is in it.
#:
#: The embedding being private is not a detail. lucent builds its result rows
#: by hand and has never returned one, and a `recall` that did would hand
#: back 4096 floats per row - measured at 240KB for five rows, crossing the
#: wire as a text literal and parsed back into floats one at a time, which is
#: four times the cost of the scan doing it in the database was supposed to
#: save. The contextual-retrieval hook calls this every turn.
_READ = select_list("memories")

#: The keys lucent's hybrid answer carries, in its own order. A caller reading
#: that answer by key sees exactly these; `recurring` is not among them,
#: because lucent's row-building code does not name it.
_RECALL_KEYS = (
    "id", "content", "tags", "source", "mind_id", "created_at", "data_class",
    "tier", "as_of", "expires_at", "superseded", "codebase_ref",
)


def _as_lucent_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One row in the shape lucent's callers read.

    `superseded` as a boolean rather than the column's `bigint`: lucent
    returns `bool(...)` or `None`, and a reader testing it for truth cannot
    tell 0 from False but a reader comparing it to `False` can.
    """
    out = {key: row.get(key) for key in _RECALL_KEYS}
    flag = out.get("superseded")
    out["superseded"] = None if flag is None else bool(flag)
    return out


def terms(query: str) -> list[str]:
    """The words of a free-text query that are worth searching for.

    Tokenised to alphanumerics, which also means nothing a caller typed can
    reach the query parser as an operator: `to_tsquery` reads `&`, `|`, `!`,
    `:` and `<->` as syntax, and a bare apostrophe is a syntax error rather
    than a word.
    """
    found = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query)]
    return [t for t in found if len(t) > 2 and t not in STOPWORDS]


#: How many of a query's words are searched for. A pasted transcript is a
#: query too, and `to_tsquery` parses an OR of 60,000 terms by recursive
#: descent: measured, that one dies on `stack depth limit exceeded`, which
#: arrives through the taxonomy as a rejected *write* on a read-only recall.
#: lucent cannot hit it - FTS5 has no such limit - so a cap keeps the port
#: from failing where the thing it replaces degrades.
MAX_QUERY_TERMS = 200


def _tsquery(query: str) -> str | None:
    """A query's words as an OR-joined tsquery, or None if it has none.

    OR rather than AND, because that is what lucent does: any meaningful
    token contributes to the ranking, and a memory matching one word of six
    is a candidate rather than a miss.
    """
    found = terms(query)[:MAX_QUERY_TERMS]
    return " | ".join(found) if found else None


def _provenance(mind_id: str | None,
                mind_ids: Sequence[str] | None,
                start: int) -> tuple[str, list[Any]]:
    """The clause scoping a read to a set of minds, as SQL and arguments.

    Three cases, and the difference between the last two is a security
    boundary rather than a convenience:

    * Neither given means no restriction. The console's own pages and the
      nightly pruner read across the hive on purpose.
    * A list, which is how a mind reads its own memories plus the `shared`
      sentinel the hive-wide standing rules are written under. A single
      value could never express that pair.
    * An **empty or blank** selection matches nothing, and this is the part
      lucent gets wrong. Its test is `if mind_id:`, so the empty string an
      unset `MIND_ID` produces is falsy, the clause disappears entirely, and
      the read returns every mind's memories - the kids' boxes alongside the
      operator's. A scoping check that becomes a scoping bypass on exactly
      the value a missing environment variable produces. Blank means "no
      provenance at all" here, and matches nothing.
    """
    if mind_ids is not None:
        if isinstance(mind_ids, (str, bytes)):
            # A `str` *is* a `Sequence[str]`, so no type checker catches
            # this, and iterating one gives its characters: `"skippy"`
            # becomes six one-letter minds and matches nothing. The caller
            # gets an empty recall, which reads as "this mind has no
            # memories about that" — a sentence a model will say out loud.
            raise ValueRefused(
                f"mind_ids was given the single name {mind_ids!r} rather "
                "than a sequence of names. Iterating a string yields its "
                "characters, which would scope the read to minds called "
                "'s', 'k' and so on, and return nothing while looking like "
                "an answer. Pass [name], or use mind_id.")
        names = [str(m).strip() for m in mind_ids if str(m or "").strip()]
        if not names:
            return "1 = 0", []
        return f'"mind_id" = ANY(${start}::text[])', [names]
    if mind_id is None:
        return "", []
    named = str(mind_id).strip()
    if not named:
        return "1 = 0", []
    return f'"mind_id" = ${start}', [named]


def _norm(embedding: Sequence[float]) -> float:
    return math.sqrt(sum(float(v) * float(v) for v in embedding))


def _checked_query_vector(embedding: Sequence[float]) -> list[float]:
    """The query vector, refused if nothing can be measured against it.

    lucent returns `{"error": "zero_query_embedding"}` for this, which is a
    sentence rather than a score. Postgres is worse than a sentence: cosine
    distance against a zero vector is NaN, every comparison with NaN is
    false, so such a row slips past a `min_score` filter that was meant to
    exclude it and arrives with `cosine: NaN` - which `json.dumps` writes as
    a bare `NaN` that a strict reader rejects outright.
    """
    values = [float(v) for v in embedding]
    if not values:
        raise ValueRefused(
            "a recall was given an empty embedding. There is nothing to "
            "measure similarity against, and an empty vector is not a "
            "query that matches everything.")
    if _norm(values) == 0.0:
        raise ValueRefused(
            "a recall was given an all-zero embedding. Cosine distance "
            "against it is NaN rather than a number, NaN compares false "
            "against every threshold, and the result would arrive looking "
            "like a score. The embedding model failing is the usual cause.")
    return values


async def store(*, mind_id: str, content: str,
                embedding: Sequence[float] | None = None,
                tags: Any = UNSET, source: Any = UNSET,
                data_class: Any = UNSET, tier: Any = UNSET,
                as_of: Any = UNSET, expires_at: Any = UNSET,
                superseded: Any = UNSET, recurring: Any = UNSET,
                codebase_ref: Any = UNSET,
                created_at: Any = UNSET,
                unit: Unit | None = None) -> dict[str, Any] | None:
    """Write a memory, and return the identity the database gave it.

    The searchable form of the text is not passed and cannot be: it is a
    generated column, computed by Postgres from the content in this same
    statement. So a memory is findable by word the moment it lands, with no
    second write to forget and no window in which it is stored and
    unsearchable - and nothing above this layer has to know how text search
    is configured.
    """
    # `UNSET` rather than `None` for everything the caller did not name.
    # The two are different writes and the package draws that line
    # everywhere else: an unpassed `superseded` written as an explicit NULL
    # overrides the column's own `DEFAULT 0`, so a memory stored through
    # this layer reads back `None` where every row lucent wrote reads
    # `False`.
    values = _sql.passed({
        "mind_id": mind_id,
        "content": content,
        "embedding": None if embedding is None else [float(v)
                                                     for v in embedding],
        "tags": tags,
        "source": source,
        "data_class": data_class,
        "tier": tier,
        "as_of": as_of,
        "expires_at": expires_at,
        "superseded": superseded,
        "recurring": recurring,
        "codebase_ref": codebase_ref,
        "created_at": created_at,
    })
    sql, args = _sql.insert("memories", values, returning=("id",))
    return await _execute_returning(unit, sql, *args)


async def get(memory_id: int, *, embedding: bool = False,
              unit: Unit | None = None) -> dict[str, Any] | None:
    """One memory by its identity.

    `embedding=True` asks for the vector as well. Off by default because it
    is 4096 numbers and almost nothing wants them; the loader and anything
    re-scoring an existing memory do.
    """
    columns = _READ if not embedding else select_list(
        "memories", schema_public("memories") + ("embedding",))
    return await _fetch_one(
        unit, f'SELECT {columns} FROM {qualified("memories")} WHERE "id" = $1',
        memory_id)


async def delete(memory_id: int, *, unit: Unit | None = None) -> dict[str, int]:
    """Remove one memory, reporting whether there was one."""
    return await _execute(
        unit, f'DELETE FROM {qualified("memories")} WHERE "id" = $1',
        memory_id)


async def similar(embedding: Sequence[float], *, limit: int = VECTOR_POOL,
                  mind_id: str | None = None,
                  mind_ids: Sequence[str] | None = None,
                  unit: Unit | None = None) -> list[dict[str, Any]]:
    """The memories nearest a vector, nearest first, with their similarity.

    Cosine, which is the measure lucent scores with, and not Euclidean
    distance: the two agree on a self-match and disagree the moment two
    vectors differ in magnitude rather than direction.

    An exact scan. pgvector's approximate indexes stop at 2000 dimensions
    and these embeddings are 4096, so there is no index to build; measured
    over the live corpus the scan costs 190ms warm, against a statement
    deadline of 30 seconds.
    """
    values = _checked_query_vector(embedding)
    clause, args = _provenance(mind_id, mind_ids, 2)
    conditions = ['"embedding" IS NOT NULL']
    if clause:
        conditions.append(clause)
    sql = (f'SELECT {_READ}, 1 - ("embedding" <=> $1) AS "score" '
           f'FROM {qualified("memories")} '
           f'WHERE {" AND ".join(conditions)} '
           f'ORDER BY "embedding" <=> $1 '
           f'LIMIT {int(limit)}')
    rows = await _fetch(unit, sql, values, *args)
    # A stored vector of all zeros gives NaN rather than a number, and NaN
    # sorts last, so these are only ever at the tail. Dropped rather than
    # returned with an unorderable score, which is what lucent does with a
    # row whose embedding it cannot use.
    return [r for r in rows if not math.isnan(float(r["score"]))]


async def keyword(query: str, *, limit: int = KEYWORD_SCAN,
                  mind_id: str | None = None,
                  mind_ids: Sequence[str] | None = None,
                  unit: Unit | None = None) -> list[dict[str, Any]]:
    """The memories whose text matches a query's words, best match first.

    The score is `ts_rank` multiplied into the range FTS5's BM25 was
    returning, so the figure means what it meant before.
    """
    parsed = _tsquery(query)
    if parsed is None:
        return []
    clause, args = _provenance(mind_id, mind_ids, 2)
    # The limit is applied ACROSS THE STORE and the provenance filter after
    # it, which is lucent's order and not the better one. lucent asks FTS5
    # for the global top `KEYWORD_SCAN` and then drops whatever does not
    # belong to the minds being read, so a mind holding few rows can end up
    # with fewer candidates than the pool is sized for. Filtering first
    # would hand every mind the full pool - a strictly better answer, and a
    # different one, which is not what a story that ports the blend
    # unchanged gets to do. It is recorded on the story that judges the
    # ranking instead.
    inner = (f'SELECT "id", '
             f'ts_rank("content_search", to_tsquery(\'{TEXT_CONFIG}\', $1)) '
             f'* {KEYWORD_SCALE} AS "score" '
             f'FROM {qualified("memories")} '
             f'WHERE "content_search" @@ to_tsquery(\'{TEXT_CONFIG}\', $1) '
             f'ORDER BY "score" DESC, "id" ASC '
             f'LIMIT {int(limit)}')
    conditions = ['m."id" = hit."id"']
    if clause:
        conditions.append(clause.replace('"mind_id"', 'm."mind_id"'))
    sql = (f'SELECT {", ".join(f"m.{c}" for c in _READ.split(", "))}, '
           f'hit."score" '
           f'FROM ({inner}) hit JOIN {qualified("memories")} m '
           f'ON {" AND ".join(conditions)} '
           f'ORDER BY hit."score" DESC, m."id" ASC')
    return await _fetch(unit, sql, parsed, *args)


def _recency(created_at: Any, now: float) -> float:
    """lucent's decay, unchanged: half-life 30 days, never below half."""
    try:
        age = max(0.0, now - float(created_at or 0))
    except (TypeError, ValueError):
        return RECENCY_FLOOR
    raw = math.exp(-age / (RECENCY_HALF_LIFE_DAYS * 86400))
    return max(RECENCY_FLOOR, raw)


async def recall(*, query: str, embedding: Sequence[float],
                 k: int = DEFAULT_K,
                 mind_id: str | None = None,
                 mind_ids: Sequence[str] | None = None,
                 min_score: float | None = None,
                 debug: bool = True,
                 now: float | None = None,
                 unit: Unit | None = None) -> dict[str, Any]:
    """One blended answer: similarity, recency and keywords together.

    lucent's algorithm, carried across step for step. The candidate pool is
    the top `VECTOR_POOL` by cosine unioned with the top `KEYWORD_POOL` by
    keyword rank; the answer is then allocated in fixed slots - three to the
    best `cosine * recency`, one to the best raw cosine regardless of age,
    one to the best keyword hit - and anything left over is filled from a
    combined ranking.

    `now` is a parameter so the decay can be driven from a test without
    waiting thirty days, and defaults to the wall clock.
    """
    k = max(1, min(k, 20))
    moment = time.time() if now is None else now
    values = _checked_query_vector(embedding)
    if min_score is not None and math.isnan(float(min_score)):
        # The same reasoning `_checked_query_vector` applies to the vector,
        # applied to the threshold beside it: every comparison with NaN is
        # false, so a NaN threshold filters nothing and the caller gets the
        # unfiltered answer with no indication the limit they set was
        # ignored. A config read or a division is how one arrives.
        raise ValueRefused(
            "a recall was given a min_score of NaN. Every comparison with "
            "NaN is false, so the threshold would filter nothing and the "
            "answer would look like one that had been filtered.")

    by_id: dict[int, dict[str, Any]] = {}
    cosine_scores: dict[int, float] = {}
    for row in await similar(values, limit=VECTOR_POOL, mind_id=mind_id,
                             mind_ids=mind_ids, unit=unit):
        row = dict(row)
        cosine_scores[int(row["id"])] = float(row.pop("score"))
        by_id[int(row["id"])] = row

    keyword_scores: dict[int, float] = {}
    # lucent wraps its whole FTS5 lookup and logs "continuing with
    # vector-only" on any failure, so a query the keyword engine cannot
    # parse still returns the memories the vector found. Carried across:
    # without it, the port answers with an error where the thing it
    # replaces answers with results. Not swallowed silently — it is logged,
    # and it never hides a failure of the vector half, which is above.
    try:
        hits = await keyword(query, limit=KEYWORD_SCAN, mind_id=mind_id,
                             mind_ids=mind_ids, unit=unit)
    except HiveDataError:
        log.warning("hive-data: the keyword half of a recall failed; "
                    "answering on similarity alone", exc_info=True)
        hits = []
    for row in hits:
        row = dict(row)
        rid = int(row["id"])
        keyword_scores[rid] = float(row.pop("score"))
        if rid not in by_id:
            by_id[rid] = row
            # A keyword hit outside the vector pool still needs a cosine for
            # the slot arithmetic. lucent has one for every row because it
            # scored the whole store; here the pool stopped at twenty, so
            # the rest are scored on their own.
    outside = [rid for rid in keyword_scores if rid not in cosine_scores]
    if outside:
        sql = (f'SELECT "id", 1 - ("embedding" <=> $1) AS "score" '
               f'FROM {qualified("memories")} '
               f'WHERE "id" = ANY($2::bigint[]) AND "embedding" IS NOT NULL')
        for row in await _fetch(unit, sql, values, outside):
            score = float(row["score"])
            if not math.isnan(score):
                cosine_scores[int(row["id"])] = score
    # A keyword hit whose row has no usable vector is dropped rather than
    # scored zero. lucent builds its candidate set in the vector pass and
    # skips any row whose embedding will not decode, and its keyword scores
    # are only recorded for rows already in that set - so it can never
    # return one. Scoring it 0.0 instead reads as "measured and distant"
    # where the truth is "never measured", and it takes the keyword slot
    # from a memory that was.
    for rid in [r for r in keyword_scores if r not in cosine_scores]:
        del keyword_scores[rid]
        by_id.pop(rid, None)

    top_keyword = [
        rid for rid, _ in sorted(keyword_scores.items(),
                                 key=lambda kv: kv[1], reverse=True)
    ][:KEYWORD_POOL]
    pool_ids = list(dict.fromkeys(list(cosine_scores)[:VECTOR_POOL]
                                  + top_keyword))

    scored: list[dict[str, Any]] = []
    for rid in pool_ids:
        if rid not in by_id:
            continue
        cos = cosine_scores.get(rid, 0.0)
        rec = _recency(by_id[rid].get("created_at"), moment)
        bm = keyword_scores.get(rid, 0.0)
        # A row that fails the similarity threshold is still kept when the
        # words matched: the threshold is about what the vector could not
        # find, and a keyword hit is evidence the vector missed something.
        if min_score is not None and cos < min_score and bm <= 0:
            continue
        scored.append({"rid": rid, "cos": cos, "rec": rec, "bm": bm,
                       "row": by_id[rid]})

    if not scored:
        return {"memories": [], "count": 0, "mode": "hybrid", "pool_size": 0}

    rec_slots = max(0, min(3, k - 2)) if k >= 5 else min(k, 3)
    cos_slots = 1 if k >= 4 else 0
    bm_slots = 1 if k >= 5 else 0
    rec_slots = min(rec_slots, k)
    cos_slots = min(cos_slots, k - rec_slots)
    bm_slots = min(bm_slots, k - rec_slots - cos_slots)

    used: set[int] = set()
    picks: list[tuple[str, dict[str, Any]]] = []

    for entry in sorted(scored, key=lambda s: s["cos"] * s["rec"],
                        reverse=True):
        if len(picks) >= rec_slots:
            break
        used.add(entry["rid"])
        picks.append(("recency", entry))

    taken = 0
    for entry in sorted(scored, key=lambda s: s["cos"], reverse=True):
        if taken >= cos_slots:
            break
        if entry["rid"] in used:
            continue
        used.add(entry["rid"])
        picks.append(("cosine", entry))
        taken += 1

    taken = 0
    for entry in sorted(scored, key=lambda s: s["bm"], reverse=True):
        if taken >= bm_slots or entry["bm"] <= 0:
            break
        if entry["rid"] in used:
            continue
        used.add(entry["rid"])
        picks.append(("bm25", entry))
        taken += 1

    if len(picks) < k:
        combined = sorted(
            scored,
            key=lambda s: (s["cos"] * s["rec"]) + (0.05 * s["bm"]),
            reverse=True)
        for entry in combined:
            if len(picks) >= k:
                break
            if entry["rid"] in used:
                continue
            used.add(entry["rid"])
            picks.append(("fill", entry))

    out = []
    for bucket, entry in picks:
        row = _as_lucent_row(entry["row"])
        if debug:
            row["debug"] = {
                "bucket": bucket,
                "cosine": round(entry["cos"], 4),
                "recency_mult": round(entry["rec"], 4),
                "combined": round(entry["cos"] * entry["rec"], 4),
                "bm25": round(entry["bm"], 4),
            }
        row["score"] = round(entry["cos"], 4)
        out.append(row)

    return {"memories": out, "count": len(out), "mode": "hybrid",
            "pool_size": len(pool_ids)}


# --------------------------------------------------------------- the browser

#: What the memory pages may read, and how wide a page of it may be. The
#: console's memory browser is a table browser: it lists the memory store's
#: own tables with their row counts and pages through any one of them. On
#: SQLite that was `sqlite_master` and `PRAGMA table_info`; here it is the
#: database's own catalogue, scoped to this one schema.
BROWSE_SCHEMA = "memory"

#: The widest page the browser will answer, whatever it was asked for. The
#: page has no paging of its own beyond this, so a larger number is a longer
#: page rather than more information - and `memories` holds a column of
#: 4096 floats per row, which is why the ceiling is low rather than absent.
BROWSE_LIMIT_MAX = 1000

#: Types no page can render, and what is shown instead of the value. An
#: embedding is 4096 floats and a `tsvector` is the content again as lexeme
#: positions; `bytea` is whatever bytes somebody stored. The SQLite browser
#: showed `<BLOB n bytes>` for exactly this reason, and the summary is
#: computed in the database so the bytes never cross the wire at all.
#:
#: Keyed by `udt_name`, as Postgres spells it.
_SUMMARISED: dict[str, str] = {
    "vector": '\'<vector \' || vector_dims("{c}") || \' dims>\'',
    "tsvector": '\'<tsvector \' || length("{c}") || \' lexemes>\'',
    "bytea": '\'<BLOB \' || octet_length("{c}") || \' bytes>\'',
}


async def _browsable(unit: Unit | None = None) -> dict[str, list[tuple[str, str]]]:
    """Every table in the memory schema, with each column and its type.

    Read from the catalogue rather than declared, because this is a browser:
    a table added to the memory schema should appear on the page rather than
    be invisible until somebody remembers to name it here. The catalogue is
    also what makes the identifiers safe - a table or column named on a
    request is only ever used after it has been matched against this answer,
    never quoted and hoped for.
    """
    rows = await _fetch(
        unit,
        'SELECT c."table_name", c."column_name", c."udt_name" '
        'FROM information_schema."columns" AS c '
        'JOIN information_schema."tables" AS t '
        '  ON t."table_schema" = c."table_schema" '
        ' AND t."table_name" = c."table_name" '
        'WHERE c."table_schema" = $1 AND t."table_type" = $2 '
        'ORDER BY c."table_name", c."ordinal_position"',
        BROWSE_SCHEMA, "BASE TABLE")
    out: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        out.setdefault(row["table_name"], []).append(
            (row["column_name"], row["udt_name"]))
    return out


async def _browse_order(unit: Unit | None = None) -> dict[str, list[str]]:
    """The columns each memory table is paged by, in key order.

    A page needs an order or it is not a page. SQLite's browser named none
    and got away with it: an unordered scan of a rowid table comes back in
    rowid order in practice, so clicking to page two showed the next rows.
    Postgres promises nothing of the sort - the same query may use a
    different plan on the second call - so paging without an order silently
    repeats some rows and skips others, which on a browser built to check a
    load is the one failure that would be read as a bad load.

    The primary key, since every table in this schema has one and it is
    unique by construction. A table that somehow has none falls back to its
    whole column list, which is an order even when it is not a cheap one.
    """
    rows = await _fetch(
        unit,
        'SELECT c."table_name", k."column_name" '
        'FROM information_schema."table_constraints" AS c '
        'JOIN information_schema."key_column_usage" AS k '
        '  ON k."constraint_name" = c."constraint_name" '
        ' AND k."constraint_schema" = c."constraint_schema" '
        'WHERE c."constraint_schema" = $1 AND c."constraint_type" = $2 '
        'ORDER BY c."table_name", k."ordinal_position"',
        BROWSE_SCHEMA, "PRIMARY KEY")
    out: dict[str, list[str]] = {}
    for row in rows:
        out.setdefault(row["table_name"], []).append(row["column_name"])
    return out


async def tables(*, unit: Unit | None = None) -> list[dict[str, Any]]:
    """The memory store's tables, each with how many rows it holds.

    One count per table rather than an estimate off the planner's
    statistics: the page exists to be looked at after a load, and an
    estimate that is right to within a few per cent cannot answer the
    question the operator is actually asking.
    """
    found = await _browsable(unit)
    out: list[dict[str, Any]] = []
    for name in sorted(found):
        row = await _fetch_one(
            unit, f'SELECT count(*) AS "n" FROM {BROWSE_SCHEMA}."{name}"')
        assert row is not None  # an aggregate always answers
        out.append({"name": name, "row_count": int(row["n"])})
    return out


async def rows(table: str, *, limit: int = 200, offset: int = 0,
               unit: Unit | None = None) -> dict[str, Any] | None:
    """One page of a memory table, with its columns and its total.

    `None` when the memory schema has no such table, which is the answer the
    console turns into a 404 - a name that matches nothing is a typo in a
    URL, not a failure of the store.

    The limit and offset are clamped rather than refused. They arrive as
    query parameters on a page somebody is clicking through, and a browser
    that answers 400 to `?limit=0` is a page that breaks on its own
    controls.
    """
    found = await _browsable(unit)
    columns = found.get(table)
    if columns is None:
        return None
    safe_limit = max(1, min(int(limit), BROWSE_LIMIT_MAX))
    safe_offset = max(0, int(offset))
    chosen = []
    for name, udt in columns:
        summary = _SUMMARISED.get(udt)
        if summary is None:
            chosen.append(f'"{name}"')
        else:
            chosen.append(summary.format(c=name) + f' AS "{name}"')
    keys = (await _browse_order(unit)).get(table) or [
        n for n, udt in columns if udt not in _SUMMARISED]
    order = (" ORDER BY " + ", ".join(f'"{name}"' for name in keys)
             if keys else "")
    count = await _fetch_one(
        unit, f'SELECT count(*) AS "n" FROM {BROWSE_SCHEMA}."{table}"')
    assert count is not None
    page = await _fetch(
        unit,
        f'SELECT {", ".join(chosen)} FROM {BROWSE_SCHEMA}."{table}" '
        f"{order} LIMIT $1 OFFSET $2", safe_limit, safe_offset)
    return {
        "table": table,
        "columns": [name for name, _ in columns],
        "rows": page,
        "total": int(count["n"]),
        "limit": safe_limit,
        "offset": safe_offset,
    }
