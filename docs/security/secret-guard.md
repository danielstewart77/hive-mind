# Secret guard

A pre-commit hook that refuses any commit whose **added** lines contain a
credential. It exists because every other layer of protection here depends
on a mind exercising judgment about data it is already holding, and that is
not a control.

The condition is permanent: `training_turns.db` stores captured tool output
losslessly, so live credentials sit in it by design, and any session that
reads the corpus pulls real secrets into a working context. Nothing
downstream of that should be trusted to remember.

## What it does

`tools/stateless/secret_guard/secret_guard.py` scans the staged diff with
`core.training_redaction.find_secrets` — the same detector the export path
uses, so there is one ruleset and no second opinion to drift out of sync.

Only added lines are scanned. A commit that *removes* a credential must
stay possible, or the fix for a leak is the one commit nobody can make.

Findings report the file, the line, the rule and a preview (`ghp_…(41
chars)`). The value is never printed — a guard that leaks the secret in
order to report it has solved nothing.

There are two exemptions, both properties of the **value** rather than the
path. A line carrying the marker `secret-guard: allow` is skipped, and so is
any line whose text contains one of `INVENTED`, `EXAMPLE`, `FIXTURE`,
`NOTAREAL` or `PLACEHOLDER`. Never exempt by path — the 2026-07-30 incident
*was* a test file, and `tests/` is precisely where a detector's fixtures and
a real leak look identical.

`scan-path .` currently reports roughly forty-five findings across the
existing training and auth test suites: invented fixtures predating the
convention, using shapes like `ghp_AAAAbbbb…`. They are committed already,
and the guard only inspects added lines, so nothing is blocked today. Each
is fixed the next time somebody edits that line, by renaming the fixture to
announce itself.

## Install

```bash
tools/stateless/secret_guard/secret_guard.py install
```

Writes `~/.git-hooks/` and sets `core.hooksPath` globally, so every repo on
the host is covered at once — including ones cloned later. A per-repo
install protects only the repos somebody remembered.

`core.hooksPath` **replaces** `.git/hooks` rather than adding to it, so the
installed set also chains: each hook execs the repo's own
`$(git rev-parse --git-dir)/hooks/<name>` if one exists. Address the local
hook through `--git-dir`, never `--git-path` — the latter is
`core.hooksPath`-aware and resolves to the installed hook itself, which
re-executes and hangs the commit in a fork loop.

## Scope

```bash
secret_guard.py scan-path core/ tools/    # audit a checkout after the fact
secret_guard.py scan-staged               # JSON, one object on stdout
```

`git commit --no-verify` still bypasses it, by git's design. That is
acceptable: the goal is to make the accident impossible, not the deliberate
act.
