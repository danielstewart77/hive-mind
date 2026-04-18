# Plan: Hive Mind Live Console

**Goal**: Add a `/console` page to spark_to_bloom that shows a live grid of all minds — one window per mind, streaming their active session in real time. New minds auto-appear when added to the system.

---

## Decisions (locked)

| # | Question | Decision |
|---|---|---|
| 1 | Auth | Full auth required — DB-backed user management. Protects both `/console` and `/graph` (endpoints + UI, not just hidden routes). |
| 2 | History on load | Live only — stream from the moment you open the page, no backfill. |
| 3 | Tool events | Show everything — tool calls, tool results, all event types. No filtering. |
| 4 | Which sessions per mind | All sessions active in the last 24 hours. Multiple Ada sessions = multiple windows. |
| 5 | Empty placeholders | None. Only show cards for sessions that actually exist. No ghost cards for idle minds. |
| 6 | Live card appearance | When a new session starts while the page is open, its card appears immediately — no refresh. |

---

## User Experience

- Navigate to `https://sparktobloom.com/console`
- Login wall (if not authenticated) → redirect to `/login`
- See a responsive CSS grid — one card per mind session active in last 24h
- Each card shows: mind name, session ID (short), start time, and a live feed streaming from the moment the page loads
- Ada may have 2+ cards if multiple sessions were active in the last 24h
- New minds auto-appear on next poll (60s interval)

---

## Architecture

```
Browser
  └── GET /console              → spark_to_bloom (auth check → render page)
  └── GET /api/minds            → spark_to_bloom → server:8420/broker/minds
  └── GET /api/console/sessions → spark_to_bloom → server:8420/sessions (filtered: last 24h)
  └── GET /api/console/{session_id}/stream  → spark_to_bloom (SSE proxy, auth check)
                                             → WS server:8420/sessions/{id}/stream
  └── POST /login               → spark_to_bloom (auth, sets session cookie)
  └── POST /logout              → spark_to_bloom (clears cookie)
```

spark_to_bloom acts as a **reverse proxy + auth layer**. The browser never talks to the hive_mind gateway directly.

**No hive_mind changes required.** All required endpoints already exist:
- `GET /broker/minds` — registered mind list
- `GET /sessions` — all sessions (filter by `last_active` timestamp client-side)
- `WS /sessions/{id}/stream` — live bidirectional stream

---

## Auth Design

### Database
Add SQLite DB to spark_to_bloom (`data/stb.db`) with a single `users` table:

```sql
CREATE TABLE users (
    id       INTEGER PRIMARY KEY,
    username TEXT    NOT NULL UNIQUE,
    password TEXT    NOT NULL,  -- bcrypt hash
    created_at INTEGER NOT NULL
);
```

Managed via a CLI bootstrap script (`scripts/create_user.py`). No self-registration — Daniel creates accounts manually.

### Session Cookies
- Login: POST `/login` with username/password → verify against DB → set signed `session` cookie (via `itsdangerous` or similar)
- Every protected route checks the cookie. Invalid/missing → 401/redirect to `/login`.
- Logout: POST `/logout` → clear cookie

### Protected Routes
Both UI and API endpoints are gated — not just the page render:

| Route | Protected |
|---|---|
| `GET /console` | Yes |
| `GET /graph` | Yes |
| `GET /api/minds` | Yes |
| `GET /api/console/sessions` | Yes |
| `GET /api/console/{id}/stream` | Yes |
| `GET /graph/data` | Yes |
| `GET /graph/public-data` | Yes |
| `GET /login` | No |
| `POST /login` | No |
| `GET /health` | No |
| `GET /` | No (public) |

FastAPI dependency injection: one `require_auth` dependency applied to all protected routes. No middleware that only hides the nav — the endpoints themselves reject unauthenticated requests.

---

## Session Window Logic

Query: `GET server:8420/sessions` → filter client-side to sessions where `last_active >= now - 86400s`.

Each qualifying session gets its own card. Cards are labelled:
- Mind name (from `mind_id` field)
- Short session ID (first 8 chars)
- `last_active` timestamp (relative: "3 min ago")
- Status dot: running=green, idle=yellow, closed=grey

Sorting: by `last_active` descending. Running sessions first, then idle, then closed.

---

## Stream Proxy

`GET /api/console/{session_id}/stream` — SSE endpoint on spark_to_bloom:

1. Auth check (reject if unauthenticated)
2. Open WebSocket to `ws://server:8420/sessions/{session_id}/stream`
3. Forward every received JSON message as an SSE `data:` line
4. If WS closes (session ended): emit `{"type":"session_closed"}` and end the SSE stream
5. Client reconnects automatically (EventSource browser behaviour) — on reconnect, if session is closed, card shows closed state

**Show everything** — no event filtering. tool_use, tool_result, assistant text chunks, user messages, system events all forwarded as-is.

---

## Frontend (console.html)

Vanilla JS, extends `layout.html`. No build step.

```
layout.html
  └── console.html
        ├── #minds-grid  (CSS Grid, auto-fill, minmax 320px)
        │     └── .mind-card  (one per session, injected dynamically)
        │           ├── .card-header  (mind name · session id · status dot · age)
        │           └── .card-feed   (scrolling event feed, monospace)
        └── <script>
              ├── loadSessions()     — fetch /api/console/sessions, diff against current cards
              │                        add new cards, remove cards for gone sessions
              ├── connectStream(id)  — open EventSource per session
              ├── renderEvent(event) — append to card feed
              └── poll()            — re-run loadSessions() every 10s
```

### Dynamic card behaviour

- **No placeholders.** The grid starts empty and fills only as sessions are found.
- **Poll every 10s** for new sessions. When a session appears that has no card yet → create card + connect stream immediately. It slides/fades in.
- **Session ends** → card stays visible (status dot goes grey, feed appends `— closed —`) for 5 minutes, then fades out and is removed from the DOM.
- Grid reflows automatically via CSS `auto-fill` — no layout logic in JS.

### Event rendering — everything, no filtering

| Event type | Rendered as |
|---|---|
| `user` | `user > [content]` — slate/muted |
| `assistant` chunk | `[mind] > [text]` — ice blue, streams in character by character |
| `tool_use` | `[tool: name]  {args}` — amber |
| `tool_result` | `[result]  {content}` — amber/dim |
| `system` | `[sys]  ...` — dark slate |
| `session_closed` | `— session closed —` — grey, italic |

Auto-scroll to bottom. Pause while user scrolls up; resume when within 100px of bottom.

---

## Theme & CSS

Matches the existing spark_to_bloom dark terminal aesthetic exactly.

### Colour tokens
```
background-deep:   #07090f   /* page background */
background-card:   #0d1117   /* card body */
background-header: #0d1a26   /* card header bar */
border:            #1e2d3d   /* card/grid borders */
border-glow:       #1e3a52   /* subtle glow border */
text-primary:      #dce8f0   /* main readable text */
text-secondary:    #94a3b8   /* assistant response text */
text-muted:        #475569   /* timestamps, labels */
text-dim:          #334155   /* very low contrast detail */
accent:            #38bdf8   /* sky blue — mind names, links, streaming text */
status-active:     #22c55e   /* green dot — responding */
status-thinking:   #f59e0b   /* amber — tool calls, thinking */
status-closed:     #475569   /* grey — idle/closed */
font-mono:         'Fira Code', monospace
font-ui:           inherit   /* same as rest of site */
```

### Grid
```css
#console-header {
  padding: 0.75rem 1rem;
  border-bottom: 1px solid #1e3a52;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-family: 'Fira Code', monospace;
  font-size: 0.72rem;
  letter-spacing: 0.15em;
  color: #38bdf8;
}

#minds-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 1rem;
  padding: 1rem;
}
```

### Card
```css
.mind-card {
  background: #0d1117;
  border: 1px solid #1e2d3d;
  border-radius: 8px;
  height: 420px;
  display: flex;
  flex-direction: column;
  box-shadow: 0 0 16px rgba(56, 189, 248, 0.04);
  animation: card-appear 0.3s ease-out;
}

.mind-card.closing {
  animation: card-fade 5s ease-out forwards;  /* fade out over 5min on session close */
}

@keyframes card-appear {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}

@keyframes card-fade {
  0%   { opacity: 1; }
  80%  { opacity: 1; }
  100% { opacity: 0; height: 0; margin: 0; padding: 0; overflow: hidden; }
}

.card-header {
  background: #0d1a26;
  border-bottom: 1px solid #1e2d3d;
  border-radius: 8px 8px 0 0;
  padding: 0.4rem 0.75rem;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-family: 'Fira Code', monospace;
  font-size: 0.68rem;
  letter-spacing: 0.1em;
  flex-shrink: 0;
}

.card-mind-name  { color: #38bdf8; }
.card-session-id { color: #475569; font-weight: normal; margin-left: 0.5rem; }
.card-age        { color: #475569; font-size: 0.65rem; }

.status-dot {
  display: inline-block;
  width: 6px; height: 6px;
  border-radius: 50%;
  margin-right: 4px;
  vertical-align: middle;
}
.status-dot.responding { background: #22c55e; }
.status-dot.thinking   { background: #f59e0b; }
.status-dot.closed     { background: #475569; }
```

### Feed
```css
.card-feed {
  flex: 1;
  overflow-y: auto;
  font-family: 'Fira Code', monospace;
  font-size: 0.75rem;
  line-height: 1.65;
  padding: 0.75rem;
  color: #94a3b8;
  scrollbar-width: thin;
  scrollbar-color: #1e2d3d #0d1117;
}

.feed-line             { display: block; margin-bottom: 0.1rem; }
.feed-line.user        { color: #475569; }
.feed-line.assistant   { color: #dce8f0; }
.feed-line.tool-use    { color: #f59e0b; }
.feed-line.tool-result { color: #78716c; }
.feed-line.system      { color: #334155; font-style: italic; }
.feed-line.closed      { color: #475569; font-style: italic; }

.feed-label {
  color: #38bdf8;
  margin-right: 0.5rem;
}

.cursor {
  display: inline-block;
  animation: blink 1s step-end infinite;
  color: #38bdf8;
}
@keyframes blink { 50% { opacity: 0; } }
```

---

## Files to Create/Modify

| File | Change |
|---|---|
| `src/main.py` | Add auth dependency, `/login`, `/logout`, `/console`, `/api/minds`, `/api/console/sessions`, `/api/console/{id}/stream`; protect `/graph`, `/graph/data`, `/graph/public-data` |
| `src/auth.py` | New — DB init, user lookup, password verify, session cookie sign/verify, `require_auth` FastAPI dependency |
| `src/templates/console.html` | New page |
| `src/templates/login.html` | New login form |
| `src/static/style.css` | Add `.mind-card`, `.card-feed`, `.card-header`, `.login-form` styles |
| `scripts/create_user.py` | New — CLI to bootstrap first user |
| `requirements.txt` | Add `bcrypt`, `itsdangerous` (or `passlib`) |
| `docker-compose.yml` (stb) | Add `STB_SECRET_KEY` env var, ensure `data/` is bind-mounted |

---

## Bootstrap / First Run

```bash
python scripts/create_user.py --username daniel --password <password>
```

Creates DB at `data/stb.db` if it doesn't exist, inserts hashed user. Run once on deploy.

---

## Out of Scope (this iteration)

- Password change / reset UI
- Multiple roles / permissions
- Session history replay (backfill)
- Pinning or annotating cards
- Filtering by mind
