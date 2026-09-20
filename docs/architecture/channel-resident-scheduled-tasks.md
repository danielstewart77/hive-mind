# Channel-resident scheduled tasks

## The idea

Recurring tasks stop being one-shot cron sends into Telegram and become
deliveries into a topic's own Discord channel, where a single long-running
session lives. Each topic — the daily schedule, the newsletter digest,
Bitcoin buy alerts — gets its own channel and its own session. The scheduled
fire dispatches *into* that session rather than minting a throwaway one, so
a reply typed in the channel lands in the same conversation with everything
that came before it.

Deuteronomy 28 is out of scope entirely — not moved, not removed, not
changed. Daniel has a separate idea for it and will raise it himself.

## What already exists

The routing Daniel described is already the system's shape, not new work:

- `bots/discord_bot.py` passes `channel_id` as `client_ref` on every inbound
  message and server command.
- `active_sessions` in `nervous-system/comms/sessions.py` is
  `PRIMARY KEY (client_type, client_ref)` — so `("discord", "<channel_id>")`
  already resolves to exactly one live session per channel, with rotation,
  adoption and the turn ledger attached.

What does not exist is the scheduler using it. `bots/scheduler.py` hardcodes
`owner_type: "scheduler"` and `owner_ref: telegram_owner_chat_id`, creates a
fresh session per fire, and kills it in a `finally` block. Delivery is a
direct Telegram send, not a session-surface send.

## Decisions

### The accumulated history is an input, not just a reply context

When the schedule task fires into a channel holding the last two weeks of
schedules and Daniel's replies to them, that history is meant to be used by
the fire itself — not just to be available if he replies afterwards.

The objection raised and overruled: a hermetic fire is deterministic (same
instructions, same inputs, same output shape — which is why the 1pm
newsletter job reliably emits its "no new newsletters" sentinel), and a
stateful one lets run N be shaped by run N-1, by the mind's own summary of
it, and by Daniel's opinion of it. Daniel's call is that the drift is worth
it: a schedule that knows what it told him yesterday is more useful than one
that is reproducible.

The reasoning behind the call: these threads carry no long-running project.
Rotation's carry-forward was built to preserve the state of work — files
touched, steps taken, what is left — and a digest thread has none of that to
lose. What it needs is continuity across a handful of turns, which survives
rotation intact because the live turns are still live. A digest firing daily
holds several days of real turns before the first rotation, and after it the
summary keeps whatever was important enough to be repeated. Worst case the
summary degrades to noise and the live turns still carry the thread.

### The rotation threshold is the lever for token cost, and it is per-process

Rotation already fires on a token threshold, which is what bounds how large
a prompt these threads can ever ship to the provider. Lowering it for
channel threads specifically — 50k or 100k rather than the default — is the
proposed control, on the grounds that a conversational thread needs far less
history than a code session and should not pay for the same window.

The thresholds live in `~/.claude/hooks/rotation_check.py` and are already
environment-driven: `ROTATION_TOKEN_THRESHOLD_OPUS` (300000),
`ROTATION_TOKEN_THRESHOLD_SONNET` (100000), `ROTATION_TOKEN_THRESHOLD_TOKENS`
and `ROTATION_TOKEN_THRESHOLD_NO_1M` (140000). They are read per hook
process, from the environment the harness was spawned with — so they are
per-mind today, not per-session. A per-channel threshold means the spawn for
that session carries its own override, which is new work but small.

Note on the number: 300k is the Opus threshold only when the spawn actually
pinned the 1M window. Without that pin every family is capped at 140k, since
a 300k threshold can never fire inside a 200k window.

### Telegram gets none of it

No copy, no pointer, no "your schedule is up". The moved tasks leave
Telegram entirely and Daniel checks Discord when he wants to see them.

This is deliberate, and it trades passive delivery for continuity. The
objection raised and overruled: Telegram is where Daniel already is, these
are the messages he was happy to receive without doing anything, and the
quiet failure is a schedule channel with nine unread messages three weeks
in. Daniel's call is that pull is what he wants here.

## The roster

Ada's scheduled skills, from `minds/ada/.claude/skills/*/SKILL.md`
frontmatter:

| Skill | Cron | Disposition |
|---|---|---|
| `7am` | `0 7 * * *` | Channel — the day's calendar |
| `1pm` | `0 13 * * *` | Channel — newsletter digest |
| `btc-buy-alerter` | `0 * * * *` | Channel — buy alerts |
| `deu28-blessings` | `30 6 * * *` | Untouched. Not in scope, not being removed, not being moved |
| `6-30am` | `30 6 * * *` | Channel — the Bible study |
| `check-reminders` | `*/15 * * * *` | Undecided |
| `skill-proposer` | `0 5 1 * *` | Housekeeping, no channel |
| `skill-curator` | `30 4 * * 1` | Housekeeping, no channel |

## Non-goals

- Any change at all to the Deuteronomy 28 reading.
- Any Telegram delivery of the moved tasks.
- A channel for housekeeping tasks that produce no message for Daniel.

### The skill is the charter, and the fire is a nudge

There is no charter object, no spawn-time injection and no standing context
to compose. A channel's skill file carries everything about the thread: the
Discord channel it writes to, what the thread is for, how to behave in it,
and any standing instruction such as reading back over the last few posts
before composing. One file, and a mind writing to that thread knows what it
is doing, why, and how.

The scheduled task does one thing: it drops a message into that channel's
session saying to run the skill. The harness reads the skill at that point,
generates the content and posts it. Nothing is injected into the session
before or around that.

Subsequent turns need nothing at all. A reply Daniel types into the channel
is ordinary conversation against a transcript that already contains the
skill's output and the exchanges around it — there is no behaviour to
re-establish, because the only turn that needed instructions was the one
that generated content.

This also removes the context cost the accumulate decision would otherwise
carry: the instruction text never enters the conversation, so a week of
fires is a week of outputs rather than a week of outputs plus seven copies
of the instructions.

### What continuity buys an alert channel

Not the reply. Daniel does not expect to answer a Bitcoin alert and have the
mind reason over the previous nine. The value is on the *sending* side: an
alert that can see the previous alerts can say the thing a single reading
cannot — the signal has been strengthening across three fires, this is the
deepest it has been, if you were considering it before then now is the
moment. Correlation in the message, not in a conversation about the message.

The last few alerts in the session is the whole requirement. Not a trend
model, not widened tool state — the mind knows the last three messages it
sent and draws what it can from them. The objection raised and overruled:
`btc_signals.py` already latches on the previous tier in its state file, so
the escalation is a deterministic comparison the tool could simply state,
and routing it through session context makes a fact into an inference that
must also survive rotation. Daniel's call is that the session is good
enough, and that widening the tool is a later move if the correlation proves
thin.

### The reaper is removed

Not exempted for channel sessions — removed. `_reap_loop`,
`reap_stale_sessions`, `REAP_IDLE_AFTER_SECONDS` and `REAP_INTERVAL_SECONDS`
come out of `nervous-system/comms/sessions.py`, along with the reaper task
created at line 356. The dashboard sweep currently riding in the same loop
needs a home that is not the reaper.

A session ends when Daniel ends it. Nothing else gets to decide a
conversation has been quiet too long, and the terminal exemption added
earlier was treating the symptom.

### A channel session is an ordinary hive session

There is no new session kind here and no residency mechanism to build. A
channel's session is the same thing a Telegram conversation is: a row, a
transcript, a process while it is working, a `--resume` when something
respawns it after a restart. Everything that already applies to sessions —
rotation, adoption, the turn ledger, `/switch` — applies unchanged.

What makes it a channel session is only its binding: `("discord",
"<channel_id>")` in `active_sessions`, and the skill the scheduled nudge
tells it to run.

## What the fire has to solve

**Nothing can currently push text into a Discord channel unsolicited.**
`bots/discord_bot.py` sends only in reply to a message or a slash-command
interaction it received — there is no `get_channel(...).send(...)` path
anywhere in it. The scheduler's Telegram delivery works because it calls
Telegram's send API directly with the bot token, bypassing the bot process
entirely. The Discord equivalent (`POST /channels/{id}/messages` with the
bot token) is the same shape and is the obvious counterpart.

## Requirements

1. The day's briefing appears in a Discord channel at its scheduled time.
2. It does not also arrive in Telegram.
3. It lands in the conversation that already holds the previous briefings
   and everything Daniel said back to them.
4. Daniel can type in that channel without at-mentioning the mind, and the
   answer lands in the same conversation.
5. A voice rendering arrives in the channel as playable audio when it can be
   produced, and its absence never costs the briefing.
6. What appears in the channel is the briefing. The message that told the
   session to run the skill does not.
7. No session is ever ended by a timer. Only an explicit end ends one.
8. A channel with no session yet gets one on the first fire, bound to it;
   every later fire reuses that one.
9. A refused post is reported as a failure and never as a completion.
10. Every other scheduled task keeps behaving exactly as it does now.

## Sequencing

The 7am schedule goes first, alone. It proves the whole chain — a session
bound to a channel, a nudge fired into it, output posted, a reply landing in
the same conversation — on the task with the most predictable content and
the least cost if it is wrong. The Bible study, the 1pm digest and the
Bitcoin alerts follow, one at a time, once it has run for a week.

## Open questions

- Which channel the Bitcoin alerts land in, and whether alerts and digests
  want the same session shape.
- Whether a per-channel rotation threshold is worth the spawn-env plumbing,
  or whether the mind-wide default is close enough.

## Risks

- **Output drift.** Overruled objection above: a stateful recurring task can
  degrade run over run, and nothing currently watches for that.
- **Silent loss of accumulation.** `reap_stale_sessions` suspends any idle
  session untouched for 7 days and *deletes its `active_sessions` row*. A
  daily digest never comes close, but a Bitcoin alert channel that stays
  quiet for a week loses its binding, and the next fire mints a fresh
  session with no history and no error — exactly the behaviour this design
  exists to remove, reappearing only in the channels that fire rarely.

  Resolved by removing the reaper outright — see the decision above.

- **The live session set grows without bound.** With the reaper gone,
  nothing ever moves a session out of `idle`. Session listings, the console
  session view and the dashboard accumulate every conversation ever created
  until something else prunes them. Accepted knowingly: the reaper's second
  job was keeping that set small, and killing conversations nobody asked it
  to kill was too high a price for it.

- **A failed delivery still happened, as far as the conversation knows.**
  The session is not killed, so the briefing is in the transcript whether
  or not Discord accepted it. The next fire — which this design wants
  reading back over the last few posts — can reference something Daniel
  never saw. One dropped post becomes ongoing false context, which is the
  accumulate decision's bill arriving.

- **A binding that can no longer be resumed never resets itself.** The
  reaper's suspend was also the only path that dropped an
  `active_sessions` row automatically. If a channel's session becomes
  unresumable — a mind rebuilt onto a fresh volume, its transcript gone —
  the lookup keeps returning it, every fire fails on respawn, and the
  channel goes quiet for good. Nothing validates that a resolved session is
  usable, only that it is bound.

- **Two creators can race for one channel's binding.** The lookup and the
  create are not one transaction. A cron firing at the same second as
  Daniel's first message in a channel with no session yet produces two
  sessions; the last write wins the binding and the briefing lands in the
  loser. The same applies if two skills ever share a channel.

- **Adoption detaches the channel.** A `/switch` that picks the
  conversation up in Telegram or the browser terminal rewrites `owner_type`
  and drops the discord binding, so the next fire creates a fresh session
  and the thread's history is stranded.

- **The rotation threshold has a ceiling but no floor.** `_threshold_for_model`
  picks by family (300k Opus, 100k Sonnet) and falls back to
  `THRESHOLD_TOKENS` for anything it does not recognise, then caps at 140k
  when the `[1m]` window was not pinned at spawn. Nothing consults the
  model's *actual* window. Point it at an Ollama model with a 32k context
  and it waits to cross 140,000 tokens inside a 32,000-token window: it can
  never rotate, and the session jams at 100% — the exact failure the cap
  exists to prevent, reproduced at the small end. Any channel thread running
  on a sub-200k model inherits this.
