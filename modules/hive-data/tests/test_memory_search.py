"""Recall without FTS5: pgvector for the vectors, Postgres for the words.

Every vector here is built by hand. The layer takes an embedding as given and
never generates one, so nothing in this file needs an embedding model
running — which is also what makes the similarity assertions exact rather
than approximate-and-hopeful.
"""

from __future__ import annotations

import math
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

import hive_data
from hive_data import memory

pytestmark = pytest.mark.asyncio

#: The width the column declares. A vector of any other length is refused by
#: Postgres, which is the point of declaring it.
DIMENSIONS = 4096


def direction(cosine: float, scale: float = 1.0) -> list[float]:
    """A vector whose cosine against `unit()` is exactly `cosine`.

    Two non-zero components and the rest zero, so the arithmetic is a right
    triangle and the expected similarity is readable from the call.
    """
    values = [0.0] * DIMENSIONS
    values[0] = cosine * scale
    values[1] = math.sqrt(max(0.0, 1.0 - cosine * cosine)) * scale
    return values


def unit() -> list[float]:
    """The query vector every test measures against."""
    return direction(1.0)


async def _store(mind_id: str, content: str, embedding, created_at: int):
    result = await memory.store(mind_id=mind_id, content=content,
                                embedding=embedding, created_at=created_at,
                                data_class="test", tier="contextual",
                                source="test")
    assert result["rows"] == 1
    return int(result["row"]["id"])


# --------------------------------------------------------------------------
# Similarity
# --------------------------------------------------------------------------


async def test_a_stored_memory_is_found_by_its_own_vector_at_full_similarity(
        connected, clock):
    """Cosine, not Euclidean distance. A self-match scores 1 under either, so
    the second memory points the same way at ten times the magnitude: cosine
    calls it nearly identical and Euclidean distance calls it the furthest
    thing in the table."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    same = await _store(mind, "the first memory", unit(), int(clock))
    louder = await _store(mind, "the second memory",
                          direction(0.9987, scale=10.0), int(clock))
    quieter = await _store(mind, "the third memory", direction(0.6),
                           int(clock))

    found = await memory.similar(unit(), mind_id=mind)
    scores = {int(r["id"]): float(r["score"]) for r in found}

    assert scores[same] == pytest.approx(1.0, abs=1e-6)
    assert scores[louder] == pytest.approx(0.9987, abs=1e-4)
    assert scores[quieter] == pytest.approx(0.6, abs=1e-4)
    assert [int(r["id"]) for r in found] == [same, louder, quieter]


async def test_a_similarity_search_orders_by_direction_not_by_distance(
        connected, clock):
    """The rows go in deliberately backwards, so an implementation that does
    not sort at all would hand back a sequential scan of the heap and pass.
    The off-magnitude row is second by cosine and last by Euclidean
    distance, so the order also says which measure ran."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    orthogonal = await _store(mind, "d", direction(0.0), int(clock))
    middling = await _store(mind, "c", direction(0.6), int(clock))
    louder = await _store(mind, "b", direction(0.9987, scale=10.0),
                          int(clock))
    nearest = await _store(mind, "a", unit(), int(clock))

    found = await memory.similar(unit(), mind_id=mind)

    assert [int(r["id"]) for r in found] == [
        nearest, louder, middling, orthogonal]


async def test_a_memory_whose_vector_is_all_zeros_is_left_out_of_the_results(
        connected, clock):
    """Cosine distance against a zero vector is NaN rather than a number, and
    every comparison with NaN is false — so such a row slips through a
    threshold meant to exclude it and arrives carrying `NaN` where a score
    belongs, which `json.dumps` writes as a bare `NaN` a strict reader
    rejects."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    real = await _store(mind, "a real memory", unit(), int(clock))
    await _store(mind, "a memory with no direction", [0.0] * DIMENSIONS,
                 int(clock))

    found = await memory.similar(unit(), mind_id=mind)

    assert [int(r["id"]) for r in found] == [real]


async def test_a_recall_given_an_all_zero_query_vector_is_refused(connected):
    """The embedding model failing is the usual cause, and the answer must
    not be a list of memories scored NaN."""
    with pytest.raises(hive_data.ValueRefused):
        await memory.recall(query="anything", embedding=[0.0] * DIMENSIONS)


# --------------------------------------------------------------------------
# Words
# --------------------------------------------------------------------------


async def test_a_memory_is_found_by_a_word_in_its_text(connected, clock):
    """The one-word row goes in first, so a search that does not actually
    filter would return it too."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    await _store(mind, "a note about telephones", direction(0.5), int(clock))
    wanted = await _store(mind, "a note about tendinopathy", direction(0.5),
                          int(clock))

    found = await memory.keyword("tendinopathy", mind_id=mind)

    assert [int(r["id"]) for r in found] == [wanted]


async def test_a_word_search_matches_the_other_forms_of_the_same_word(
        connected, clock):
    """FTS5 is configured `porter unicode61` today, so the live index holds
    `rotat` and no `rotation` at all — a search for "rotation" matches every
    inflection. An unstemmed configuration would match only the literal
    word and silently narrow every keyword result in the hive."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    inflected = await _store(mind, "the pane is rotating now", direction(0.5),
                             int(clock))
    unrelated = await _store(mind, "the pane is wedged", direction(0.5),
                             int(clock))

    found = {int(r["id"]) for r in await memory.keyword("rotation",
                                                       mind_id=mind)}

    assert inflected in found
    assert unrelated not in found


async def test_a_word_search_ignores_the_accents_on_a_name(connected, clock):
    """`unicode61` folds diacritics by default, so the live index holds
    `jose`, and 4,097 of the live rows carry non-ASCII bytes. Postgres folds
    nothing unless told to."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    accented = await _store(mind, "lunch with José in Montréal",
                            direction(0.5), int(clock))

    found = await memory.keyword("jose", mind_id=mind)

    assert [int(r["id"]) for r in found] == [accented]


async def test_a_memory_matching_two_of_the_query_words_outranks_one(
        connected, clock):
    """Both rows are the same length, so this is about how many distinct
    words matched and not about how long the text is. The one-word row goes
    in first, so an implementation that returns matches unranked fails."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    one = await _store(mind, "tendinopathy of the elbow joint capsule x",
                       direction(0.5), int(clock))
    both = await _store(mind, "tendinopathy and bursitis in one elbow x",
                        direction(0.5), int(clock))

    found = await memory.keyword("tendinopathy bursitis", mind_id=mind)

    assert [int(r["id"]) for r in found] == [both, one]


async def test_the_keyword_score_is_reported_on_the_scale_it_replaces(
        connected, clock):
    """Postgres has no BM25. Measured over the live corpus, FTS5's `-bm25()`
    returns 6.4 to 8.8 for a real query where `ts_rank` returns 0.040 to
    0.061 — and that figure is both added to the combined score with a fixed
    coefficient and rendered into the model's injected context every turn.
    Kept in the range the surrounding arithmetic was written against."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    await _store(mind, "tendinopathy and bursitis", direction(0.5),
                 int(clock))

    found = await memory.keyword("tendinopathy bursitis", mind_id=mind)

    assert float(found[0]["score"]) > 1.0


async def test_a_query_of_nothing_but_filler_words_matches_on_words_nowhere(
        connected, clock):
    """The configuration keeps stopwords in the documents, exactly as FTS5
    does, so a query passed through raw would match these rows on "the" and
    "of". The stoplist is applied to the query instead."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    await _store(mind, "the shape of the thing", direction(0.9), int(clock))

    assert await memory.keyword("the and of is are", mind_id=mind) == []


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


async def test_a_stored_memory_is_searchable_by_word_without_a_second_write(
        connected, clock, inspector):
    """Read back with a plain driver connection rather than through this
    package: a layer that wrote and read the same wrong thing would pass its
    own round trip."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    stored = await _store(mind, "a memory about tendinopathy", unit(),
                          int(clock))

    indexed = await inspector.fetchval(
        'SELECT "content_search"::text FROM memory."memories" '
        'WHERE "id" = $1', stored)

    assert "tendinopathi" in indexed or "tendinopathy" in indexed


async def test_a_write_naming_the_searchable_column_is_refused(connected):
    """It is GENERATED ALWAYS, so Postgres refuses a value for it outright.
    Refused here instead, with a sentence saying why."""
    with pytest.raises(hive_data.IdentityNotWritable):
        await hive_data.run(lambda c: c.fetch(*__import__(
            "hive_data._sql", fromlist=["insert"]).insert(
                "memories",
                {"mind_id": "m", "content": "x", "content_search": "y"})))


async def test_a_memory_too_long_to_index_whole_is_stored_and_still_found(
        connected, clock, inspector):
    """A tsvector is hard-limited to 1MB and `to_tsvector` over a big enough
    paste raises — which on a generated column fails the INSERT and loses
    the memory outright. Capped instead: the whole text is stored, and the
    first 100,000 characters are what can be searched. An unfindable memory
    is recoverable; a refused one is gone."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    early = "wordaaa000001"
    late = "wordzzz999999"
    filler = " ".join(f"wordfill{n:06d}" for n in range(200_000))
    content = f"{early} {filler} {late}"
    assert len(content) > 2_000_000

    stored = await _store(mind, content, unit(), int(clock))

    whole = await inspector.fetchval(
        'SELECT "content" FROM memory."memories" WHERE "id" = $1', stored)
    assert whole == content

    assert [int(r["id"]) for r in
            await memory.keyword(early, mind_id=mind)] == [stored]
    assert await memory.keyword(late, mind_id=mind) == []


async def test_an_embedding_comes_back_as_numbers_rather_than_as_text(
        connected, clock):
    """With no codec registered on the connection asyncpg hands a vector
    column back as a 50,000-character string, and nothing raises: the
    legacy decode path in lucent accepts a string, so the whole thing
    "works" while a length check measures characters instead of
    dimensions."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    stored = await _store(mind, "a memory", direction(0.5), int(clock))

    assert "embedding" not in await memory.get(stored)
    row = await memory.get(stored, embedding=True)

    assert isinstance(row["embedding"], list)
    assert len(row["embedding"]) == DIMENSIONS
    assert row["embedding"][0] == pytest.approx(0.5, abs=1e-6)


# --------------------------------------------------------------------------
# Whose memories
# --------------------------------------------------------------------------


async def test_a_recall_is_scoped_to_the_minds_it_names(connected, clock):
    mind_a = f"mind-{uuid.uuid4().hex[:8]}"
    mind_b = f"mind-{uuid.uuid4().hex[:8]}"
    a = await _store(mind_a, "a's memory", unit(), int(clock))
    b = await _store(mind_b, "b's memory", unit(), int(clock))

    only_a = await memory.similar(unit(), mind_id=mind_a)
    both = await memory.similar(unit(), mind_ids=[mind_a, mind_b])

    assert [int(r["id"]) for r in only_a] == [a]
    assert {int(r["id"]) for r in both} == {a, b}


async def test_the_hive_wide_memories_arrive_only_when_they_are_named(
        connected, clock):
    """`shared` is the provenance the hive-wide standing rules are written
    under. A mind reads its own plus that one, which is why the plural form
    exists at all — a single value could never express the pair."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    own = await _store(mind, "my own memory", unit(), int(clock))
    shared = await _store("shared", "a rule for the whole hive", unit(),
                          int(clock))

    alone = await memory.similar(unit(), mind_id=mind)
    with_shared = await memory.similar(unit(), mind_ids=[mind, "shared"])

    assert [int(r["id"]) for r in alone] == [own]
    assert {int(r["id"]) for r in with_shared} == {own, shared}


async def test_a_recall_naming_no_minds_at_all_returns_nothing(
        connected, clock):
    """An empty selection means no provenance, which matches nothing.
    Collapsing it to "no restriction" is how a scoping check becomes a
    scoping bypass."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    await _store(mind, "a memory", unit(), int(clock))

    assert await memory.similar(unit(), mind_ids=[]) == []


async def test_a_recall_whose_mind_is_the_empty_string_returns_nothing(
        connected, clock):
    """The exact value an unset `MIND_ID` produces. lucent tests the name
    for truthiness, so the empty string drops the clause entirely and the
    read returns every mind's memories — the kids' boxes alongside the
    operator's."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    await _store(mind, "a memory", unit(), int(clock))

    assert await memory.similar(unit(), mind_id="") == []
    assert await memory.similar(unit(), mind_id="   ") == []
    blank = await memory.recall(query="memory", embedding=unit(), mind_id="")
    assert blank["memories"] == []


# --------------------------------------------------------------------------
# The blend
# --------------------------------------------------------------------------


async def test_a_recall_prefers_recent_memories_but_keeps_one_slot_for_the_most_similar(
        connected, clock):
    """Three slots go to the best similarity-times-recency and one to the
    best raw similarity regardless of age. The old row here is the most
    similar of the four and still loses all three recency slots, because the
    decay floor halves it — so finding it in the similarity slot is what
    says both halves of the arithmetic ran."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    now = float(clock)
    year = int(now - 365 * 86400)
    old_and_closest = await _store(mind, "an old memory", direction(0.9),
                                   year)
    recent = [await _store(mind, f"a recent memory {n}", direction(c),
                           int(now))
              for n, c in enumerate((0.8, 0.7, 0.6))]

    answer = await memory.recall(query="memory", embedding=unit(),
                                 mind_id=mind, now=now)
    buckets = {int(r["id"]): r["debug"]["bucket"] for r in answer["memories"]}

    assert [buckets[r] for r in recent] == ["recency"] * 3
    assert buckets[old_and_closest] == "cosine"
    assert next(r for r in answer["memories"]
                if int(r["id"]) == old_and_closest
                )["debug"]["recency_mult"] == pytest.approx(
                    memory.RECENCY_FLOOR)


async def test_a_recall_keeps_a_slot_for_a_memory_the_vector_could_not_find(
        connected, clock):
    """The keyword slot's whole job. This memory is orthogonal to the query
    vector, so similarity puts it last of five and it reaches the answer
    only because its words matched."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    now = float(clock)
    for n, c in enumerate((0.9, 0.8, 0.7, 0.6)):
        await _store(mind, f"a close memory {n}", direction(c), int(now))
    words_only = await _store(mind, "tendinopathy of the left elbow",
                              direction(0.0), int(now))

    answer = await memory.recall(query="tendinopathy", embedding=unit(),
                                 mind_id=mind, now=now)
    by_bucket = {r["debug"]["bucket"]: int(r["id"])
                 for r in answer["memories"]}

    assert by_bucket["bm25"] == words_only


async def test_a_recall_reports_the_fields_its_readers_name(connected, clock):
    """The hooks render these by name into the model's context every turn,
    and a renamed field reaches them as a missing key read as absence."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    await _store(mind, "tendinopathy of the elbow", unit(), int(clock))

    answer = await memory.recall(query="tendinopathy", embedding=unit(),
                                 mind_id=mind, now=float(clock))

    assert set(answer) == {"memories", "count", "mode", "pool_size"}
    assert answer["mode"] == "hybrid"
    assert answer["count"] == len(answer["memories"])
    row = answer["memories"][0]
    assert set(row["debug"]) == {
        "bucket", "cosine", "recency_mult", "combined", "bm25"}
    assert "score" in row
    assert "content_search" not in row


async def test_a_recall_asked_for_no_debugging_reports_none(connected, clock):
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    await _store(mind, "a memory", unit(), int(clock))

    answer = await memory.recall(query="memory", embedding=unit(),
                                 mind_id=mind, debug=False, now=float(clock))

    assert "debug" not in answer["memories"][0]


async def test_a_recall_returns_no_more_rows_than_asked_for_and_never_over_twenty(
        connected, clock):
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    now = float(clock)
    for n in range(25):
        await _store(mind, f"memory number {n}", direction(0.5 + n / 100),
                     int(now))

    one = await memory.recall(query="memory", embedding=unit(),
                              mind_id=mind, k=1, now=now)
    many = await memory.recall(query="memory", embedding=unit(),
                               mind_id=mind, k=100, now=now)

    assert one["count"] == 1
    assert many["count"] == 20


async def test_a_recall_for_a_mind_with_no_memories_reports_an_empty_answer(
        connected, clock):
    """Named fields rather than a bare list: the readers index by key."""
    answer = await memory.recall(query="anything", embedding=unit(),
                                 mind_id=f"mind-{uuid.uuid4().hex[:8]}",
                                 now=float(clock))

    assert answer == {"memories": [], "count": 0, "mode": "hybrid",
                      "pool_size": 0}


async def test_a_filler_only_query_still_returns_memories_on_similarity_alone(
        connected, clock):
    """Nothing matched on words, so every row's keyword component is zero and
    the ranking that produced them was the vector's."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    await _store(mind, "the shape of the thing", direction(0.9), int(clock))
    await _store(mind, "another shape entirely", direction(0.4), int(clock))

    answer = await memory.recall(query="the and of is are", embedding=unit(),
                                 mind_id=mind, now=float(clock))

    assert answer["count"] == 2
    assert [r["debug"]["bm25"] for r in answer["memories"]] == [0, 0]


async def test_a_similarity_threshold_still_admits_a_memory_the_words_found(
        connected, clock):
    """The threshold is about what the vector could not find, so a keyword
    hit is evidence the vector missed something rather than a row to drop."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    now = float(clock)
    close = await _store(mind, "a close memory", direction(0.9), int(now))
    far_but_worded = await _store(mind, "tendinopathy of the left elbow",
                                  direction(0.1), int(now))

    answer = await memory.recall(query="tendinopathy", embedding=unit(),
                                 mind_id=mind, min_score=0.5, now=now)
    returned = {int(r["id"]) for r in answer["memories"]}

    assert returned == {close, far_but_worded}


# --------------------------------------------------------------------------
# What the pool refuses to open against
# --------------------------------------------------------------------------


async def test_a_memory_schema_whose_embedding_is_still_bytes_refuses_at_startup(
        scratch_dsn, monkeypatch):
    """The drift check compares names, and `bytea` passes a name check. What
    arrives then is raw bytes where numbers belong, or — with the codec
    registered against a column that is not a vector — a string, and
    neither raises. Every similarity number is then wrong in a way that
    reads as a working search."""
    name = f"hive_data_other_{uuid.uuid4().hex[:8]}"
    parts = urlsplit(scratch_dsn)
    admin = await asyncpg.connect(scratch_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    dsn = urlunsplit(parts._replace(path=f"/{name}"))
    target = await asyncpg.connect(dsn)
    try:
        from tests.conftest import MEMORY_SCHEMA_SQL, SCHEMA_SQL
        await target.execute(SCHEMA_SQL.read_text())
        await target.execute(MEMORY_SCHEMA_SQL.read_text())
        await target.execute(
            'ALTER TABLE memory."memories" DROP COLUMN "embedding"')
        await target.execute(
            'ALTER TABLE memory."memories" ADD COLUMN "embedding" bytea')
    finally:
        await target.close()

    monkeypatch.setenv(hive_data.config.DSN_VAR, dsn)
    try:
        with pytest.raises(hive_data.SchemaMismatch) as refusal:
            await hive_data.connect()
        assert "embedding" in str(refusal.value)
    finally:
        await hive_data.close(drain_timeout=5.0)
        admin = await asyncpg.connect(scratch_dsn)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()", name)
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


async def test_a_database_with_no_memory_schema_still_opens_for_everyone_else(
        scratch_dsn, monkeypatch):
    """Comms runs `core` and nothing else. A memory-side declaration that
    refused to open a pool for it would make one context's type decision
    break every service in the hive."""
    name = f"hive_data_other_{uuid.uuid4().hex[:8]}"
    parts = urlsplit(scratch_dsn)
    admin = await asyncpg.connect(scratch_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    dsn = urlunsplit(parts._replace(path=f"/{name}"))
    target = await asyncpg.connect(dsn)
    try:
        from tests.conftest import SCHEMA_SQL
        await target.execute(SCHEMA_SQL.read_text())
    finally:
        await target.close()

    monkeypatch.setenv(hive_data.config.DSN_VAR, dsn)
    try:
        await hive_data.connect()
        assert await hive_data.core.list_minds() == []
        with pytest.raises(hive_data.SchemaMismatch):
            await memory.similar(unit(), mind_id="whoever")
    finally:
        await hive_data.close(drain_timeout=5.0)
        admin = await asyncpg.connect(scratch_dsn)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()", name)
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


# --------------------------------------------------------------------------
# What the mutation reviewer proved nothing was guarding
# --------------------------------------------------------------------------


async def test_the_index_keeps_the_filler_words_the_query_filter_removes(
        connected, clock, inspector):
    """The decision the whole text configuration argument turns on, and it was
    unguarded: a stopword dictionary on the document side strips words FTS5
    indexes, so a query word in Postgres's list but not in lucent's — `about`,
    `same`, `than`, `such` — would match nothing where it matches today.
    Asserted on the stored tsvector, because the query-side stoplist hides
    this from every behavioural test."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    stored = await _store(mind, "a note about the thing", unit(), int(clock))

    indexed = await inspector.fetchval(
        'SELECT "content_search"::text FROM memory."memories" '
        'WHERE "id" = $1', stored)

    assert "'about'" in indexed
    assert "'the'" in indexed


async def test_a_query_word_outside_lucents_stoplist_still_finds_its_memory(
        connected, clock):
    """`about` is not in lucent's eighty-word list, so it reaches Postgres as
    a search term and has to match. The same break seen from the caller's
    side rather than the index's."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    wanted = await _store(mind, "a note about tendinopathy", unit(),
                          int(clock))
    await _store(mind, "a note concerning tendinopathy", unit(), int(clock))

    found = await memory.keyword("about", mind_id=mind)

    assert [int(r["id"]) for r in found] == [wanted]


async def test_a_similarity_threshold_excludes_what_neither_half_found(
        connected, clock):
    """The other side of the threshold. The suite guarded the keyword bypass
    and not the threshold itself, so removing the filter outright passed."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    now = float(clock)
    close = await _store(mind, "a close memory", direction(0.9), int(now))
    await _store(mind, "an unrelated memory", direction(0.1), int(now))

    answer = await memory.recall(query="zzzznomatch", embedding=unit(),
                                 mind_id=mind, min_score=0.5, now=now)

    assert [int(r["id"]) for r in answer["memories"]] == [close]


async def test_a_recall_given_a_threshold_of_nan_is_refused(connected):
    """Every comparison with NaN is false, so the threshold would filter
    nothing and the answer would look like one that had been filtered."""
    with pytest.raises(hive_data.ValueRefused):
        await memory.recall(query="anything", embedding=unit(),
                            min_score=float("nan"))


async def test_the_keyword_score_weighs_on_the_leftover_ranking(
        connected, clock):
    """The backfill adds a fraction of the keyword score to the combined one,
    which is the only reason that score is rescaled at all — and it was
    unguarded, so the term could be deleted outright.

    The two rows under test tie exactly on similarity and on age, and the
    keyword slot is already spoken for by a stronger match, so the only thing
    that can order them is the backfill."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    now = float(clock)
    for n in range(3):
        await _store(mind, f"a filler memory {n}", direction(0.95 - n / 100),
                     int(now))
    await _store(mind, "another filler", direction(0.9), int(now))
    await _store(mind, "tendinopathy bursitis elbow pain and swelling",
                 direction(0.7), int(now))
    plain = await _store(mind, "aaa bbb ccc ddd", direction(0.5), int(now))
    worded = await _store(mind, "tendinopathy xxx yyy zzz", direction(0.5),
                          int(now))

    answer = await memory.recall(query="tendinopathy bursitis",
                                 embedding=unit(), mind_id=mind, k=20,
                                 now=now)
    order = [int(r["id"]) for r in answer["memories"]]
    buckets = {int(r["id"]): r["debug"]["bucket"] for r in answer["memories"]}

    assert buckets[worded] == "fill"
    assert buckets[plain] == "fill"
    assert order.index(worded) < order.index(plain)


async def test_punctuation_a_caller_typed_never_reaches_the_query_parser(
        connected, clock):
    """`to_tsquery` reads `&`, `|`, `!` and `<->` as syntax. It does not
    raise on them — it silently reinterprets, so a typed `!` turns an OR
    search into an AND-NOT and quietly drops matches."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    both = await _store(mind, "tendinopathy of the elbow", unit(),
                        int(clock))
    one = await _store(mind, "tendinopathy of the shoulder", unit(),
                       int(clock))

    assert memory.terms("rotation & !pane <-> x") == ["rotation", "pane"]
    found = {int(r["id"])
             for r in await memory.keyword("tendinopathy & !elbow",
                                           mind_id=mind)}

    assert found == {both, one}


async def test_a_recall_naming_no_mind_at_all_reads_across_the_hive(
        connected, clock):
    """The console's own pages and the nightly pruner do this on purpose.
    Every other scoping test names a mind, so turning the unrestricted read
    into "match nothing" passed the whole suite."""
    mind_a = f"mind-{uuid.uuid4().hex[:8]}"
    mind_b = f"mind-{uuid.uuid4().hex[:8]}"
    a = await _store(mind_a, "a's memory", unit(), int(clock))
    b = await _store(mind_b, "b's memory", unit(), int(clock))

    found = {int(r["id"]) for r in await memory.similar(unit())}

    assert {a, b} <= found


async def test_a_single_mind_name_passed_as_a_list_of_one_is_refused(
        connected):
    """A `str` is a `Sequence[str]`, so no type checker catches it and
    iterating one gives its characters: the read scopes to minds called 's',
    'k' and so on, returns nothing, and reads as "no memories about that"."""
    with pytest.raises(hive_data.ValueRefused):
        await memory.similar(unit(), mind_ids="skippy")


async def test_a_memory_stored_without_a_vector_is_skipped_not_crashed_on(
        connected, clock):
    """`store` accepts no embedding — the loader writes rows before their
    vectors exist. Without the guard, scoring one computes a float of None."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    real = await _store(mind, "a memory with a vector", unit(), int(clock))
    await memory.store(mind_id=mind, content="a memory with none",
                       created_at=int(clock))

    assert [int(r["id"]) for r in await memory.similar(unit(), mind_id=mind)
            ] == [real]


async def test_a_memory_with_no_vector_never_takes_the_keyword_slot(
        connected, clock):
    """lucent builds its candidates in the vector pass and records keyword
    scores only for rows already there, so it can never return one. Scoring
    it zero reads as "measured and distant" where the truth is "never
    measured", and takes the slot from a memory that was."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    now = float(clock)
    for n in range(4):
        await _store(mind, f"a close memory {n}", direction(0.9 - n / 100),
                     int(now))
    measured = await _store(mind, "tendinopathy measured", direction(0.0),
                            int(now))
    await memory.store(mind_id=mind, content="tendinopathy unmeasured",
                       created_at=int(now))

    answer = await memory.recall(query="tendinopathy", embedding=unit(),
                                 mind_id=mind, now=now)
    by_bucket = {r["debug"]["bucket"]: int(r["id"])
                 for r in answer["memories"]}

    assert by_bucket["bm25"] == measured


async def test_a_keyword_hit_outside_the_vector_pool_is_scored_for_itself(
        connected, clock):
    """lucent has a cosine for every row because it scores the whole store;
    here the vector pool stops at twenty, so a keyword hit past it is scored
    on its own. Nothing had more than twenty rows, so the branch never ran."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    now = float(clock)
    for n in range(25):
        await _store(mind, f"a filler memory {n}", direction(0.9 - n / 1000),
                     int(now))
    far = await _store(mind, "tendinopathy of the elbow", direction(0.2),
                       int(now))

    answer = await memory.recall(query="tendinopathy", embedding=unit(),
                                 mind_id=mind, now=now)
    row = next(r for r in answer["memories"] if int(r["id"]) == far)

    assert row["debug"]["cosine"] == pytest.approx(0.2, abs=1e-3)


async def test_the_keyword_slot_goes_empty_rather_than_to_a_row_no_word_matched(
        connected, clock):
    """The bucket label is rendered into the model's context every turn, so a
    row labelled `bm25` that matched no word is a claim about why it is
    there."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    now = float(clock)
    for n in range(5):
        await _store(mind, f"a filler memory {n}", direction(0.9 - n / 100),
                     int(now))

    answer = await memory.recall(query="zzzznomatch", embedding=unit(),
                                 mind_id=mind, now=now)
    buckets = [r["debug"]["bucket"] for r in answer["memories"]]

    assert "bm25" not in buckets


async def test_a_recall_reports_the_raw_similarity_and_the_pool_it_chose_from(
        connected, clock):
    """`score` is the raw cosine, not the recency-weighted product, and
    `pool_size` is how many candidates were considered. Both were asserted
    to exist and never to be right."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    now = float(clock) - 365 * 86400
    stored = await _store(mind, "an old memory", direction(0.8), int(now))

    answer = await memory.recall(query="memory", embedding=unit(),
                                 mind_id=mind, now=float(clock))
    row = answer["memories"][0]

    assert int(row["id"]) == stored
    assert row["score"] == pytest.approx(0.8, abs=1e-3)
    assert row["debug"]["recency_mult"] == pytest.approx(memory.RECENCY_FLOOR)
    assert answer["pool_size"] == 1


async def test_a_vector_of_the_wrong_width_is_refused(connected, clock):
    """The column declares 4096. A bare `vector` would take a row of any
    width and the mismatch would then arrive at query time, on every recall,
    as an error about a row nobody can identify."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"

    with pytest.raises(hive_data.ValueRefused):
        await memory.store(mind_id=mind, content="too short",
                           embedding=[1.0, 0.0, 0.0], created_at=int(clock))


async def test_a_recall_row_carries_the_keys_lucents_callers_read_and_no_more(
        connected, clock):
    """lucent builds its rows by hand. A key it never returned is a key a
    reader did not budget for — and the embedding is 4096 of them."""
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    await _store(mind, "a memory", unit(), int(clock))

    answer = await memory.recall(query="memory", embedding=unit(),
                                 mind_id=mind, now=float(clock))
    row = answer["memories"][0]

    assert set(row) == set(memory._RECALL_KEYS) | {"score", "debug"}
    assert row["superseded"] is False


async def test_a_memory_is_deleted_and_the_deletion_is_reported(
        connected, clock):
    mind = f"mind-{uuid.uuid4().hex[:8]}"
    stored = await _store(mind, "a memory", unit(), int(clock))

    assert (await memory.delete(stored))["rows"] == 1
    assert await memory.get(stored) is None
    assert (await memory.delete(stored))["rows"] == 0
