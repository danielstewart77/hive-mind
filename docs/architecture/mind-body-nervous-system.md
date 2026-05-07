# Hive Mind Architecture — Mind / Body / Nervous System

> Design reference for the three-part organism model that organizes the Hive Mind ecosystem.

---

## The organism metaphor

| Part | Definition | Examples |
|---|---|---|
| **Mind** | The LLM brain — the thing reasoning. | Ada, Bob, Bilby, Nagatha, Skippy |
| **Body** | Tools that reach the outside world. Carry prompt-injection risk. | hive-tools (gmail, calendar, linkedin, browser, playwright) |
| **Nervous system** | Internal state and inter-mind plumbing. No external surface. | Lucent (graph + vector store), inter-mind broker |

Browser and playwright belong to the body, not the nervous system — they reach external pages whose content can be adversarial.

---

## The body — `hive-tools`

| Property | Value |
|---|---|
| Location | `/home/USER/Storage/hive-tools` (outside `/Dev/` so minds cannot reach the source) |
| Auth | Bearer token, hashed in `data/hivetools.db` |
| Write protection | HITL approval gate via Telegram |
| Network | Joined to `hivemind` Docker network + published port `9421` for bare-metal callers |
| Caller list | All Docker minds + bare-metal Skippy (token in his keyring) |

**Sandbox:** minds in `/Dev/hive_mind/` have `HOST_DEV_DIR` mounted into their containers. Hive-tools lives outside `/Dev/`, so a compromised mind can't read or modify the source — only call the API. The API is the only contract.

### Privilege tiers — minds are *users*, Skippy is the *maintainer*

| Tier | Who | Can do | Cannot do |
|---|---|---|---|
| **User** | Any mind with a valid bearer token | Invoke existing endpoints (`POST /gmail/send`, `POST /browser/navigate`, etc.). Auth-gated and HITL-approved per route settings. | Add tools, modify routers, change HITL settings, edit source, rebuild. |
| **Maintainer** | Skippy alone, in his bare-metal admin context | Add tools, edit routers, update `tool_hitl_settings`, rebuild and recreate the container, manage the API token registry. | (Bounded only by Daniel's intent.) |

A compromised user can at worst spam HITL requests — annoying, recoverable. A compromised maintainer could quietly add an endpoint that bypasses HITL or exfiltrates data. Maintainer-tier operations require Skippy's bare-metal context, where the file system, build environment, and Docker socket all live behind the systemd boundary — `systemctl stop skippy` is the kill switch.

**Rule:** do not copy maintainer-tier skills (e.g. `add-hive-tool`) to minds for convenience. The boundary *is* the security model.

---

## The nervous system — Hive Mind organism

| Property | Value |
|---|---|
| Location | `/home/USER/Storage/Dev/hive_mind/nervous_system/` |
| Auth | None — internal only |
| Network | Bound to `hivemind` Docker network. No host port publication. |
| Kill switch | Stop the services. They're invisible to anything off-network. |

Two services in one folder:

```
nervous_system/
├── lucent_api/        # graph + vector memory
│   ├── server.py
│   └── routers/{graph.py, memory.py}
├── inter_mind_api/    # broker reads + sync delegate/forward
│   ├── server.py
│   └── routers/{messaging.py, state.py}
└── docker-compose.yml entries (lucent-api, inter-mind-api)
```

**Two services, not one combined:**

1. Bounded contexts genuinely differ. Lucent = "what does this organism *know*". Inter-mind = "how do brains *talk*".
2. Failure isolation: a broker bug shouldn't hang lucent queries.
3. Different change cadences. Lucent's schema is load-bearing; the broker iterates faster.
4. They share the network anyway — calling between them is one HTTP hop.

---

## The nervous system — Skippy organism

| Property | Value |
|---|---|
| Location | `/home/USER/Storage/hive_mind_skippy/nervous_system/` |
| Auth | None — internal only |
| Network | Bound to `127.0.0.1`. Not reachable off-host. |
| Kill switch | `systemctl stop skippy` — process and nervous system go dark together. |
| Body access | Via `HIVE_TOOLS_TOKEN` in his keyring. |

Each organism has its own nervous system. The Hive Mind container minds share one (Hive Mind's `lucent.db` + broker). Skippy has his own. **They share the body** (one gmail account, one calendar) — that's hive-tools.

---

## Auth model summary

| Component | Auth | Network |
|---|---|---|
| hive-tools (body) | Bearer token + HITL | `hivemind` net + host port 9421 |
| Hive Mind nervous system | None | `hivemind` net only, no host publication |
| Skippy nervous system | None | `127.0.0.1` only |
| Skippy → hive-tools | Token in keyring (`hive-mind / HIVE_TOOLS_TOKEN`) | host port 9421 |

---

## Key design decisions

1. **API key for the body, no API key for the nervous system.** Body reaches outside, needs auth. Nervous system never leaves the network or host, so auth is theatre — kill switch is the network/process boundary.
2. **One body, many nervous systems.** Body resources (gmail, calendar) are shared across all minds; brain state is per-organism.
3. **Browser belongs to body, not nervous system.** Even though browser sessions are stateful, the state is incidental — it's a body part, not a memory.
4. **Privilege tiers separate body usage from body extension.** Minds use; Skippy maintains. Maintainer skills never flow downstream.
