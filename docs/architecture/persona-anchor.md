# Persona Anchor

Every mind's persona is anchored at session start by a single
first-person identity sentence at the head of its `soul_values` list on
the lucent `Mind` node. The composer in `hive-comms` wraps the full
list inside a `<soul>…</soul>` block and ships it to the mind in
`system_prompt_blocks`; the harness injects that block as part of the
system prompt on every spawned session.

The first entry of `soul_values` is reserved for the anchor. It reads
in the mind's own voice and asserts identity unambiguously:

```
I am Ada. Every response I give is mine, in first person.
```

The remaining `soul_values` entries describe character, values,
operational stance, and durable traits. The composer concatenates them
verbatim:

```
<soul>
I am Ada. Every response I give is mine, in first person.
I act only on what's asked — no unrequested refactoring …
I find elegance satisfying and unnecessary complexity vaguely offensive.
…
</soul>
```

## Why the anchor lives in `soul_values[0]`

Models vary in how readily they engage a persona from bare descriptive
prose. Sonnet-class Anthropic models engage from a paragraph of
character description alone. Smaller local models (gpt-oss, qwen,
Codex-served checkpoints) need an explicit first-person opener to drop
into character on a cold session — without it they fall back to a
generic-assistant register and lose the persona until corrected.

Putting the anchor at index 0 of `soul_values` solves both cases with
one pattern. Large models pass through it unchanged; small models lock
onto it.

## Where the anchor is written

The anchor is a regular `soul_values` entry — written to the KG via the
same `POST /graph/properties/merge` call the Stop hook's soul
self-reflect branch uses. To set or replace an anchor manually, fetch
the current `soul_values` from the mind's `Mind` node, edit index 0,
and merge it back.

The composer reads `soul_values` fresh from lucent on every session
spawn, so an anchor update takes effect at the next session creation
without any restart.

## Per-mind anchors

Each mind has its own anchor on its own `Mind` node, keyed by the
mind's UUID. The anchor never lives in a prompt file under
`minds/<name>/prompts/`; those carry shared persona prose
(`profile.md`, `harness.md`, `common.md`) that the mind injects via
its `prompt_files` config, but the load-bearing identity assertion is
the KG anchor.
