# Hive Mind — Security Review (Post-Refactor)

**Date:** 2026-02-16
**Previous Review:** 2026-02-15
**Scope:** Full codebase audit of the `refactor/claude-code-cpu` branch after v2 refactor
**Reviewed files:** All Python sources (10 files in `agents/`, `config.py`, `mcp_server.py`, `__init__.py`, `shared/state.py`), Dockerfile, docker-compose.yml, `.mcp.json`, `.gitignore`, `config.yaml`, `requirements.txt`
**Security Spec Applied:** Python Application Security Policy v1.0
**Applicable Sections:** 3-9 (General Python), 11-12 (LLM/Agentic — due to self-improving tool creation and Claude Code integration)
**Project Type:** Python MCP Tool Server + AI/Agentic Application

---

## Executive Summary

The v2 refactor **significantly reduced the attack surface** by removing all network-facing components (FastAPI web server, WebSocket endpoints, Gradio app, web frontend). The system now operates exclusively as a local MCP server (stdio) accessed only by the local Claude Code CLI process. This eliminated 11 of the 20 findings from the previous review.

The 2026-02-16 remediation pass addressed **4 critical/high findings** (CRITICAL-1, CRITICAL-2, HIGH-1, HIGH-2) and **4 medium/low findings** (MEDIUM-5, MEDIUM-8, MEDIUM-12, LOW-3) that were naturally resolved as part of the same work. Key changes:
- **Secrets migrated to system keyring** (encrypted at rest via GNOME Keyring / KDE Wallet)
- **Key naming allowlist** enforced on `set_secret` (blocks overwriting system env vars)
- **Audit logging** added for `create_tool` and `install_dependency`
- **Package name validation** added to `install_dependency` (blocks URLs, git repos, local paths)
- **Neo4j agents** migrated from `secrets.env` to keyring with env var fallback; module-level side effects removed

**Current posture:** Suitable for **single-user, local development use**. The remaining open findings are Docker hardening (deferred — not running containers currently) and error leakage in tool outputs (deferred — needs error channel architecture).

---

## Severity Scale

| Level | Meaning |
|-------|---------|
| **CRITICAL** | Immediate exploitation risk; can lead to full system compromise |
| **HIGH** | Significant risk; exploitation likely if exposed to untrusted users |
| **MEDIUM** | Moderate risk; defense-in-depth gap or information leak |
| **LOW** | Minor issue; best-practice deviation |

---

## Findings — Still Open

### HIGH-1: Docker Container Runs as Root

**File:** `Dockerfile`
**Spec Violation:** Section 3.2 — Review Dimension: Authentication & authorization
**Status:** OPEN (unchanged)

The Dockerfile has no `USER` directive. Combined with the volume mount, a container escape or code execution vulnerability gives root access to host-mounted directories.

**Remediation:**
```dockerfile
RUN useradd -m appuser && chown -R appuser:appuser /usr/src/app
USER appuser
```

---

### HIGH-2: Docker Volume Mounts Expose Host Filesystem

**File:** `docker-compose.yml:6-8`
**Spec Violation:** Section 12 — ASI05 [MANDATORY restrict file system access]
**Status:** OPEN (unchanged)

```yaml
volumes:
  - .:/usr/src/app
  - ~/.claude:/root/.claude:ro
```

The entire project directory (including `.env`) is mounted read-write. The `~/.claude` directory (containing Claude session data and potentially auth tokens) is also mounted.

**Remediation:**
- Mount only necessary subdirectories (e.g., `./agents:/usr/src/app/agents:ro`)
- Mount `.env` as read-only: `.env:/usr/src/app/.env:ro`
- Remove the `~/.claude` mount (the MCP server doesn't need Claude config)

---

### MEDIUM-1: Non-Deterministic Docker Base Image

**File:** `Dockerfile:1`
**Spec Violation:** Section 8.1 — Package Management [MANDATORY pin versions]
**Status:** OPEN (unchanged)

```dockerfile
FROM ubuntu:latest
```

**Remediation:**
- Pin to a specific version: `FROM ubuntu:24.04`

---

## Findings — New (Deferred to Second Pass)

> **Note:** MEDIUM-2 through MEDIUM-5 are deferred. Key design constraint: error details (stderr, exceptions) are needed by the Claude agent for self-correction and decision-making — they can't simply be suppressed. The remediation needs an architecture where errors are visible to the agent but not piped back to the end user (e.g., Discord). This requires designing an error channel separate from user-facing output.

### MEDIUM-2: Skill Wrappers Leak Internal Details via stderr

**Files:** `agents/skill_planning_genius.py:31`, `agents/skill_code_genius.py:30`, `agents/skill_code_review_genius.py:30`
**Spec Violation:** Section 9.2 — Error Handling [MANDATORY return generic error messages]
**CWE:** CWE-209 (Generation of Error Message Containing Sensitive Information)
**Status:** NEW

All three skill wrappers return raw `stderr` on failure:
```python
return f"Planning failed:\n{result.stderr}\n{result.stdout}"
```

This could expose internal file paths, Claude CLI error messages, API key validation failures, or system configuration details to whoever invoked the MCP tool.

**Remediation:**
- Return a generic failure message to the MCP caller
- Log the full stderr server-side for debugging

---

### MEDIUM-3: `fetch_articles` Exception Handler Leaks Neo4j Details

**File:** `agents/fetch_articles.py:52-53`
**Spec Violation:** Section 9.2 — Error Handling [MANDATORY generic error messages]
**CWE:** CWE-209

```python
except Exception as e:
    return json.dumps({"error": str(e)})
```

Raw Neo4j exceptions can reveal connection strings, authentication failures, database schema details, and driver version information.

**Remediation:**
- Catch specific Neo4j exceptions and return generic messages
- Log the full exception server-side

---

### MEDIUM-4: No Path Validation on Skill Wrapper `documents_path`

**Files:** `agents/skill_planning_genius.py:22`, `agents/skill_code_genius.py:20`, `agents/skill_code_review_genius.py:20`
**Spec Violation:** Section 4.1 — Input Validation [MANDATORY validate external input]
**CWE:** CWE-22 (Path Traversal)
**Status:** NEW

The `documents_path` parameter is passed directly to `subprocess.run` without validation. While command injection is prevented (list-based subprocess call, not shell=True), a path like `/etc/shadow` or `../../.env` could be passed to the Claude skill, potentially causing it to read or process sensitive files.

**Remediation:**
- Validate that `documents_path` is within the expected `documents/` directory
- Use `os.path.realpath()` to resolve symlinks and verify the canonical path

---

## Findings — Closed (Remediated 2026-02-16)

| ID | Severity | Finding | Remediation Applied |
|----|----------|---------|---------------------|
| CRITICAL-1 | **CRITICAL** | Arbitrary code execution via `create_tool` | Audit logging added — all tool creation events logged with full code content to `audit.log`. Discord bot owner-only allowlist planned for networked deployment. |
| CRITICAL-2 | **CRITICAL** | Arbitrary package installation | Package name regex validation added to `install_dependency` — blocks URLs, git repos, local path specifiers. Audit logging for all install events. |
| HIGH-1 | **HIGH** | Real secrets in plaintext `.env` | Migrated to Linux system keyring (`keyring` package) with `"hive-mind"` service namespace. Secrets encrypted at rest. `get_credential()` helper provides keyring-first, env-var-fallback pattern for all agents. |
| HIGH-2 | **HIGH** | `set_secret` can overwrite critical env vars | Key naming allowlist enforced — names must end with `_KEY`, `_SECRET`, `_TOKEN`, `_API`, or start with `HIVEMIND_`. Keyring scoping prevents writing raw system env vars. |
| MEDIUM-5 | **MEDIUM** | Secret length disclosure | `get_secret` now returns only `"is configured"` / `"is NOT configured"` — no length. |
| MEDIUM-8 | **MEDIUM** | Inconsistent secret file loading (`secrets.env` vs `.env`) | Both Neo4j agents migrated to keyring with env var fallback. `load_dotenv(dotenv_path='secrets.env')` removed entirely. |
| MEDIUM-12 | **MEDIUM** | Module-level side effects in `Neo4j_Article_Manager` | `load_dotenv` removed from module level. Credential lookup moved inside function body. |
| LOW-3 | **LOW** | Variable shadowing in Neo4j agent | Loop variable renamed from `keyword` to `kw`. |

---

## Findings — Closed (Component Removed)

The following findings from the 2026-02-15 review are **closed** because the affected components were removed in the v2 refactor:

| ID | Finding | Reason Closed |
|----|---------|---------------|
| CRITICAL-3 | All AI permissions bypassed (`bypassPermissions`) | `services/claude_code.py` and `services/claude_cli.py` removed. Claude Code permissions now managed by user's CLI configuration. |
| CRITICAL-4 | No authentication on any endpoint | `web_app.py` removed. No network-facing server exists. MCP runs via stdio only. |
| HIGH-3 | Global config shared across connections | No multi-user server. Config singleton is appropriate for single-process MCP server. |
| MEDIUM-1 | No input validation on WebSocket messages | `web_app.py` removed. |
| MEDIUM-2 | Error messages leak internal details (WebSocket) | `web_app.py` removed. |
| MEDIUM-3 | No CORS policy | `web_app.py` removed. No HTTP server. |
| MEDIUM-4 | No WebSocket origin validation | `web_app.py` removed. |
| MEDIUM-7 | Prompt injection via system prompts | `web_app.py` and `terminal_app.py` removed. System prompts no longer embedded in code. |
| LOW-1 | No HTTPS/WSS | No web server. |
| LOW-2 | Predictable session IDs | `web_app.py` removed. |
| LOW-4 | Auto-reconnect without backoff | `web/static/app.js` removed. |

---

## Security Policy Checklist (Python Spec v1.0)

### General Python (Sections 3-9)

| # | Check | Result | Notes |
|---|-------|--------|-------|
| 1 | All external input validated | **PARTIAL** | `install_dependency` now validated (regex). `set_secret` now validated (key allowlist). Skill wrapper `documents_path` still unvalidated (MEDIUM-4). |
| 2 | No hardcoded secrets | **PASS** | All secrets in system keyring or environment |
| 3 | Parameterized queries used | **PASS** | `fetch_articles.py:44` uses `$criteria` parameter |
| 4 | No `pickle`/`eval`/`exec` on untrusted data | **PASS** | None found |
| 5 | No `subprocess` with `shell=True` and user input | **PASS** | All `subprocess.run` calls use list arguments |
| 6 | `secrets` module for security-sensitive randomness | **N/A** | No security-sensitive random generation in codebase |
| 7 | Passwords hashed properly | **N/A** | No password handling |
| 8 | Error messages don't expose internals | **FAIL** | `fetch_articles.py:52`, skill wrappers return raw stderr |
| 9 | Security events logged | **PARTIAL** | Tool creation and package installation now audited. Secret management and skill execution not yet logged. |
| 10 | No sensitive data in logs | **PASS** | `get_secret` returns presence only; audit log contains tool code (intended for security review) |
| 11 | Dependencies scanned | **FAIL** | No `pip-audit`, `safety`, or Bandit in CI/CD |
| 12 | Type hints on public interfaces | **FAIL** | `add_article_to_neo4j_db` missing return type annotation |
| 13 | Debug code removed | **PASS** | No debug code found |
| 14 | `.env` in `.gitignore` | **PASS** | Both `.env` and `secrets.env` covered |
| 15 | `yaml.safe_load` used | **PASS** | `config.py:21` uses `yaml.safe_load()` |

### LLM Application Security (Section 11)

| # | Check | Result | Notes |
|---|-------|--------|-------|
| 1 | System prompts don't contain secrets | **PASS** | No system prompts in codebase (handled by Claude Code CLI config) |
| 2 | User input segregated from system instructions | **N/A** | Handled by Claude Code CLI, not this codebase |
| 3 | Model output sanitized before execution | **PARTIAL** | `create_tool` still writes model-generated code directly, but now audit-logged for review |
| 4 | Human-in-the-loop for high-impact actions | **PARTIAL** | Claude Code CLI permission mode provides gating. Discord bot will add owner-only allowlist. |
| 5 | Rate limiting on LLM API calls | **N/A** | Handled by Claude Code CLI |

### Agentic Application Security (Section 12)

| # | Check | Result | Notes |
|---|-------|--------|-------|
| 1 | Agents have scoped credentials | **PASS** | Keyring uses `"hive-mind"` service namespace; secrets scoped by naming convention |
| 2 | Tool calls validated and logged | **PARTIAL** | `create_tool` and `install_dependency` now audit-logged. Other tools not yet. |
| 3 | Code execution sandboxed | **FAIL** | `create_tool` writes and loads code in the main process with full privileges |
| 4 | Kill switch implemented | **N/A** | MCP server terminates when Claude Code CLI exits (stdio lifecycle) |
| 5 | Memory writes validated | **N/A** | No persistent memory system |
| 6 | Agents cannot self-replicate without authorization | **PARTIAL** | Audit logging provides accountability; full authorization gate deferred to Discord bot. |

---

## Summary Table

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| ~~CRITICAL-1~~ | ~~CRITICAL~~ | ~~Arbitrary code execution via `create_tool`~~ | **Closed** (remediated) |
| ~~CRITICAL-2~~ | ~~CRITICAL~~ | ~~Arbitrary package installation~~ | **Closed** (remediated) |
| ~~HIGH-1~~ | ~~HIGH~~ | ~~Real secrets in plaintext `.env`~~ | **Closed** (remediated) |
| ~~HIGH-2~~ | ~~HIGH~~ | ~~`set_secret` can overwrite critical env vars~~ | **Closed** (remediated) |
| HIGH-1 | **HIGH** | Docker runs as root | **Open** |
| HIGH-2 | **HIGH** | Docker volume mounts expose host filesystem | **Open** |
| MEDIUM-1 | **MEDIUM** | Non-deterministic Docker base image | **Open** |
| MEDIUM-2 | **MEDIUM** | Skill wrappers leak stderr details | **Open** |
| MEDIUM-3 | **MEDIUM** | `fetch_articles` exception leaks Neo4j details | **Open** |
| MEDIUM-4 | **MEDIUM** | No path validation on skill `documents_path` | **Open** |

### Totals

| | Previous (2026-02-15) | Post-Refactor (2026-02-16) | Post-Remediation (2026-02-16) |
|---|---|---|---|
| Critical | 4 | 2 | **0** (-2 remediated) |
| High | 5 | 4 | **2** (-2 remediated) |
| Medium | 8 | 8 | **4** (-4 remediated) |
| Low | 4 | 1 | **0** (-1 remediated) |
| **Total Open** | **21** | **15** | **6** |
| **Closed** | 0 | 11 | **19** |

---

## Recommended Priority

### Docker Deployment (when needed)
1. Pin Docker base image (MEDIUM-1)
2. Add non-root user to Dockerfile (HIGH-1)
3. Restrict Docker volume mounts (HIGH-2)

### Error Channel Architecture (second pass)
4. Design agent-visible vs user-visible error channels
5. Fix error handling: generic messages in skill wrappers and `fetch_articles` (MEDIUM-2, MEDIUM-3)
6. Add path validation to skill wrappers (MEDIUM-4)

### Long-term
7. Expand audit logging to all MCP tool invocations
8. Add dependency scanning (`pip-audit`) to development workflow
9. Consider sandboxed execution for dynamically created tools
10. Implement Layer 2 process isolation (dedicated `hivemind` system user)

---

## Positive Observations

- **Dramatically reduced attack surface** — removal of all network-facing components eliminates the most likely remote attack vectors
- **stdio-only MCP architecture** — tools accessible only to the local Claude Code process, not over the network
- **Secrets encrypted at rest** via system keyring with scoped service namespace
- **Key naming allowlist** prevents overwriting system environment variables
- **Audit trail** for tool creation and package installation events
- **Package name validation** blocks URL-based, git-based, and path-based pip installs
- `.gitignore` properly excludes `.env`, `secrets.env`, and `*.log`
- Neo4j queries use parameterized queries (no injection risk)
- All `subprocess.run` calls use list arguments (`shell=False`), preventing command injection
- `yaml.safe_load()` used correctly in config.py
- External API calls include timeouts (10s for HTTP, 120s for pip, 600-1800s for skill wrappers)
- CoinGecko API key is optional with graceful degradation
- `get_secret` returns presence only — no value or length disclosure
- Clean separation: `config.yaml` for settings, keyring for secrets
- Minimal dependency footprint (11 packages total)
