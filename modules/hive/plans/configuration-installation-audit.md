# Configuration and Installation Audit

## Scope and evidence

The audit covered every rendered Hive console page, every visible form and
button, every application API route, the local Hive Mind and Hive Edge Minds
configuration formats, the Hive Comms registration API, the Hive Tools
approval API, the live Ollama service, and the live Pi-hole service.

Live service findings:

- Ollama is reachable at `http://192.168.4.64:11434`, reports version 0.30.8,
  and supports model inventory, running-model inventory, pull, and delete.
- Pi-hole 6 is reachable at `http://192.168.4.16:8080`. Its API is healthy,
  but administrative endpoints require a Pi-hole session ID. Its own OpenAPI
  document is served at `/api/docs/specs/main.yaml`.
- Hive Comms can register and update minds through `/broker/minds` and
  `/broker/minds/{name}`.
- Hive Tools can restart local Compose services through an approval-gated
  request. It cannot install software on a different machine.
- The Edge Mind runtime exposes an unauthenticated `/health` response with its
  `mind_id`, which is sufficient to prove that the intended runtime is present
  before broker registration.

Reference material:

- Ollama API: <https://docs.ollama.com/api/introduction>
- Pi-hole 6 API: <https://docs.pi-hole.net/api/>
- Local Pi-hole OpenAPI: `http://192.168.4.16:8080/api/docs/`
- Edge Mind installer: `/mnt/dev/hive-edge-minds/setup.sh`
- Hive Comms API: `http://hive-comms:8424/openapi.json`

## Controls completed by this change

| Area | Previous state | Completed behavior |
|---|---|---|
| Capability Node endpoint | Ollama and Pi-hole required container environment edits and a recreation | The owner can save, test, change, or clear either endpoint in the persistent Hive database without restarting the site |
| Ollama | Health badge only | The owner can see the server version, installed models, running models, pull a model, and delete a model |
| Pi-hole | Unconfigured card with no explanation | The owner can connect the live API; a 401 response is correctly reported as reachable but awaiting administrative authentication rather than as offline |
| Local Mind model | Raw configuration was read-only | The owner can change `default_model`; Hive preserves the rest of `runtime.yaml`, updates broker metadata, and exposes an approval-gated runtime restart |
| Core Mind setup affordance | “Create mind” led to a name check and terminal link that did not install anything | The UI now states that Core provisioning is not automated, explains that name checks reserve nothing, and keeps the working Edge Mind path primary |
| Edge Mind identity | The deployment command minted an unknown ID on the target | The Hive now mints the Mind ID first and stamps it into the target environment, so installation and registration refer to one identity |
| Edge Mind network wiring | The generated plan omitted reachable Hive endpoints | The generated environment includes the configured LAN endpoints for Hive Comms, Lucent, and Hive Tools |
| Edge Mind container deployment | Generated containers used core-only hostnames, required a pre-existing `hivemind` Docker network, and did not publish the Mind API | The companion Edge installer now consumes the generated environment, publishes the selected port, and runs on an ordinary remote Docker host |
| Edge Mind registration | The page stopped after showing shell commands | The owner can enter the running target URL; the Hive verifies the reported Mind ID, registers the runtime with Hive Comms, and persists its profile, role, and deployment type |

## Already complete before this audit

| Area | Evidence |
|---|---|
| Setup readiness | Uses live Hive Comms, Lucent, session, and Capability Node data |
| Name availability | Checks the live broker registry |
| Session management | Lists and deletes Hive Comms sessions |
| Voice reference clips | Validates and writes WAV references for registered minds |
| Installer downloads | Serves allowlisted Hive Init assets |
| Rules | Lists, creates, edits, and deletes standing and contextual rules |
| Canvas | Saves owner submissions and supports live collaboration |

## Remaining backlog

### Mind installation and management

| Priority | Missing end-user behavior | Why it is not safely wireable in the Hive site alone | Required work |
|---|---|---|---|
| 1 | One-click remote Edge Mind installation | A machine that does not yet run Hive software has no authenticated control channel. The old Remote Admin service is an SSH bridge, accepts unknown host keys, stores sessions only in memory, and is not deployed as a trusted node agent | Build a small paired node agent with one-time enrollment, host-key pinning, scoped operations, progress events, rollback, and Hive Tools approval integration |
| 1 | Remote Edge Mind configuration, restart, update, logs, and uninstall | The Hive can reach the Mind runtime API, but that API manages sessions only and intentionally cannot mutate its host | Add a versioned node-management API to the paired node agent and surface its capabilities on each Edge Mind page |
| 1 | Install the Sentinel Edge profile | The previous wizard promised telemetry collectors, Loki, Grafana, and a dashboard but generated only a repository clone. The option is now hidden rather than pretending the bundle is installable | Publish a versioned Sentinel bundle manifest with supported collectors, storage, dashboard assets, readiness checks, rollback, and platform-specific installers |
| 1 | Create a new Core Mind from the setup page | Core creation spans `runtime.yaml`, the shared harness implementation, Compose includes, secrets scopes, bot credentials, voice selection, first identity seed, build, start, and broker registration | Define a Core Mind manifest and an idempotent host-side installer owned by Hive Tools; make the site submit and monitor that plan |
| 2 | Change a Mind's provider or harness | Provider changes also require provider-specific environment, proxy routing, credentials, resume policy, and sometimes a different implementation or image | Define validated runtime schemas and provider transition plans, then apply them transactionally with health verification and rollback |
| 2 | Configure conversation surfaces and allowed users | Telegram and Discord changes require secret tokens, allowlists, bot processes, and restarts | Add secret collection through a write-only secret broker API, surface-specific validation, and coordinated restart plans |
| 2 | Configure Mind secret scopes | Hive Comms can grant scopes, but the UI does not expose the secret inventory or explain each capability's blast radius | Add a permission editor with capability descriptions, diff preview, audit log, and explicit owner confirmation |
| 3 | Edit identity, soul, voice assignment, and backstory together | Identity lives in Lucent, runtime lives in YAML, and voice assets live on the core host | Add a Mind profile workflow that coordinates the three stores without treating deployment profile as personality |

### Capability Nodes

| Priority | Missing end-user behavior | Constraint | Required work |
|---|---|---|---|
| 1 | Persist Pi-hole administrative authentication | Pi-hole 6 requires a password exchange for a temporary session ID. The Hive database is not a secret store, and Hive Comms currently exposes secret reads and scopes but no write-only owner API | Add a write-only secret storage endpoint with audit and approval, then implement Pi-hole session creation, summary, DNS blocking control, lists, and configuration adapters |
| 1 | Install Pi-hole on a remote host | Hive Tools controls Docker only on its own host | Use the paired node agent, with an installation plan that detects ports and volumes before creating the container |
| 2 | Install or update Ollama on another host | Ollama's API manages models, not its own installation | Use the paired node agent and official platform-specific installers; keep model management in the current Ollama adapter |
| 2 | Assign Ollama models to Minds | Model presence and Mind runtime configuration are separate operations | Add a guided workflow that pulls the model first, validates harness compatibility, updates the Mind, restarts it, and verifies a test session |
| 2 | Install, restart, or update Hive Tools | The site can inspect Hive Tools and use its API, but Hive Tools cannot safely replace itself | Add an external supervisor or host service with a narrowly scoped self-update operation and rollback |
| 3 | Install or configure Chatterbox and Kokoro | The current controls cover service health and reference clips, not engine lifecycle or voice assignment | Add voice service adapters, synthesis preview, per-Mind engine and voice selection, and approval-gated restart controls |

### Hive and owner settings

| Priority | Missing end-user behavior | Required work |
|---|---|---|
| 2 | Change password, add an owner, disable an account, or inspect login history | Add account CRUD, password confirmation, session invalidation, rate limiting, and an authentication audit table |
| 2 | Configure Hive Comms and Lucent endpoints from the browser | Add validated persistent connection settings and a controlled process reload; bearer tokens must use the secret store |
| 2 | Finish Hive Init as an end-to-end installer | Hive Init currently writes a manifest, but no committed harness consumes that manifest to build and start the complete stack |
| 3 | Voice clip preview, replacement history, and deletion | Add audio metadata validation, preview synthesis, versioning, and explicit removal |

## Recommended sequence

The next architectural step should be the paired node agent. It unlocks the
largest group of unfinished controls without giving the Hive site arbitrary
SSH or Docker access. After that, add a write-only secret storage API, then a
transactional Mind manifest that can install and reconfigure both Core and
Edge Minds. Capability-specific adapters can build on those two foundations.
