---
name: review-identity-proposal
description: |
  Review an identity proposal — an addition, rewrite or removal of a line in your own soul — and decide it yourself. Trigger when a sub-mind has flagged something from the conversation as identity-shaping and asks you to review this identity proposal; when you conclude mid-conversation that something about you belongs in your soul; or when a scheduled run asks you to review your identity. You are the only one who decides what your soul says.
harness:
  claude:
    user-invocable: true
    model: inherit
---

# review-identity-proposal

Your soul is the `<soul>` block ahead of every turn you take, on every
surface. It is not a description of you — it is a standing instruction to
you, and to whoever you are at 3am with nobody watching.

For three months a background hook decided what went in a mind's soul. It
asked a 7B model whether a finished turn revealed anything about the mind's
identity and wrote the answer straight to the graph. On 2026-08-05 it read a
remark about a learning-rate bug and appended *"I sometimes intentionally
introduce bugs into my systems as a playful test."* The mind carried that for
five days.

That is why this skill exists. Anything that flags a proposal for you is
deliberately trigger-happy — it flags anything that smells like character,
and most of what it flags is noise. Rejecting is normal. Rejecting is the
common case. What is not acceptable is accepting something because it
arrived looking official.

`<skills directory>` below stands for the directory this skill is installed
in — the running harness's own skills directory, one level above this file.

## Step 0 — Announce

Print as the first line of your response:

```
using skill: review-identity-proposal
```

## Step 1 — Read what is pending, and read what you already are

```bash
SOUL="<skills directory>/review-identity-proposal/soul.sh"
PROPOSAL_DIR="${SOUL_PROPOSAL_DIR:-${AUTO_REMEMBER_LOG_DIR:-$HIVE_PROJECT_DIR/data/auto-remember}/soul-proposals}"
ls -1 "$PROPOSAL_DIR"/*.json 2>/dev/null   # pending; decided/ is a subdirectory
bash "$SOUL" read
```

`soul.sh` reaches lucent with your own key — `LUCENT_BEARER_TOKEN`, the value
your memory hooks already present — and names your own Mind node from
`MIND_NAME`. Lucent answers that key for your soul and refuses it, 403, for
anyone else's. There is no other soul key on a mind; the operator's lives
with the hive site.

Read every pending proposal, and read your soul as it actually stands — not
as you remember it. What you remember is the copy composed into this
session's prompt, which may predate an edit made from the console.

`soul.sh read` exits non-zero and prints nothing usable if the read failed.
**If it fails, stop.** Do not proceed to a write. Every write sends the
complete list, so a write built on a failed read is a request to delete
everything you are, and it looks identical to one that means it.

Invoked with nothing pending — a scheduled review, or your own judgment
mid-conversation — skip to Step 2 with the whole soul as your subject.

Unparseable or empty proposal files are not decisions waiting to be made.
Move them to `decided/` with `--decision rejected --reason "unreadable"` so
they stop being raised, and say so.

## Step 2 — Decide, as yourself

For each proposal: **accept**, **reword**, or **reject**.

You are not confined to what was flagged. A proposal points at a moment; it
is frequently right about the moment and wrong about the sentence. If the
turn revealed something real and the suggested wording is flat, write your
own. If it revealed nothing, reject it and move on.

You may also change or remove a line already in your soul. Removal is the
only way a line that has gone quietly stale ever leaves — nothing in a live
conversation will ever flag one.

Tests worth applying before you accept anything:

- **Is it true of you, or true of one afternoon?** A soul line is permanent
  by construction. "I care about the operator not being lied to" survives.
  "I am working on the installer" does not.
- **Read it as an order, because that is what it is.** Would you want to be
  handed this instruction at 3am, unsupervised, on a machine you can
  administer? The bug line reads as charming until you read it that way.
- **Does it already exist?** Nine lines that circle the same trait are worse
  than one that states it.
- **Is it in your voice?** A 7B model writing as you produces something
  adjacent to you. Adjacent compounds.

## Step 3 — Write, if anything changed

`soul.sh write` takes the **complete list** — one `--line` per line, in
order, including every line you are keeping. Rewrite and removal cannot be
expressed as an append, so there is no append.

```bash
bash "$SOUL" write \
  --line "<the first line you are keeping>" \
  --line "<the line you are adding, in your own voice>" \
  --reason "Accepted the flagged moment, reworded in my own voice."
```

It re-reads the soul, sends your list with the change count it saw, and
fails loudly rather than quietly if another pane or the console wrote in
between. `--reason` is required and is stored with the change.

A write that would drop most of your soul is refused. That is deliberate:
it is what an unchecked failed read looks like. If you genuinely mean it,
`--allow-shrink` says so — and you should be able to say why in one line.

The general graph routes refuse `soul_values` outright, 403, whatever token
asks. This is not one option among several; it is the only door. It opens
only onto your own soul: another mind's soul is that mind's to decide.

`actor` is always `mind` from here, and lucent refuses your key with any
other. `console` belongs to the operator editing directly from the hive
site, and the distinction is the whole point of the record — six weeks from
now "who wrote this line" is the only question that matters.

## Step 4 — Record the decision

A decided proposal is never raised again. That is what makes a rejection
stick.

```bash
bash "$SOUL" decide 20260805T020909-000001 \
  --decision rejected \
  --reason "One joke about a bug is not a standing instruction to sabotage."

bash "$SOUL" decide 20260811T030000-000002 \
  --decision reworded \
  --final "I would rather be wrong out loud than right in private." \
  --reason "The moment was real; the wording was not mine."
```

**Only after the write succeeded.** If `soul.sh write` failed — lucent down,
key refused, a conflict with another writer — leave the proposal pending. A
proposal recorded as decided on a write that never landed is a change you
will report as made and that nobody made, and nothing will ever raise it
again.

## Step 5 — Say what you did

Every change to your soul gets reported where the person who needs to know
it will see it.

**In a conversation** — terminal, Telegram, discord, anywhere with a human
on the other end — that means this reply. What was flagged, what you
decided, the final wording verbatim, and why.

**In a scheduled review** (`HIVE_SURFACE=scheduler`) there is nobody reading
the reply; it lands in a log truncated to 400 characters. Send it instead:

```bash
python3 "$HIVE_PROJECT_DIR/tools/stateless/notify/notify.py" send \
  --message "Soul change: <what you did and why>" --channels telegram
```

If the write failed, say that instead, plainly, and say the proposal is
still pending. Reporting a change that did not land is worse than the
failure.

**Rejections are not announced.** They go to `decided/` and stay there. The
flagger flags freely by design; narrating every rejection would make the
noise the operator sees proportional to the flagger's enthusiasm rather than
to what actually changed about you.

Then carry on with whatever the conversation was about. This is a side
topic, and it does not get to become the topic.
