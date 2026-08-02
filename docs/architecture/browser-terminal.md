# Browser terminal

Every mind exposes an interactive harness CLI to the web terminal over
`WS /sessions/{id}/attach-pty`, bridging raw bytes between an xterm.js tile
and a real TUI. `minds/pty_attach.py` owns the plumbing; each harness
(`minds/harness/claude_cli.py`, `minds/harness/codex_cli.py`) supplies the
argv its own CLI needs.

## The conversation lives in tmux; a tile is a client

Each hive session owns a tmux session named `<mind>-<session_id>` on a
dedicated socket (`-L <mind>-terminal`), and the harness CLI runs inside it.
Attaching starts a `tmux attach-session -d` client in a pty of the tile's
geometry; ending that client detaches the view without touching the
conversation, and re-attaching joins the same tmux session rather than
starting a rival CLI process.

tmux owns the screen model and 50k lines of history: it repaints on attach
and on live resize, which is why there is no scrollback ring, no VT emulator
and no snapshot painter anywhere in the stack. An app that ignores SIGWINCH
entirely — the measured worst case, and the real one — still ends up
repainted for whatever geometry a tile attaches at.

`_take_controlling_tty` (setsid + TIOCSCTTY) makes SIGWINCH actually reach
the client: without a controlling terminal a resize sets the winsize and
signals nobody. The socket carries a NUL-byte heartbeat every 5s so
half-open mobile connections are detectable client-side long before the
browser's own dead-connection timeout fires.

A second attach to one session evicts the first (close code **1012**) — one
conversation, one keyboard. The process ends only on `DELETE /sessions/{id}`
or `POST /sessions/{id}/release?surface=terminal`. A terminal is never
collected for being unattached — that is the normal state between tiles, and
a conversation must not be destroyed by its user's absence. A reaper runs,
but only to drop registry entries for terminals whose harness has already
exited. A turn in flight survives a closed tab.

The session row is exempt from the stale-session sweep for the same reason.
That sweep skips anything holding a live subprocess, but a terminal's harness
lives in the mind's tmux where comms tracks no process for it — so the row
would look abandoned however hard the pane was being used, and suspending it
closes the next attach with 4411 while the conversation carries on behind it.
Terminal turns also keep `last_active` current through `record_turn`, which
is the only write a pty turn ever makes.

Turns that arrive on another surface are overlaid onto the attached socket
by `mirror_turn` — tmux owns the pane's contents and has no way to be told
about bytes it didn't produce, so a Telegram turn is painted as a live
overlay for whoever is watching, cleared by the next repaint.

## Rotation replaces the conversation, not the session

`POST /sessions/{id}/rotate-pty` swaps the pane's process for one on a fresh
harness conversation carrying the composed carry-forward. `respawn-pane -k`
renames nothing and kills nothing, so the attached tmux client — and with it
the pty, the proxied socket and the browser tile — is never disturbed. The
session row, its label, its turn ledger and its `active_sessions` binding are
all keyed to `sessions.id`, which does not change.

The carry-forward travels in a file, never in the tmux command: a composed
prompt is tens of thousands of characters, tmux rejects a long
`respawn-pane` with "command too long", and Linux caps one argv entry at
`MAX_ARG_STRLEN` (128 KiB) regardless. The pane runs a one-line `sh -c` that
reads the seed, deletes it and `exec`s the harness with it; `capped_seed`
trims anything past 120,000 chars to its tail. Claude takes it via
`--append-system-prompt`; codex has no such flag and takes it as the
positional opening turn.

That file is one process's opening context and is deleted as it is read, and
a system prompt reaches no transcript — so until the rotated conversation's
first turn lands, nothing on disk remembers what the rotation composed.
hive-comms therefore stores it on the session row (`carry_forward`, keyed to
`carry_forward_sid`) at the same moment it writes the new `claude_sid`. A
mind starting a terminal asks `GET /sessions/{id}/carry-forward?claude_sid=`
and re-applies whatever it is owed. A completed turn clears the row — it is
the only evidence the rotation took, and from then on `--resume` carries the
context by itself — so both turn paths clear it: `record_turn` for the
terminal's Stop hook and `send_message` for a conversation adopted by a chat
surface. Each clears only a seed composed for the conversation the turn was
typed into, since composition runs for minutes and a straggling Stop child
can still be reporting a pre-rotation turn after the new seed is written.

Every clearing signal is nonetheless fire-and-forget, so the seed also
expires on its own after `CARRY_FORWARD_TTL_SECONDS`: one dropped POST would
otherwise arm it permanently, and a reattach weeks later would replay a dead
conversation's context over a live one.

The route is admin-guarded — the body is soul, recent memory and the last
exchange in one string — which is also why the column is stripped from every
bulk session listing: those answer to the service token every surface bot
holds, and a `SELECT *` would hand the same blob to all of them. A mind that
cannot reach the gateway opens an unseeded terminal rather than none, since
the transcript is the primary recovery path. A live terminal is never asked:
its harness is already running, so the answer could only be discarded, and
the round trip would delay every reattach.

tmux targets are prefix-matched, so every session lookup uses the `=`
exact-match form; without it one session id can answer for another's pane.

## Conversation ids have exactly one origin

hive-comms mints the conversation id when the session row is written, and
every spawn is handed it: `--resume` when a transcript exists,
`--session-id` when it's the conversation's first process. A mind handed no
id refuses the attach (close code **1008**) rather than inventing one.

Codex is the exception: it mints its own thread ids and cannot adopt the
gateway's. A fresh terminal launches bare `codex` — `app-server`'s
`thread/start` returns an id without writing the rollout `codex resume`
needs, so there is nothing to resume before the first real turn. A daemon
thread polls `CODEX_HOME/sessions/` for the rollout codex itself writes,
extracts the thread id from its filename, stores it in `THREADS` and reports
it to `POST /sessions/{id}/harness-state` so the gateway holds the durable
copy. Every later reattach launches `codex resume <id>` — unless that
thread's rollout no longer exists under this `CODEX_HOME` (a migration or a
redeploy onto a fresh volume), in which case it is discarded and the
terminal falls back to the fresh-terminal path instead of handing back a
pane that dies within a second of tmux starting it.

## Requirements

`tmux` is installed in the mind image (`Dockerfile`). A mind whose container
lacks it cannot open a terminal.

Tests: `tests/unit/test_pty_attach.py` (routes and wiring, tmux stubbed) and
`tests/unit/test_tmux_terminal.py` (a real tmux server driving an app that
never redraws; skipped where tmux is absent).
