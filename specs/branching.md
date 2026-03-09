# Branch Strategy

## Branch Naming

```
master          — stable, deployed
story/*         — SDLC stories (created by orchestrator, e.g. story/audit-logging)
feature/*       — new features (manual development)
fix/*           — bug fixes (manual development)
refactor/*      — refactoring work (manual development)
```

`master` is the base branch for all PRs.

## PR Checklist

Before merging any PR, verify:

- [ ] No secrets or credentials in tracked files
- [ ] `.gitignore` updated if new generated/personal file types added
- [ ] `config.yaml.example` updated if `config.yaml` schema changed
- [ ] `docs/security/security-usability-tradeoffs.md` updated if new open security findings apply
- [ ] `goals.md` updated if new autonomous capabilities were added
