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
| Location | Its own repo, outside any tree mounted into mind containers (so minds cannot reach the source) |
| Auth | Bearer token, hashed in `data/hivetools.db` |
| Write protection | HITL approval gate via Telegram |
| Network | Joined to `hivemind` Docker network + published port `9421` for bare-metal callers |
| Caller list | All Docker minds + bare-metal Skippy (token in his keyring) |

**Sandbox:** minds in `/Dev/hive-mind/` have `HOST_DEV_DIR` mounted into their containers. Hive-tools lives outside `/Dev/`, so a compromised mind can't read or modify the source — only call the API. The API is the only contract.

### Privilege tiers — minds are *users*, Skippy is the *maintainer*

| Tier | Who | Can do | Cannot do |
|---|---|---|---|
| **User** | Any mind with a valid bearer token | Invoke existing endpoints (`POST /gmail/send`, `POST /browser/navigate`, etc.). Auth-gated and HITL-approved per route settings. | Add tools, modify routers, change HITL settings, edit source, rebuild. |
| **Maintainer** | Skippy alone, in his bare-metal admin context | Add tools, edit routers, update `tool_hitl_settings`, rebuild and recreate the container, manage the API token registry. | (Bounded only by Daniel's intent.) |

A compromised user can at worst spam HITL requests — annoying, recoverable. A compromised maintainer could quietly add an endpoint that bypasses HITL or exfiltrates data. Maintainer-tier operations require Skippy's bare-metal context, where the file system, build environment, and Docker socket all live behind the systemd boundary — `systemctl stop skippy` is the kill switch.

**Rule:** do not copy maintainer-tier skills (e.g. `add-hive-tool`) to minds for convenience. The boundary *is* the security model.

---

## The nervous system

| Property | Value |
|---|---|
| Location | [`nervous-system/`](../../nervous-system/) in this repo |
| Services | `hive-lucent` (graph + vector memory) and `hive-comms` (gateway: sessions, broker, HITL routing) |
| Auth | Bearer token per service (`LUCENT_BEARER_TOKEN`, `COMMS_BEARER_TOKEN`) on every route except `/health` |
| Network | `hivemind` Docker network + host ports `8425` (lucent) and `8426` (comms) so bare-metal and LAN minds can reach it |
| Kill switch | Stop the containers. |

**Two services, not one combined:**

1. Bounded contexts genuinely differ. Lucent = "what does this organism *know*". Comms = "how do brains *talk*" and "who is speaking to whom".
2. Failure isolation: a gateway bug shouldn't hang lucent queries.
3. Different change cadences. Lucent's schema is load-bearing; comms iterates faster.
4. They share the network anyway — calling between them is one HTTP hop.

**One nervous system, shared by every organism.** The container minds and the bare-metal super-minds (e.g. Skippy) all talk to the same lucent and the same comms — one database, many writers, provenance per write via `mind_id`. Super-minds joined the shared nervous system by design (2026-05-05 decision); per-organism nervous systems were retired.

---

## Auth model summary

| Component | Auth | Network |
|---|---|---|
| hive-tools (body) | Bearer token + HITL | `hivemind` net + host port 9421 |
| Nervous system (lucent, comms) | Bearer token per service | `hivemind` net + host ports 8425 / 8426 |
| Bare-metal mind → hive-tools | `HIVE_TOOLS_TOKEN` in its env | host port 9421 |

---

## Key design decisions

1. **Bearer tokens everywhere.** The body reaches outside; the nervous system is published on host ports so bare-metal and LAN minds can reach it — both carry auth, and the kill switch is still the network/process boundary.
2. **One body, one nervous system, many minds.** Body resources (gmail, calendar) and brain state (lucent, sessions, broker) are shared; identity is carried per write by `mind_id`.
3. **Browser belongs to body, not nervous system.** Even though browser sessions are stateful, the state is incidental — it's a body part, not a memory.
4. **Privilege tiers separate body usage from body extension.** Minds use; an operator mind maintains. Maintainer skills never flow downstream.
