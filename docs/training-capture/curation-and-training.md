# Curation, export, and fine-tuning

`data-contract.md` describes the raw store. This document describes what
happens to it afterwards: how rows are judged, how a judged corpus becomes a
JSONL dataset, and how that dataset becomes a LoRA adapter served by Ollama.

The capture layer is lossless on purpose. Everything selective lives here,
downstream, where it can be re-run with a different policy without ever
having lost the rows a previous policy discarded.

## The pipeline

```
training_turns.db  ──curate──▶  quality_flag / exclusion_reason / curation_meta
                                          │
                                       export
                                          ▼
                        train.jsonl + eval.jsonl  ──train──▶  LoRA adapter
                                                                   │
                                                              Ollama Modelfile
```

Each stage is a separate module under `core/`, and each writes a row to the
run ledger in `data/training_runs.db` so the console can show what ran, what
it produced, and what is running now.

| stage | module | writes |
|---|---|---|
| curate | `core/training_curation.py` | verdict columns on `training_turns` |
| redact | `core/training_redaction.py` | nothing — applied in memory at export |
| export | `core/training_export.py` | `train.jsonl`, `eval.jsonl` |
| train | `core/training_finetune.py` | `finetune_spec.json`, adapter dir |
| ledger | `core/training_runs.py` | `training_runs` table |

The CLI at `tools/stateless/training_pipeline/training_pipeline.py` drives
all of them, one JSON object per invocation, stdlib only — so the console
can call it across a container boundary without sharing an environment.

## Curation rules

Rules run in order; the first match excludes the row. Every row keeps its
verdict in `quality_flag` (`keep` / `excluded` / `pending`), the rule name in
`exclusion_reason`, and machine-readable detail in `curation_meta`. Nothing
is ever deleted.

| reason | what it catches | why it would hurt the model |
|---|---|---|
| `malformed_blocks` | `assistant_blocks` is not a JSON array | teaches broken tool-call syntax |
| `no_assistant_response` | array parses but is empty | no behaviour to learn |
| `interrupted` | abort markers, or a trailing `tool_use` with no result | teaches stopping mid-task |
| `harness_error` | API errors, overload, context exhaustion | teaches the failure, not the work |
| `too_short` | under the token floor with no tool call | noise |
| `too_long` | over the sequence budget | cannot fit the training window |
| `tool_error_dominant` | most tool results failed and never recovered | teaches flailing |
| `contains_secret` | credential detected (opt-in exclusion) | see *Secrets* below |
| `near_duplicate` | beyond the per-cluster budget | see *Near-duplicate collapse* below |

A turn where one tool call fails and the next succeeds is **kept**.
Recovering from a failed command is a behaviour worth learning; only turns
that never recover are dropped.

## Near-duplicate collapse

This is the dominant defect in a corpus captured from a running hive.
Scheduled skills fire on a timer, so their turns are near-identical, and a
raw token count wildly overstates how much *distinct* behaviour is present.
On the corpus as of 2026-07-30, one scheduled skill accounted for a single
cluster of 316 turns, and near-duplicates were 60% of all rows.

A cluster is keyed on two things together:

- the **normalized prompt** — lowercased, with timestamps, UUIDs and bare
  numbers blanked, truncated to `cluster_prefix_chars`. This is what makes
  the 7am briefing and the 8am briefing land in the same cluster.
- the **tool signature** — the ordered names of the turn's `tool_use`
  blocks. Two alert runs that both read a log and posted a message share
  this; a run that instead escalated does not, and keeps its own cluster.

Within a cluster, `keep_per_cluster` rows survive, ranked by reasoning
first, then tool breadth, then length, then recency. Reasoning ranks first
because a turn that shows its work is worth several that do not.

`keep_per_cluster` is the knob that matters most. One is too few — a
scheduled skill genuinely is part of the job. Unbounded is how hundreds of
identical alert runs drown out every hand-written debugging session.

## Secrets

The corpus is captured from real tool output, so it contains real
credentials: `cat .env` results, bearer headers, tokens in git remote URLs.

**The corpus stays raw.** Capture never sanitizes, and curation never
rewrites content. What credentials do on the way *out* is an export
decision, controlled by `ExportOptions.secrets` — `--secrets` on the CLI, a
dropdown in the console — with three values.

`randomize` **(default)**. Each
credential becomes a *different* string of the same length and character
class, with the vendor prefix preserved: `ghp_` stays `ghp_`, and the
thirty-six characters after it become something else. The model learns the
transferable fact — what a GitHub token looks like — and never sees a real
one. The mapping is a salted HMAC of the secret, so one credential appearing
in two hundred turns becomes the *same* surrogate in all of them (the model
sees a consistent world, not noise) and a re-export with the same salt
reproduces the dataset byte for byte. Private keys are the exception: a key
block has no shape worth preserving, so it is dropped wholesale.

This is the default because it is the only policy that survives both failure
modes at once, and it is what the field settled on independently: clinical
de-identification calls it *hiding in plain sight*, adopted after
sentinel-token redaction was found to teach models to emit the sentinel.

`keep` **(deliberate only)**. Real values. This dataset trains a model that
runs on this hardware, so it is not absurd — but memorization scales with
duplication and with epochs, and a LoRA doing several passes over a few
thousand turns is exactly the regime where extraction works. The corpus
itself is unaffected either way; this only governs what lands in the JSONL.

`redact` **(bluntest)**. Placeholders. This teaches the model that
`<REDACTED_SECRET>` is what belongs in the credential slot, so it emits one
at the moment it needs a live token — a silent failure, and usually worse
than the leak it was meant to prevent. Use it only when the dataset must
provably contain no credential-shaped strings at all.

`core/training_redaction.py` never touches the raw store under any policy.
It runs in two modes:

- **detection**, during curation, which counts contaminated rows and records
  which rule fired in `curation_meta` — so the console can show how much of
  the corpus is affected without displaying any secret.
- **replacement**, during export, under `randomize` or `redact`.

Contaminated rows are kept during curation, because the underlying turn is
usually a good demonstration of tool use. `--exclude-secret-rows` drops them
instead.

Detection is deliberately over-eager, since a missed credential is permanent
once it is in the weights and an over-eager match costs only a surrogate
where none was needed. Auth *schemes*
(`Bearer`, `Basic`), type annotations (`password: str`), and env indirection
(`TOKEN=$OTHER`) are allowlisted so ordinary code survives intact.

**Never paste a value out of this corpus into a tracked file.** Test
fixtures need invented credentials, not real ones — the corpus is
gitignored, but a test file is not.

## Export

Export translates the stored block array into an alternating message list.
A run of consecutive `tool_use` blocks becomes **one** assistant message
carrying several `tool_calls`, because that is how the harness issues
parallel calls — flattening them would teach the model to serialize work it
is allowed to batch.

Two modes, per the data contract:

- `stripped` drops every `thinking` block and leaves no placeholder. The
  correct input for a non-reasoning base model.
- `reasoning` emits each thought in front of the action group it produced.

There is no mode that emits an empty thought.

The train/eval split is **by session, never by turn**. Turns from one
session share a system prompt, a working directory, and often the literal
file being edited; splitting by turn would put turn three in train and turn
four in eval, and the eval score would measure memorization. The split is a
hash of `session_id`, so it is stable across re-exports.

Long tool results are truncated with an explicit marker rather than
dropped. A 150,000-character log dump is one example that costs as much as
three hundred useful ones, and eliding it teaches what the harness actually
does with long output.

## Fine-tuning

Training never runs in-process. The hive has one RTX A6000 shared with
inference, so a job is *described* by `FineTuneSpec` and handed to a
container with the GPU attached. That keeps a long run from taking the
console down with it and keeps torch out of every other service's image.

LoRA rather than a full fine-tune: a 30B model in bf16 does not fit in 48 GB
alongside anything else, harness-driving is a format-and-policy adaptation
that low-rank adapters handle well, and a full fine-tune is far likelier to
erode the base model's general coding ability.

`plan_run` is pure and always available. It reports whether the job can
start and, when it cannot, exactly why — a busy GPU is an expected outcome
while inference is serving, not an error. `launch` returns a structured
refusal rather than raising.

The adapter is served through Ollama with an `ADAPTER` directive over a
shared base tag, so a new fine-tune costs adapter-sized disk rather than
another full model.

## Running it

```bash
venv/bin/python tools/stateless/training_pipeline/training_pipeline.py status
venv/bin/python tools/stateless/training_pipeline/training_pipeline.py curate --keep-per-cluster 3
venv/bin/python tools/stateless/training_pipeline/training_pipeline.py export --mode reasoning --name v1
venv/bin/python tools/stateless/training_pipeline/training_pipeline.py train --train-file data/training_sets/v1/train.jsonl --dry-run
```

`curate --reset` returns every row to `pending` so a changed policy can be
applied to the whole corpus. Curation is idempotent: running it twice
produces the same verdicts.
