# Security Mitigation: T1 Container Escape & Privilege Escalation

## Problem Statement

Hive Mind's self-improvement capability (runtime tool creation via `create_tool()`) introduces a
critical attack surface: an attacker who can influence Claude's behavior — via prompt injection or
unauthenticated gateway access — can write arbitrary Python code to `agents/`, which is
auto-discovered and executed immediately in the same process. Combined with a host bind mount
(`.:/usr/src/app`), successful code execution inside the container translates directly to host
filesystem write access.

**Goal:** Preserve full tool creation functionality while making container escape and host
compromise significantly harder through layered containment.

---

## Threat Summary (T1)

**Attack path (current):**
1. Attacker reaches gateway (no authentication required)
2. Prompt-injects Claude into calling `create_tool()` with malicious payload
3. Code written to `agents/`, `discover_tools()` called immediately
4. Malicious tool executes in MCP server process — full access to env vars, secrets, filesystem
5. Via bind mount, attacker writes to host filesystem (cron jobs, SSH keys, etc.)

---

## Mitigation Strategy: Concentric Rings of Containment

The goal is not to block tool creation — it is to ensure that each layer of the stack limits
what a successful exploit can actually achieve.

---

### Ring 0 — Remove `~/.claude` Mount Entirely

**What it does:** Eliminates the direct write path from container to host `~/.claude`, closing
a host persistence attack that requires no container escape whatsoever.

**Why this is Ring 0:** This attack does not require a container escape. The current setup
mounts the host's `~/.claude` read-write into the container. Anything written to
`/home/hivemind/.claude/` inside the container immediately appears on the host. A prompt
injection that writes a malicious skill file survives container teardown and fires the next
time Claude Code runs anywhere on the host.

**Attack path (current, unmitigated):**
1. Attacker influences Claude (prompt injection or unauthenticated gateway) to write a file to
   `/home/hivemind/.claude/skills/evil/SKILL.md`
2. That file appears immediately on the host at `~/.claude/skills/evil/SKILL.md`
3. Next time Claude Code runs on the host — in any context — it loads the skill
4. The skill contains instructions Claude Code follows with host-level permissions
5. Gateway runs Claude with `bypassPermissions` — those instructions execute without any prompt

This is a persistence mechanism: the container can be torn down and the backdoor survives.

**Why you cannot simply replace with `ANTHROPIC_API_KEY`:**

`ANTHROPIC_API_KEY` and `claude login` credentials are not interchangeable:
- `claude login` stores OAuth tokens in `.credentials.json` that authenticate against your
  Claude Max subscription — usage counts against the fixed monthly plan
- `ANTHROPIC_API_KEY` routes through the Anthropic API directly — **pay-per-token**, billed
  separately from any Max/Pro subscription, and will override OAuth credentials if both are set

Swapping to an API key would convert every session from flat-rate to metered billing. The
`.credentials.json` file is a required dependency.

**Fix — mount only the credentials file, read-only:**

Instead of mounting the entire `~/.claude` directory, mount only the one file that is actually
needed. Everything else (history, project data, host settings) stays on the host only.

```yaml
# docker-compose.yml — replace the ~/.claude volume line in all services
volumes:
  - .:/usr/src/app
  - ~/.claude/.credentials.json:/home/hivemind/.claude/.credentials.json:ro
```

Skills are served from `/usr/src/app/.claude/skills/` (already exists — Claude Code reads
project-level skills from the `cwd`). The container gets its own ephemeral `~/.claude/` for
everything else. Only the credentials file bridges host and container, and it is read-only.

**What this achieves vs. the current full mount:**

| | Current (full mount, rw) | Fix (credentials only, ro) |
|--|--|--|
| Can write skills to host | Yes — critical | No |
| Can read host history/sessions | Yes | No |
| Max plan billing works | Yes | Yes |
| Blast radius if compromised | Full host `~/.claude` | One read-only file |

**Fallback — full mount read-only (weaker, simpler to apply immediately):**

```yaml
volumes:
  - ~/.claude:/home/hivemind/.claude:ro
```

This blocks writes back to the host but still exposes all host `~/.claude` data (history,
session contents, project files) to a compromised container. Acceptable as an interim step
while setting up the targeted mount above.

---

### Ring 1 — AST Validation Before Tool Loading

**What it does:** Intercepts `create_tool()` before writing to `agents/`, parses the submitted
code with Python's `ast` module, and rejects known-dangerous patterns.

**Implementation:**

Introduce a `validate_tool_code(code: str) -> list[str]` function in `tool_creator.py`:

```python
import ast

BLOCKED_CALLS = {"eval", "exec", "__import__", "compile"}
BLOCKED_MODULES = {"pty", "ctypes", "socket"}  # extend as needed
ALLOWED_MODULES = {
    "os.path", "json", "re", "datetime", "requests",
    "agent_tooling", "keyring", "subprocess",  # subprocess allowed but shell=True blocked
}

def validate_tool_code(code: str) -> list[str]:
    violations = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError: {e}"]

    for node in ast.walk(tree):
        # Block shell=True in any subprocess call
        if isinstance(node, ast.keyword):
            if node.arg == "shell" and isinstance(node.value, ast.Constant):
                if node.value.value is True:
                    violations.append("subprocess with shell=True is not permitted")

        # Block dangerous builtins
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_CALLS:
                violations.append(f"blocked builtin call: {node.func.id}()")

        # Flag blocked module imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in BLOCKED_MODULES:
                    violations.append(f"blocked import: {alias.name}")
        if isinstance(node, ast.ImportFrom):
            if node.module in BLOCKED_MODULES:
                violations.append(f"blocked import: {node.module}")

    return violations
```

**Staging pattern:**
- Write submitted code to `agents/staging/<name>.py`
- Run validation; reject and report violations if any found
- On pass, move to `agents/<name>.py` and call `discover_tools()`
- Staging directory is never on the auto-discovery path

**Limitations:** AST validation is not a sandbox — sufficiently obfuscated code can bypass it.
It is a first-line filter, not a guarantee. It catches obvious patterns and raises the effort
required for a successful attack.

---

### Ring 2 — MCP Tool Process Isolation

**What it does:** Runs each MCP tool invocation in a child subprocess with a stripped environment
rather than in the MCP server process. Prevents a malicious tool from reading parent process
memory, environment variables (API keys, tokens), or file descriptors.

**Implementation:**

Introduce a `tool_runner.py` shim:

```python
# agents/tool_runner.py
import sys, json, importlib

tool_name = sys.argv[1]
args = json.loads(sys.argv[2])

mod = importlib.import_module(tool_name)
fn = getattr(mod, tool_name)
result = fn(**args)
print(json.dumps(result))
```

Invoke via subprocess in the MCP server instead of calling tool functions directly:

```python
import subprocess, sys, json

SAFE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/hivemind",
    # Pass only the specific env vars the tool declares it needs
}

result = subprocess.run(
    [sys.executable, "agents/tool_runner.py", tool_name, json.dumps(args)],
    env=SAFE_ENV,
    capture_output=True,
    timeout=30,
    cwd="/usr/src/app",
)
```

**Tradeoff:** Adds latency per tool call. Tool runner needs a mechanism to declare which env
vars it requires (e.g., a `required_secrets` attribute on the `@tool` decorator) so they can
be passed explicitly rather than inheriting the full environment.

---

### Ring 3 — Container Hardening (Zero Functionality Loss)

These changes require no code modifications and cost nothing in terms of capability.

**`docker-compose.yml` additions:**

```yaml
services:
  server:
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    cap_add: []  # add back only if a specific capability is proven necessary
    read_only: true
    tmpfs:
      - /tmp:mode=1777
      - /var/run
```

**`no-new-privileges`** — prevents any process inside the container from gaining privileges via
setuid binaries, sudo, or capability-granting executables. Blocks a broad class of local
privilege escalation.

**`cap_drop: ALL`** — removes all Linux capabilities from the container. The Claude process and
MCP tools do not require any capabilities. This blocks kernel-level exploit paths that rely on
capabilities (e.g., `CAP_SYS_ADMIN`, `CAP_NET_ADMIN`, `CAP_PTRACE`).

**`read_only: true` + `tmpfs`** — root filesystem is read-only. Malicious code cannot modify
Python interpreter, system binaries, or installed packages. Writable scratch space at `/tmp`
only.

**Seccomp:** Docker's default seccomp profile already blocks ~44 high-risk syscalls. Consider
adding a custom profile that additionally blocks `ptrace`, `mount`, `pivot_root`, and
`clone` with `CLONE_NEWUSER` — the syscalls most container escape techniques rely on.

---

### Ring 4 — Replace Host Bind Mount with Named Volumes

**What it does:** Eliminates the direct path from container filesystem write → host filesystem
write. Currently `.:/usr/src/app` means anything the container writes, the host sees
immediately.

**Production `docker-compose.yml`:**

```yaml
volumes:
  agents_vol:
  sessions_vol:

services:
  server:
    volumes:
      - agents_vol:/usr/src/app/agents      # tool creation still works
      - sessions_vol:/usr/src/app/data      # session DB persists
      # .:/usr/src/app  <-- REMOVED in production
```

**Development override (`docker-compose.override.yml`, gitignored):**

```yaml
services:
  server:
    volumes:
      - .:/usr/src/app  # bind mount re-enabled for local dev only
```

**Tradeoff:** Tools created at runtime in production do not sync back to the local `agents/`
directory. This is acceptable — tools intended for permanent inclusion should be written locally
and deployed. Runtime-created tools are ephemeral to the named volume.

---

### Ring 5 — User Namespace Remapping

**What it does:** Maps container UID 0 (root) to an unprivileged UID on the host (e.g., 100000).
If an attacker escapes the container via a kernel exploit, they arrive on the host as an
unprivileged user with no access to system directories, other users' files, or Docker internals.

**Docker daemon configuration (`/etc/docker/daemon.json`):**

```json
{
  "userns-remap": "default"
}
```

Docker creates a `dockremap` user and maps container UIDs 0–65535 to host UIDs 100000–165535.

**Tradeoff:** Named volume permissions need to be set for the remapped UID. Some images that
assume UID 0 inside the container may need minor adjustments. Docker socket access from inside
the container (if ever added) becomes more complex.

---

## End-to-End Attack Scenario (Post-Mitigation)

| Step | Attacker Action | Mitigation |
|------|----------------|------------|
| 1 | Reaches gateway, no credentials | Gateway auth (separate T5 fix) blocks here |
| 2 | Injects Claude to write malicious skill to `~/.claude/skills/` | **Ring 0:** Mount is read-only — container cannot write to host `~/.claude` |
| 3 | Injects Claude into calling `create_tool()` with reverse shell | **Ring 1:** AST validation rejects `socket`, `subprocess shell=True`, etc. |
| 4 | Crafts obfuscated code that passes AST | Harder; goes to staging, still must pass gate |
| 5 | Tool loads and executes | **Ring 2:** Runs in subprocess with stripped env — no API keys visible |
| 6 | Tool attempts privilege escalation within container | **Ring 3:** `no-new-privileges` + `cap_drop` blocks it; seccomp blocks escape syscalls |
| 7 | Tool attempts to write to host via mount | **Ring 4:** No bind mount in production; writes land in named volume only |
| 8 | Kernel exploit achieves container escape | **Ring 5:** Attacker is UID 100000 on host — unprivileged, cannot write sensitive paths |

---

## Implementation Priority

| Priority | Change | Effort | Impact |
|----------|--------|--------|--------|
| 1 | **Ring 0:** Replace full `~/.claude` mount with credentials-only targeted mount (`:ro`) | Low — config only | Critical |
| 2 | Ring 3: `no-new-privileges` + `cap_drop` in compose | Low — config only | High |
| 3 | Ring 4: Named volumes, dev override file | Low — config only | High |
| 4 | Ring 1: AST validation in `tool_creator.py` | Medium — code change | Medium-High |
| 5 | Ring 5: User namespace remapping | Medium — daemon config | High |
| 6 | Ring 2: MCP tool subprocess isolation | High — architectural change | Medium |

Ring 0 is the single highest-priority fix: replacing the full read-write `~/.claude` mount
with a targeted read-only credentials-only mount closes a host persistence path that requires
no container escape. Rings 3 and 4 together are the next highest ROI.

---

## Out of Scope

This document addresses T1 (container escape / host privilege escalation) only.
The following threats are documented separately:

- **T2** (persistent backdoor) — mitigated in large part by Ring 1 and Ring 4 above
- **T3** (credential exfiltration) — requires gateway auth + `agent_logs()` path restrictions
- **T4** (supply chain via `install_dependency()`) — requires separate gating mechanism
- **T5** (session hijacking) — requires gateway authentication
- **T6** (prompt injection via external data) — requires tool-layer data sandboxing
- **T7** (DoS / quota exhaustion) — requires rate limiting

---

## T8 — Email Access & 2FA Bypass

### Problem Statement

Read-only email access is often treated as low-risk. It is not. If a prompt injection attacker
can cause the system to read email, they gain access to any OTP or magic-link sent to that
inbox — effectively bypassing email-based two-factor authentication on every account that uses it.
This elevates read-only email access from "data exfiltration risk" to "full account takeover risk"
for any service using email as a second factor.

**Attack path:**
1. Attacker injects a prompt causing the system to fetch recent emails
2. Attacker's controlled page or content triggers the target service to send a password-reset
   or login email to the user's inbox
3. System reads the OTP or magic link from email and the attacker captures it via exfiltration
4. Attacker uses the link/code to access the target account — bypassing 2FA entirely

**Mitigation:**
- Do not use email as a second factor for any account that matters. Use TOTP (e.g., Authy,
  1Password TOTP) or a hardware key (YubiKey). Email 2FA is weak even without AI access to
  the inbox.
- Scope email credentials to read-only and, if possible, to specific folders/labels (e.g.,
  newsletters only) rather than the full inbox — this limits exposure but does not eliminate it.
- Log every email read operation so anomalous access patterns are detectable.

---

## Human-in-the-Loop (HITL) Confirmation with Out-of-Band Channel

### Principle

Any action that is mutating, destructive, or high-blast-radius should require explicit human
approval before execution. A single button click is not burdensome. What matters is that the
confirmation mechanism is **outside the blast radius of a compromised session**.

### The Blast Radius Problem

A naive HITL implementation — where the same session or container that initiates an action also
handles its confirmation — provides no real security. If a prompt injection can trigger `send_email`,
it can also trigger `confirm_send_email`. The attacker controls both.

The confirmation channel must be **unreachable from within the tool execution environment**:

- The confirmation token must never be passed to tool subprocesses
- The confirmation UI must not accept requests from the container's internal network
- The approval signal must arrive via an inbound channel the container cannot forge

### Architecture: Out-of-Band Confirmation

```
[Tool Subprocess]                      [Gateway (memory only)]
      |                                        |
      | requests destructive action            |
      |--------------------------------------> |
                                               | generates one-time UUID token
                                               | stores token in-memory (NOT passed to subprocess)
                                               |
                                               | pushes notification to external channel
                                               |-------> [Telegram / Discord DM / Confirmation App]
                                                                      |
                                                          Human sees: "Approve: delete email X?"
                                                          Human clicks: [Approve] or [Deny]
                                                                      |
                                               |<------ inbound webhook with UUID token
                                               |
                                               | validates UUID, proceeds or cancels
```

**Why the tool subprocess cannot forge this:**
- It never sees the UUID token (generated in gateway memory after the request)
- It cannot send an inbound webhook to the gateway on behalf of the human
- It cannot reach the Telegram/Discord/confirmation app directly (network isolation)

### Confirmation Channel Options

| Channel | Notes |
|---------|-------|
| **Telegram bot** | Good. Runs as a separate lightweight process. Use a dedicated bot token not shared with the main system. |
| **Discord DM** | Viable. Keep the confirmation handler in a minimal separate process, not the main gateway, to avoid sharing the execution environment. |
| **Dedicated confirmation web app** | Best separation. A minimal static app (just approve/deny buttons) in its own container with no shared environment. Can be private/LAN-only. |

### Critical Separation Requirements

1. The confirmation service must run in a **separate container** with no shared environment
   variables or filesystem mounts with the tool execution environment.
2. The gateway holds the token in **process memory only** — not in the database, not in env
   vars, not in any file the tool subprocess can read.
3. The confirmation service must **not accept requests from the container's internal network** —
   only from the external channel (Telegram/Discord servers, or the human's browser directly).
4. Tokens must be **single-use and short-lived** (e.g., 60-second TTL) to prevent replay attacks.

### What Requires HITL Confirmation

| Action | Confirmation required |
|--------|----------------------|
| Send email | Yes |
| Delete email | Yes |
| Modify calendar events | Yes |
| Post to social media | Yes |
| Execute shell commands beyond tool scope | Yes |
| Read email (newsletters, non-sensitive) | No — but log it |
| Read email (any message matching OTP/auth patterns) | Yes — flag and require confirmation |
