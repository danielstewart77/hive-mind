# Harness-Native Operations

## Principle

The Claude Code harness is a composable runtime environment, not just a code editor. It can execute shell commands, make HTTP requests, query databases, call MCP tools, parse and transform data, manage processes, automate browsers, search the web, and orchestrate multi-step workflows — all without writing a single line of application code.

**Only write code when the harness cannot accomplish the task.** If the harness can do it, let the harness do it.

## Harness Tool Inventory

### File Operations
| Tool | What it does |
|---|---|
| **Read** | Read files (text, images, PDFs, notebooks). Supports offset/limit for large files, page ranges for PDFs. |
| **Write** | Create or overwrite files. |
| **Edit** | Targeted string replacement in files. Supports `replace_all` for bulk renames. |
| **Glob** | Find files by pattern (e.g. `**/*.md`, `minds/*/MIND.md`). |
| **Grep** | Regex search across files. Output modes: content, file paths, counts. Context lines, type filtering. |
| **NotebookEdit** | Modify Jupyter notebook cells. |

### Shell Execution
| Tool | What it does |
|---|---|
| **Bash** | Execute any shell command. Access to curl, jq, yq, sqlite3, docker, git, psql, python3, and every CLI tool on the system. Background execution supported. |

Bash alone covers: HTTP requests, database queries, data transformation, process management, container orchestration, file manipulation, API calls, and anything with a CLI.

### Web
| Tool | What it does |
|---|---|
| **WebFetch** | Fetch and parse content from URLs. Extract specific information via prompt. |
| **WebSearch** | Search the web with domain filtering. |
| **Chrome** | Full browser automation — navigate, fill forms, click, extract from JS-rendered sites. |

### Agent & Delegation
| Tool | What it does |
|---|---|
| **Agent** | Spawn a subagent with its own context window. Types: Explore (read-only research), Plan (strategy), general-purpose (full access), and custom agents defined in `.claude/agents/`. |
| **SendMessage** | Continue a previously spawned agent with preserved context. |
| **TeamCreate** | Multi-agent coordination (experimental). |

### Task & Scheduling
| Tool | What it does |
|---|---|
| **TaskCreate/TaskGet/TaskList/TaskUpdate/TaskStop** | Background task management. |
| **CronCreate/CronList/CronDelete** | Schedule recurring or one-shot prompts within a session. |

### Planning
| Tool | What it does |
|---|---|
| **EnterPlanMode / ExitPlanMode** | Read-only design mode with approval gate before execution. |
| **EnterWorktree / ExitWorktree** | Isolated git worktree for parallel work. |

### MCP Integration
| Tool | What it does |
|---|---|
| **MCP tools** | Any tool exposed by connected MCP servers — appears alongside built-in tools. |
| **ToolSearch** | Discover and load deferred MCP tools on demand. |
| **ListMcpResourcesTool / ReadMcpResourceTool** | Access MCP server resources. |

### Skills
| Tool | What it does |
|---|---|
| **Skill** | Execute a reusable workflow defined in markdown. Skills can use all harness tools, accept arguments, fork into subagents, and preload other skills. |

### User Interaction
| Tool | What it does |
|---|---|
| **AskUserQuestion** | Ask the user a question with structured options. |

## What the Harness Can Accomplish

Because the harness has Bash, it has access to every CLI tool on the system. Combined with the other native tools, this means:

| Capability | How |
|---|---|
| Make HTTP requests | `curl`, `httpx`, `wget` |
| Query/modify SQL databases | `sqlite3`, `psql`, `mysql` |
| Query/modify Neo4j | MCP tools or `cypher-shell` |
| Parse JSON/YAML/XML | `jq`, `yq`, `python3 -c`, `xmllint` |
| Transform data between formats | Pipe chains, `jq`, `python3 -c` |
| Manage Docker containers | `docker`, `docker compose` |
| Manage system services | `systemctl` |
| Git operations | `git` |
| GitHub operations | `gh` (PRs, issues, checks, releases) |
| Process management | `ps`, `kill`, `top`, `htop` |
| Network diagnostics | `curl`, `ping`, `ss`, `netstat` |
| File archiving | `tar`, `zip`, `rsync` |
| Text processing | `sed`, `awk`, `sort`, `uniq`, `wc` |
| Cron/scheduling | `crontab`, CronCreate |
| Send notifications | MCP tools, `curl` to Telegram/Slack/email APIs |
| Browser automation | Chrome tool, or Bash with headless browsers |
| Search the web | WebSearch, WebFetch |
| Read/write any file format | Read + Write tools, or Bash with appropriate CLI |
| Run Python one-liners | `python3 -c "..."` |
| Orchestrate multi-step workflows | Skills |
| Delegate parallel work | Agent tool with multiple subagents |

## When to Write Code

Write code **only** when the task requires something the harness fundamentally cannot provide:

| Write code when... | Example | Why the harness can't do it |
|---|---|---|
| A process must run continuously | FastAPI server, WebSocket listener, background task loop | The harness runs on-demand, not as a daemon |
| State must persist in memory across requests | In-memory registry consulted on every API call | The harness doesn't live between requests |
| Real-time event handling is needed | SSE stream consumption, filesystem watcher | Requires a long-running event loop |
| Specialized computation is required | ML inference, audio synthesis, video generation | No CLI equivalent with acceptable performance |
| A library has no CLI equivalent | Claude Code SDK, Playwright driver, Neo4j Bolt driver | Must be called programmatically |
| Performance requires optimized code | Tight loops over large datasets, custom data structures | Shell tools too slow |

## When NOT to Write Code

| Don't code this... | The harness does it with... |
|---|---|
| "List all registered minds" | `curl GET /broker/minds \| jq` or a skill |
| "Create a MIND.md file" | Read template + Write tool |
| "Register a mind with the broker" | `curl POST /broker/minds -d '{...}'` |
| "Check if a service is healthy" | `curl GET /health` or `docker ps` |
| "Parse YAML and extract a field" | `yq '.field' file.yaml` |
| "Delete old config files" | `rm` via Bash |
| "Query SQLite for a value" | `sqlite3 data/broker.db "SELECT ..."` |
| "Send a notification" | MCP tool or `curl` to Telegram API |
| "Search code for a pattern" | Grep tool |
| "Run a test suite" | `pytest` via Bash |
| "Build and restart containers" | `docker compose up -d --build` |
| "Create a PR" | `gh pr create` |
| "Validate JSON schema" | `python3 -c "import json; ..."` or `jq` |
| "Poll an API until a condition is met" | Bash loop or a skill with retry logic |

## Skills Are Harness Programs

A skill (`.claude/skills/<name>/SKILL.md`) is a program written for the harness. It describes steps the harness executes using its native tools. Skills can accomplish anything the harness can — which is nearly everything.

When designing a feature, ask: **can this be a skill?** If the entire workflow can be expressed as "read this, call that API, write this file, verify that response" — it's a skill, not application code.

Skills can also:
- Accept arguments (`$ARGUMENTS`, `$1`, `$2`)
- Execute shell commands inline (`` !`command` ``)
- Fork into isolated subagents (`context: fork`)
- Restrict tool access (`allowed-tools`)
- Preload other skills
- Run on a specific model

## Impact on Implementation Planning

When writing an `IMPLEMENTATION.md`:

1. For each step, apply the decision rule: **can the harness do this?**
2. If yes → harness-native operation. Write the instructions, not the code.
3. If no → write code and tests.
4. Do not write tests for harness-native operations. The harness executes and verifies in the moment. Persisting tests that assert file existence or API response shape after the operation is a state test with no ongoing value.

Mark harness-native steps clearly:

```markdown
### Step N: Register Minds with Broker

**Harness-native operation — no application code needed.**

- [ ] `curl -X POST http://localhost:8420/broker/minds -d '{"name": "...", ...}'`
- [ ] Verify: `curl http://localhost:8420/broker/minds | jq`
```

## The Boundary

The boundary is not "development time vs. runtime." The harness operates at both. The boundary is:

**Can the harness do this with its tools?**
- Yes → harness-native. No code.
- No → write code. Test it.

The harness is the primary execution environment. Code exists only where the harness cannot reach.
