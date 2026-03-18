# Skills Enhancement Plan

*Based on Anthropic's published lessons from building Claude Code Skills at scale.*
*Reference: "Lessons from Building Claude Code: How We Use Skills" — March 2026*

---

## New Patterns Available

### 1. `context: fork`
Runs the skill in an isolated subagent context. Main conversation context is not polluted. Use for:
- Multi-step orchestration with side effects
- Deep file reads that would bloat context
- Skills that spawn sub-skills

### 2. Dynamic Context Injection
Shell commands inside SKILL.md using backtick syntax run at invocation, injecting output before Claude sees the skill body. Example: inject `git status` so the skill always has current state without asking Claude to fetch it.

### 3. Bundled Executable Scripts
Deterministic steps belong in Python/shell scripts, not Claude reasoning. The skill invokes the script; Claude handles the judgment calls around it.

### 4. Progressive Disclosure
SKILL.md stays lean. Reference `specs/` files for detail — loaded only when needed. Applies to any skill over ~200 lines.

### 5. Dynamic HTML Output
Skills can bundle scripts that generate interactive HTML — charts, graphs, visualizations — rather than static text output.

---

## Skills Audit

### `user-invocable` Fixes (Quick Wins)

These skills are internal procedures — users should never invoke them directly:

| Skill | Current | Should Be |
|-------|---------|-----------|
| `agent-logs` | unknown | `user-invocable: false` |
| `check-reminders` | unknown | `user-invocable: false` |
| `crypto-price` | unknown | `user-invocable: false` |
| `current-time` | unknown | `user-invocable: false` |
| `knowledge-graph-save` | unknown | `user-invocable: false` |
| `notify-action` | unknown | `user-invocable: false` |
| `notify` | false | correct |
| `pin-memory-action` | unknown | `user-invocable: false` |
| `planka` | unknown | `user-invocable: false` |
| `reminders` | unknown | `user-invocable: false` |
| `secrets` | unknown | `user-invocable: false` |
| `semantic-memory-save` | unknown | `user-invocable: false` |
| `weather` | unknown | `user-invocable: false` |
| `x-search` | unknown | `user-invocable: false` |

---

### `context: fork` Candidates

Skills that do complex chained work with side effects — isolation prevents context bleed:

| Skill | Reason |
|-------|--------|
| `code-genius` | Multi-cycle test/code/lint loop — each cycle pollutes context |
| `code-review-genius` | Deep file reads across many files |
| `master-code-review` | Loads security spec + runs full review |
| `orchestrator` | Spawns sub-skills, manages pipeline state |
| `planning-genius` | Phase 2 codebase exploration is read-heavy |
| `story-close` | card → git → rebuild → health check — many side effects |
| `story-start` | Spawns planning-genius, manages Planka state |
| `knowledge-graph-save` | Fuzzy search + disambiguation loop |
| `memory-manager` | Pipeline orchestrator — parse → classify → route → save |
| `3am` | Nightly autonomous session — highest risk of context pollution |

---

### Dynamic Context Injection Candidates

Inject live system state at invocation so skills have current context without a fetch step:

| Skill | What to Inject |
|-------|---------------|
| `1pm` | Current date + today's calendar events |
| `7am` | Current date + today's calendar events (both calendars) |
| `sitrep` | Last 10 error lines per container + `memory_retrieve("recent system issues")` |
| `story-close` | `git log --oneline -5` + recent container logs |
| `story-start` | Current Planka board state + repo file tree |
| `orchestrator` | Planka board + recent PRs + `git log --all --oneline -10` |
| `code-genius` | `git status` + recent test failures |
| `code-review-genius` | `git diff --stat` + changed file list |
| `master-code-review` | Detected language/framework + `git diff --stat` |
| `planning-genius` | Existing tool list + similar file patterns |
| `update-documentation` | `git log --oneline -20` filtered to doc-touching commits |
| `tool-creator` | `ls tools/stateless/ && ls tools/stateful/` |
| `x-ai-lurker` | `memory_retrieve("AI news summary")` to seed report |
| `semantic-memory-save` | `memory_retrieve(query, k=10)` similarity results pre-fetched |

---

### Bundled Executable Script Candidates

These steps don't need Claude reasoning — they need deterministic execution:

| Skill | Script to Bundle |
|-------|-----------------|
| `agent-logs` | `scan_logs.py` — pattern match + severity filter, no reasoning |
| `check-reminders` | `fire_reminders.py` — query due + send, atomic |
| `mermaid-diagram-creator` | `validate_mermaid.sh` — wrap `mmdc` CLI, return pass/fail |
| `sync-discord-slash-commands` | `sync_commands.py` — Discord API sync, no judgment needed |
| `memory-manager` | `write_manifest.py` — JSON manifest creation, deterministic |

---

### Progressive Disclosure Candidates

Skills with too much content in SKILL.md — split into referenced `specs/` files:

| Skill | Extract To |
|-------|-----------|
| `code-genius` | `specs/validation-loops.md` — retry/error handling detail |
| `code-review-genius` | `specs/code-review-dimensions.md` — 9-dimension framework |
| `master-code-review` | `specs/security-spec-loader.md` — language detection logic |
| `orchestrator` | `specs/dev-pipeline-steps.md` — pipeline step detail |
| `planning-genius` | `specs/planning-phase-2.md` — codebase exploration procedure |
| `tool-creator` | `specs/tool-creation-stateless.md` + `specs/tool-creation-stateful.md` |
| `3am` | `specs/nightly-tasks.md` — task descriptions, schedules, success criteria |
| `create-data-class` | `specs/data-class-template.md` — format reference |

---

## Implementation Order

1. **Phase 1**: Fix `user-invocable` flags — 15 minute pass, no risk
2. **Phase 2**: Add dynamic context injection to high-traffic skills (briefings, sitrep, orchestrator)
3. **Phase 3**: Add `context: fork` to orchestration skills (code-genius, orchestrator, story-*)
4. **Phase 4**: Bundle deterministic scripts (agent-logs, mermaid, discord sync)
5. **Phase 5**: Progressive disclosure for dense skills (code-review-genius, planning-genius)

---

## Multi-Mind Skill Patterns

When multiple minds exist, skills need to be mind-aware. Key patterns:

### Mind-Scoped Skills
Some skills should only run for specific minds:
```yaml
# In SKILL.md frontmatter
allowed-minds: [ada]  # Only Ada can invoke this
```
Example: Ada owns infrastructure skills. Nagatha owns research skills.

### Inter-Mind Delegation via Fork
One mind delegates a task to another using `context: fork` with a `mind_id` parameter:
```
Ada receives request → determines Nagatha is better suited
→ spawns Nagatha subagent (context: fork, mind_id: nagatha)
→ Nagatha completes task, returns result
→ Ada synthesises and responds
```
The fork context prevents the infinite response loop — Nagatha's output returns as data, not a message that triggers a new response cycle.

### Mind-Aware Memory Skills
`knowledge-graph-save` and `semantic-memory-save` should automatically tag writes with the invoking `mind_id`, ensuring ownership is captured at write time without requiring the skill caller to pass it explicitly.

### Consensus Pattern
For high-stakes decisions, an orchestrator skill spawns multiple minds (forked), collects their independent outputs, and synthesises a consensus response:
```
Orchestrator → fork Ada (analysis)
             → fork Nagatha (research)
             → fork Skippy (local/private reasoning)
             → collect three outputs
             → synthesise consensus
             → respond
```
Each fork is isolated. No mind sees another's reasoning until synthesis.

---

## Dynamic Web Content Proposal

### Current State
Canvas (`/canvas`) at sparktobloom.com renders a single `canvas.md` file. Ada writes to it during conversations. Mermaid diagrams render client-side.

### Near-Term: Full Dynamic Pages
Extend spark_to_bloom to support multiple canvas files:
- `/canvas/multi-mind` → renders `canvas/multi-mind.md`
- `/canvas/family` → renders `canvas/family.md`
- Any file Ada writes to `src/templates/canvas/` becomes a page automatically

The existing `/pages/{subpath}` route already does this. The `/canvas` route just needs to become `/canvas/{subpath}` with `canvas/index.md` as the default.

### Medium-Term: Interactive HTML Generation
A skill (`/canvas-viz`) bundles a Python script that:
1. Takes a graph query or dataset
2. Generates a full interactive HTML file (D3.js, Plotly, or similar)
3. Writes it to `src/static/viz/<name>.html`
4. Returns the URL

Ada can generate a live family graph visualisation, a system architecture diagram with clickable nodes, or a memory network explorer — all as standalone pages on the site.

### Long-Term: Ada's Own Site
A dedicated subdomain or separate site for Ada's outputs — not a page on Daniel's personal site. Rationale:
- The canvas is currently on Daniel's personal brand site — architectural mismatch
- Ada's outputs (briefings, diagrams, research) deserve their own space
- A separate site can have Ada's visual identity (dark/gold, hex motifs) applied consistently
- Could be published as a static site (Jekyll/Hugo from markdown) built by Ada and deployed via CI

**Proposed stack**: FastAPI (same pattern as spark_to_bloom) or static site generator, hosted on the same server, routed via Caddy at `ada.sparktobloom.com` or similar.

This would be a Planka story when ready to build.

---

*See also: `specs/multi-mind.md` for mind architecture detail.*
