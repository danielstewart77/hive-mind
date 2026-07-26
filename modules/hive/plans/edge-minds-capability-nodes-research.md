# Edge Minds and Capability Nodes

## Product taxonomy

The Hive site separates identity-bearing cognition from external services:

| Area | Meaning | Examples |
|---|---|---|
| Core Minds | Minds hosted by the Hive's primary installation | Ada, Bob, Bilby, Nagatha |
| Edge Minds | Minds belonging to the Hive but running outside its core installation | operator workstation, family machine, Sentinel |
| Capability Nodes | External services that supply controlled powers without becoming minds | Hive Tools, Ollama, Chatterbox, Kokoro, Pi-hole |
| Core Services | The shared nervous system required for the Hive to operate | Hive Comms, Lucent |

An Edge Mind has a deployment profile. The standard profile is one generic
mind runtime. The Sentinel profile adds an explicit bundle: the Edge Mind
runtime, `hive-sentinel`, telemetry collectors, Loki and Grafana, and the
Sentinel dashboard. Identity and personality remain separate from profile.

## Interface research

### OpenClaw

Sources:

- <https://docs.openclaw.ai/web/control-ui>
- <https://docs.openclaw.ai/cli/nodes>
- <https://docs.openclaw.ai/web/dashboards>
- <https://github.com/openclaw/openclaw>

Patterns adopted:

- Treat remote runtimes as first-class nodes with pairing, scopes, status,
  and diagnostics rather than as repositories.
- Keep liveness and readiness distinct and expose repair guidance.
- Separate installed capability inventory from discovery and configuration.
- Make privileged changes operator-scoped and approval-aware.
- Present machine health, active work, and recent sessions on the overview.

### Hermes Agent

Sources:

- Local clone at `/mnt/dev/hermes-agent`
- `website/docs/user-guide/features/web-dashboard.md`
- Dashboard screenshots under `website/static/img/dashboard/`

Patterns adopted:

- Use a machine-level management surface rather than one dashboard per mind.
- Keep a persistent sidebar with clear operational domains.
- Summarize configured, active, and unhealthy resources before listing them.
- Provide a catalog-style service view with expandable details and controls.
- Keep health, repair operations, sessions, providers, and integrations
  accessible without exposing raw configuration first.

### Open WebUI and Portainer

Sources:

- <https://github.com/open-webui/open-webui>
- <https://github.com/portainer/portainer>

Patterns adopted:

- Group model providers and tool servers as integrations with explicit access
  control, not as properties of an individual mind.
- Use an environment inventory that shows placement, type, state, and purpose.
- Keep expandable operational details behind a concise fleet overview.

## Resulting site structure

The primary navigation is organized around Overview, Minds, Activity,
Topology, Knowledge, and Workspace. Topology contains Edge Minds and
Capability Nodes. Edge Mind deployment begins with a profile choice and then
collects role, runtime, deployment, and surfaces. Capability Nodes display
kind, health, endpoint, access posture, intended consumers, and repair action.
