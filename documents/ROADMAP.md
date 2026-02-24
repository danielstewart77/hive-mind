# Hive Mind Roadmap

## 1. Mobile-First Web UI Rewrite

**Problem:** The current vanilla HTML/JS/CSS chat interface is nearly unusable on mobile — layout doesn't adapt, touch targets are too small, and there's no responsive behavior.

**Goal:** Replace the static frontend with a component-based framework that provides responsive/mobile-first layout, better state management, and a foundation for richer UI features down the road.

**Considerations:**
- **Framework candidates:** React, Vue, Svelte, SolidJS — evaluate based on bundle size, ecosystem, and team familiarity.
- The backend (FastAPI + WebSocket) stays the same; only the client changes.
- Must preserve existing features: WebSocket streaming, voice record/playback, settings panel, slash commands.
- Add proper mobile affordances: responsive layout, swipe gestures, bottom-sheet settings, appropriately sized touch targets.
- Consider a component library (e.g. shadcn, Radix, Headless UI) for accessible, mobile-friendly primitives.

**Rough scope:**
- [ ] Choose framework and scaffold project (e.g. Vite + React)
- [ ] Implement chat view with streaming markdown rendering
- [ ] Implement voice UI (hold-to-record, playback)
- [ ] Implement settings panel / sidebar
- [ ] Responsive layout — mobile, tablet, desktop breakpoints
- [ ] Integrate into existing FastAPI static serving or add a build step

---

## 2. PostgreSQL Chat Thread Storage

**Problem:** Chat history is ephemeral — stored only in per-connection memory (`sessions` dict in `web_app.py`). Closing the tab loses everything. The terminal app has no history at all beyond the Claude Code SDK session.

**Goal:** Persist chat threads in the existing PostgreSQL database so users can resume, search, and review past conversations.

**Schema (initial):**

```sql
CREATE TABLE threads (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title         TEXT,                          -- auto-generated or user-set
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id    TEXT,                          -- Claude Code SDK session ID for resume
    backend       TEXT,                          -- 'anthropic' or 'ollama'
    model         TEXT                           -- model used
);

CREATE TABLE messages (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id     UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role          TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content       TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    audio_ref     TEXT                           -- optional reference to stored audio
);

CREATE INDEX idx_messages_thread ON messages(thread_id, created_at);
CREATE INDEX idx_threads_updated ON threads(updated_at DESC);
```

**Scope:**
- [ ] Add PostgreSQL connection config to `.env` / `config.py`
- [ ] Create migration scripts (or use Alembic)
- [ ] Add a `services/db.py` module with async connection pool (asyncpg)
- [ ] Wire thread create/load/list into `web_app.py` WebSocket handler
- [ ] Add thread list / switcher UI in the web frontend
- [ ] Optionally wire into `terminal_app.py` for persistent terminal history

---

## 3. Authentication & Access Control

**Problem:** The web interface is wide open — no login, no tokens, no access control. Anyone who can reach the port has full access to Claude Code (which can read/write files, run commands, manage secrets). This is a critical attack surface, especially when exposed beyond localhost.

**Goal:** Add authentication to the web interface so only authorized users can interact with Hive Mind.

**Considerations:**
- **Approach candidates:**
  - **Simple API key / shared secret** — low effort, good for single-user self-hosted. A token in `.env`, validated on WebSocket connect via query param or first message.
  - **Username/password with sessions** — login page, hashed passwords in PostgreSQL (depends on item 2), JWT or session cookie.
  - **OAuth / SSO** — Google, GitHub, etc. More complex but standard for multi-user setups.
- Start simple (API key or single-user password), upgrade later if multi-user is needed.
- WebSocket connections must be authenticated — validate before accepting or on the first frame.
- The `/api/status` endpoint and static assets can remain open; the `/ws` endpoint must be gated.
- Terminal app doesn't need auth (local access only).

**Scope:**
- [ ] Add `AUTH_SECRET` or `HIVE_MIND_PASSWORD` to `.env` / `config.py`
- [ ] Add login page or token prompt to the web UI
- [ ] Validate credentials on WebSocket upgrade (reject unauthorized connections)
- [ ] Add session/token management (JWT or secure cookie)
- [ ] Rate limiting on auth attempts
- [ ] Optional: tie into PostgreSQL users table for multi-user support later

---

## 4. Modern Terminal UX

**Problem:** The terminal REPL is a basic `input()` loop. No arrow-key navigation, no history recall, no inline editing — unlike modern CLI tools (Claude Code, etc.).

**Goal:** Upgrade the terminal experience with readline-style editing, history, and keybindings.

**Features:**
- **Up/Down arrows** — cycle through command history (persisted across sessions)
- **Left/Right arrows** — cursor movement within the current input
- **Ctrl+C** — cancel current input (not kill the process)
- **Ctrl+R** — reverse search through history
- **Tab completion** — for slash commands (`/backend`, `/model`, `/clear`, `/status`)
- **Multiline input** — Shift+Enter or backslash continuation
- **Syntax highlighting** — optional, for code blocks in responses

**Implementation options:**
- [`prompt_toolkit`](https://github.com/prompt-toolkit/python-prompt-toolkit) — full-featured, supports history, completion, key bindings, mouse, async
- [`readline`](https://docs.python.org/3/library/readline.html) — stdlib, lighter, covers history and basic editing

**Scope:**
- [ ] Replace `input()` with `prompt_toolkit` prompt session
- [ ] Add persistent history file (`~/.hive_mind_history`)
- [ ] Add slash-command tab completion
- [ ] Add Ctrl+C handling for input cancellation
- [ ] Improve streaming output rendering (live markdown)

---

## 5. Web UI Keyboard Shortcuts & Power Features

**Problem:** The web UI has minimal keyboard support — just Enter to send. No shortcuts, no history navigation, no command palette.

**Goal:** Add keyboard-driven workflows to match the terminal experience and modern web apps.

**Features:**
- **Up arrow** (in empty input) — recall previous messages for editing/resending
- **Ctrl+K / Cmd+K** — command palette (search commands, switch threads, change settings)
- **Ctrl+Shift+V** — toggle voice mode
- **Escape** — close settings panel, cancel recording
- **Ctrl+L** — clear chat display
- **Slash commands** — autocomplete dropdown when typing `/`
- **Thread switcher** — keyboard-navigable sidebar (depends on item 2)

**Scope:**
- [ ] Add keydown event handler with shortcut dispatch
- [ ] Implement input history ring buffer
- [ ] Build command palette overlay component
- [ ] Add slash-command autocomplete dropdown
- [ ] Wire thread navigation shortcuts (after PostgreSQL storage lands)

---

## Priority Order

| Priority | Item | Dependency |
|----------|------|------------|
| 1 | PostgreSQL chat storage | None |
| 2 | Authentication & access control | Benefits from PostgreSQL for user/session storage |
| 3 | Mobile-first UI rewrite | None (but benefits from storage + auth) |
| 4 | Modern terminal UX | None |
| 5 | Web UI keyboard shortcuts | Builds on items 1 and 3 |
