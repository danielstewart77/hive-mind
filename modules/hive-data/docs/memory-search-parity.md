# Memory search parity: FTS5 against Postgres

Generated 2026-10-26T21:20:11 by `tools/memory_search_parity.py`.

* corpus: **7912** memories, snapshotted from the live lucent store and loaded into a scratch Postgres database this script created and dropped
* queries: **10** real ones, each embedded once and handed to both engines, so any difference below is a difference in the search
* identical top-5 **in the same order**: **3/10**
* mean overlap in the top 5: **86%**
* largest disagreement on a shared row's cosine: **1.00e-04**

Cosine is the half that can be expected to agree exactly, and the figure above says it does to well inside the four decimal places the response rounds to. The keyword half cannot agree exactly: Postgres has no BM25, so `ts_rank` is a different formula rescaled into the range the surrounding arithmetic was written against. Where the two answers differ, it is that half moving.

## The keyword scale

Postgres has no BM25. `ts_rank` is bounded below 1 and `bm25()` is not bounded at all, so no single multiplier maps one onto the other for every query — but the figure is added to the combined score with a fixed coefficient and rendered into every mind's context, so being within a factor of two of the old magnitude matters. `hive_data.memory.KEYWORD_SCALE` is **264**; measured here the ratio runs **93 to 538**, median **264**.

| query | FTS5 `bm25` | raw `ts_rank` | ratio |
|---|---|---|---|
| `rotation threshold for a terminal pane` | 17.988 | 0.08304 | 217 |
| `which voice engine speaks a mind` | 16.806 | 0.06904 | 243 |
| `postgres migration one database` | 19.011 | 0.06459 | 294 |
| `surface claim who a bot answers` | 17.349 | 0.06024 | 288 |
| `left elbow tendinopathy reloading` | 19.815 | 0.03683 | 538 |
| `restart the comms container` | 11.635 | 0.08275 | 141 |
| `what did Daniel decide about Zeek output` | 15.001 | 0.05682 | 264 |
| `lunch with Jose` | 14.195 | 0.03800 | 374 |
| `rotating a session` | 8.806 | 0.09418 | 93 |

### `rotation threshold for a terminal pane`

* overlap in the top 5: 4/5

| rank | lucent (FTS5) | bucket | Postgres | bucket | cosine agrees |
|---|---|---|---|---|---|
| 1 | 7518 | recency | 7518 | recency | yes |
| 2 | 7537 | recency | 7537 | recency | yes |
| 3 | 4459 | recency | 4459 | recency | yes |
| 4 | 4680 | cosine | 4680 | cosine | yes |
| 5 | 4560 | bm25 | 3921 | bm25 |  |

### `which voice engine speaks a mind`

* overlap in the top 5: 4/5

| rank | lucent (FTS5) | bucket | Postgres | bucket | cosine agrees |
|---|---|---|---|---|---|
| 1 | 8164 | recency | 8164 | recency | yes |
| 2 | 8166 | recency | 8166 | recency | yes |
| 3 | 6998 | recency | 6998 | recency | yes |
| 4 | 1814 | cosine | 1814 | cosine | yes |
| 5 | 7026 | bm25 | 7003 | bm25 |  |

### `postgres migration one database`

* overlap in the top 5: 4/5

| rank | lucent (FTS5) | bucket | Postgres | bucket | cosine agrees |
|---|---|---|---|---|---|
| 1 | 8136 | recency | 8136 | recency | yes |
| 2 | 8321 | recency | 8321 | recency | yes |
| 3 | 8047 | recency | 8047 | recency | yes |
| 4 | 8011 | cosine | 8011 | cosine | yes |
| 5 | 8010 | bm25 | 575 | bm25 |  |

### `surface claim who a bot answers`

* overlap in the top 5: 5/5 — same order

| rank | lucent (FTS5) | bucket | Postgres | bucket | cosine agrees |
|---|---|---|---|---|---|
| 1 | 8164 | recency | 8164 | recency | yes |
| 2 | 8064 | recency | 8064 | recency | yes |
| 3 | 8166 | recency | 8166 | recency | yes |
| 4 | 5017 | cosine | 5017 | cosine | yes |
| 5 | 7951 | bm25 | 7951 | bm25 | yes |

### `left elbow tendinopathy reloading`

* overlap in the top 5: 4/5

| rank | lucent (FTS5) | bucket | Postgres | bucket | cosine agrees |
|---|---|---|---|---|---|
| 1 | 8065 | recency | 8065 | recency | yes |
| 2 | 8066 | recency | 8066 | recency | yes |
| 3 | 7890 | recency | 7890 | recency | yes |
| 4 | 6677 | cosine | 6677 | cosine | yes |
| 5 | 6272 | bm25 | 4132 | bm25 |  |

### `restart the comms container`

* overlap in the top 5: 4/5

| rank | lucent (FTS5) | bucket | Postgres | bucket | cosine agrees |
|---|---|---|---|---|---|
| 1 | 8255 | recency | 8255 | recency | yes |
| 2 | 7975 | recency | 7975 | recency | yes |
| 3 | 7953 | recency | 7953 | recency | yes |
| 4 | 6153 | cosine | 6153 | cosine | yes |
| 5 | 3885 | bm25 | 7126 | bm25 |  |

### `what did Daniel decide about Zeek output`

* overlap in the top 5: 4/5

| rank | lucent (FTS5) | bucket | Postgres | bucket | cosine agrees |
|---|---|---|---|---|---|
| 1 | 8183 | recency | 8183 | recency | yes |
| 2 | 8050 | recency | 8050 | recency | yes |
| 3 | 8013 | recency | 8013 | recency | yes |
| 4 | 2784 | cosine | 2784 | cosine | yes |
| 5 | 8042 | bm25 | 734 | bm25 |  |

### `the and of is are to be`

* overlap in the top 5: 5/5 — same order

| rank | lucent (FTS5) | bucket | Postgres | bucket | cosine agrees |
|---|---|---|---|---|---|
| 1 | 8064 | recency | 8064 | recency | yes |
| 2 | 8177 | recency | 8177 | recency | yes |
| 3 | 7747 | recency | 7747 | recency | yes |
| 4 | 2146 | cosine | 2146 | cosine | yes |
| 5 | 7180 | fill | 7180 | fill | yes |

### `lunch with Jose`

* overlap in the top 5: 5/5 — same order

| rank | lucent (FTS5) | bucket | Postgres | bucket | cosine agrees |
|---|---|---|---|---|---|
| 1 | 8064 | recency | 8064 | recency | yes |
| 2 | 7747 | recency | 7747 | recency | yes |
| 3 | 7180 | recency | 7180 | recency | yes |
| 4 | 3047 | cosine | 3047 | cosine | yes |
| 5 | 3048 | bm25 | 3048 | bm25 | yes |

### `rotating a session`

* overlap in the top 5: 4/5

| rank | lucent (FTS5) | bucket | Postgres | bucket | cosine agrees |
|---|---|---|---|---|---|
| 1 | 8099 | recency | 8099 | recency | yes |
| 2 | 3954 | recency | 3954 | recency | yes |
| 3 | 1914 | recency | 1914 | recency | yes |
| 4 | 4557 | cosine | 4557 | cosine | yes |
| 5 | 1841 | bm25 | 528 | bm25 |  |
