---
name: master-code-review
description: "Security-aware code review orchestrator. Detects project language/framework, loads the matching security spec, then runs a structured code review combining /code-review-genius quality dimensions with policy-driven security analysis. Use when code needs a comprehensive review before PR."
argument-hint: [documents-path]
tools: Read, Write, Edit, Glob, Grep, Bash, Skill
model: opus
user-invocable: true
---

# Master Code Review

`$ARGUMENTS[0]` = documents path; ask if missing.
Security specs: `~/.claude/skills/master-code-review/security/`
Output: `<docs>/CODE-REVIEW.md` (enhanced with security section). Read-only — no code changes.

## STEP 1 — Detect Project Type & Load Spec

Scan repo for markers:
- **Python**: `*.py`, `pyproject.toml`, `requirements.txt`, fastapi/django/flask imports → load `security/python-spec.md`
  - Always apply: Sections 3–9, 13–14
  - FastAPI detected → add Section 10
  - LLM imports (openai, anthropic, langchain, etc.) → add Section 11
  - Agentic patterns (agent, tool_use, multi-agent) → add Section 12
- **Angular**: `angular.json` or `*.component.ts` → no spec yet, inform user
- **Dotnet**: `*.csproj`, `*.sln`, `Program.cs` → no spec yet, inform user
- Unknown → no spec, inform user, run standard review only

Read the full spec file if selected.

## STEP 2 — Standard Quality Review
Run `/code-review-genius <docs-path>`. Wait for completion → produces `<docs>/CODE-REVIEW.md`.

## STEP 3 — Re-Read Everything
Read the CODE-REVIEW.md output. Re-read every changed file in full for the security pass.

## STEP 4 — Security Pass (skip if no spec)

For each changed file, evaluate only applicable spec sections:

**4a. Input & Injection** (Sections 4, 10.2)
- Input validated before use? (Pydantic for FastAPI, type checks for scripts)
- Parameterized queries? No raw SQL with user input?
- `pickle`/`eval()`/`exec()`/`yaml.load()` on untrusted data?
- `subprocess` with `shell=True` + user-controlled args?
- User-controlled format strings?

**4b. Secrets** (Section 7)
- Hardcoded credentials/API keys/tokens/connection strings?
- `.env` files committed?
- Secrets passed as function params without secure handling?

**4c. Auth & AuthZ** (Sections 5, 10.1)
- FastAPI: auth via dependency injection? AuthZ at every endpoint? Tokens validated (sig, expiry, issuer, audience)? `passlib` with bcrypt/Argon2?

**4d. Crypto & Randomness** (Section 6)
- `secrets` module (not `random`) for security values? bcrypt/Argon2 (not MD5/SHA alone)? `cryptography` lib (not custom)?

**4e. Logging & Errors** (Section 9)
- Error responses leak stack traces/paths/internals? Security events logged? PII/tokens excluded from logs? Bare `except:` clauses?

**4f. FastAPI Hardening** (Section 10, if applicable)
- CORS: explicit origins (no wildcards)? Security headers via middleware? Rate limiting on auth? `/docs`/`redoc` restricted for prod? CSRF on state-changing ops?

**4g. LLM Security** (Section 11, if applicable)
- System prompts contain secrets? User input segregated from system instructions? Model output sanitized before render/exec? Human-in-loop for high-impact actions? Rate limiting + timeouts on LLM calls?

**4h. Agentic Security** (Section 12, if applicable)
- Agents scoped with least-privilege? Tool calls validated + logged? Code execution sandboxed? Kill switch? Memory writes validated?

Record each finding: file:line, spec section violated, severity, [MANDATORY]/[PROHIBITED] tag, description, fix.

## STEP 5 — Enhance CODE-REVIEW.md

Append after "Findings", before "Remediation Plan":
```markdown
---
## Security Policy Review
**Security Spec Applied:** <name or None>
**Applicable Sections:** <list>
**Project Type:** <detected type>

### Security Findings
#### Critical
##### SC1: <title>
- **File:** `path:line`
- **Spec Violation:** Section X.Y - <title> [MANDATORY|PROHIBITED]
- **CWE:** CWE-XXX
- **Description:** <what/why>
- **Suggested Fix:** <how>
#### Major
#### Minor

### Security Checklist
| # | Check | Result | Notes |
|---|-------|--------|-------|
```

Also update:
- **Summary**: append "Security review: <PASS | X findings (Y critical, Z major)> against <spec>."
- **Verdict**: Critical security → CHANGES REQUESTED; Major security → at least APPROVED WITH MINOR FIXES
- **Remediation Plan**: append security steps referencing spec sections. Don't duplicate findings already in quality review.

## STEP 6 — Update State
If `<docs>/STATE.md` exists, update the code review state marker.

## STEP 7 — Notify & EXIT
`/notify-me`: "Master code review complete for story #<N>. Quality verdict: <verdict>. Security: <PASS | X findings>. Spec: <spec or None>."
