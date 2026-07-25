# Hive Site Redesign

## Product Direction

Hive should feel like a calm control center for a personal AI system, not a collection of internal diagnostics. A new owner should be able to clone Hive, understand what is running, create the first mind, connect optional services, and add another machine without learning the repository layout first.

The interface has two modes:

1. First run guides a new clone from an empty installation to a working Hive.
2. Steady state provides fast operational visibility and management without hiding advanced diagnostics.

The design borrows proven interaction patterns from GitHub's scoped settings, Vercel's deployment flows, Home Assistant's integration setup, and Portainer's environment management. It should retain Hive's identity without retaining the current terminal aesthetic as the organizing principle.

## Current-State Findings

The present site exposes useful capabilities, but its information architecture reflects implementation history rather than user intent.

- Graph is the default route even though it is an inspection tool, not the natural home screen.
- The top navigation gives graph, memory, rules, canvas, settings, and terminal equal weight.
- Settings combines mind inventory, active sessions, Hive Tools installation, voice files, mind creation, downloads, and federation.
- Several settings tabs describe placeholders and future work directly in the product.
- Minds are rendered as raw configuration dumps instead of manageable entities.
- Hive Tools is treated as a repository path and container health check rather than an optional capability service.
- There is no first-run readiness check or guided path from fresh clone to useful system.
- Outposts have no first-class presence even though they are the natural way to add a mind on another machine.
- Operational status is fragmented across individual pages and raw endpoint responses.
- The all-monospace, low-contrast, terminal-box presentation makes routine administration feel like debugging.

## Information Architecture

Use a persistent left navigation on desktop and a compact drawer on mobile. The primary sections are organized by owner intent.

### Overview

The default route. Shows system health, minds, recent activity, pending decisions, and setup tasks. A fresh clone sees a focused setup checklist instead of an empty dashboard.

### Minds

The people-like entities in the Hive. This section owns mind identity, status, model, surfaces, voice, permissions, rules, and recent sessions. Creating a mind starts here, but personality and backstory remain a guided conversation rather than an inferred form value.

### Outposts

Machines running one connected mind. This is a first-class fleet view, separate from settings. It owns installation, registration, health, updates, machine role, deployment type, and removal.

### Tools

Optional capabilities available to minds. This section owns Hive Tools installation, tool availability, credentials, permissions, health, and testing. The user sees capabilities such as Gmail, calendar, browser, Docker, and notifications rather than repository internals.

### Knowledge

A grouped section containing Graph, Memory, and Rules. These are different views of the same knowledge system and should be adjacent without occupying three top-level destinations.

### Workspace

Contains Canvas and Terminal. Both are working surfaces rather than system configuration.

### Activity

A unified event stream for mind registration, session changes, tool calls requiring approval, Outpost health, updates, rule changes, and system errors. Filters support mind, source, severity, and time.

### Settings

Durable system and owner configuration only. Settings should not be the home for operational products merely because they need configuration.

## Navigation Model

The desktop shell uses a 232-pixel left rail with the Hive mark, installation name, primary navigation, and a bottom cluster for system status, help, and the owner menu. The content area has a compact page header with title, context, search, and page-specific actions.

The rail may collapse to icons on medium screens. On mobile it becomes a drawer, while the page header retains the current location and primary action. Navigation state is preserved across visits.

Suggested order:

- Overview
- Minds
- Outposts
- Tools
- Activity
- Knowledge: Graph, Memory, Rules
- Workspace: Canvas, Terminal
- Settings

Do not expose repository names, mount paths, bearer tokens, or container hostnames in the normal interface. Put those details behind an Advanced or Diagnostics disclosure.

## First-Run Experience

The first authenticated owner lands on Setup, a full-width workflow with visible progress and resumable steps.

### Step 1: Welcome and Installation Identity

Name the Hive, confirm the owner profile, choose timezone, and confirm the public or private access posture. Explain in one sentence that the name labels this installation and does not alter mind identity.

### Step 2: Readiness Check

Run automatic checks for Hive services, database access, signing secret, network reachability, and required bearer credentials. Present each result as Ready, Needs attention, or Optional. Every failure includes a specific repair action and a retry control.

### Step 3: Provider Connection

Connect at least one supported AI provider or confirm a locally available provider. Secret values use masked fields, explicit save behavior, and a test connection action. Model selection follows provider connection rather than appearing as an unexplained text field.

### Step 4: First Mind

Choose a name and operating location, then begin the required identity conversation. The system can collect role, harness, model, and surfaces structurally, but it must ask the owner to define personality, voice, and backstory. A clear review screen shows exactly what will be created before launch.

### Step 5: Conversation Surface

Offer Telegram, browser terminal, Discord, and supported voice options as integrations. Each choice opens only the fields it needs and provides a connection test.

### Step 6: Optional Capabilities

Offer Hive Tools as an optional capability service. Explain the benefit in user terms and defer credentials for individual tools until the owner enables them.

### Step 7: Finish

Show the first mind's status, a direct way to start a conversation, and the next useful actions: add an Outpost, enable a tool, inspect memory, or invite another owner if supported later.

The setup checklist remains on Overview until all required steps pass. Optional steps can be dismissed and restored from Settings.

## Overview Screen

The home screen answers four questions in the first viewport: Is Hive healthy, who is available, what needs attention, and what happened recently?

The header shows the installation name, overall state, last refresh time, and a global Create menu for Mind, Outpost, and Rule.

The main layout contains:

- Attention queue for failed services, unavailable minds, pending approvals, and incomplete setup.
- Mind roster with avatar, name, status, current model, location, and last activity.
- System services strip for Comms, Lucent, Hive Tools, voice, and external providers.
- Recent activity feed with concise human-readable events.
- Quick actions for starting a conversation, adding an Outpost, and opening the terminal.

Healthy services remain compact. Problems expand with cause and corrective action. The dashboard should not reward health with a wall of green boxes.

## Minds

### Mind List

Use a compact table on desktop and stacked rows on mobile. Columns are status, name, role, location, harness and model, surfaces, and last active. Search and filters cover status, location, and role.

The primary action is Create mind. A row opens the mind detail view; raw identifiers stay secondary.

### Mind Detail

The header shows avatar, name, current state, role, host location, and primary actions such as Start conversation and Restart. Tabs are Overview, Identity, Runtime, Surfaces, Voice, Rules, Sessions, and Diagnostics.

Overview shows current model, provider, Outpost or local host, service reachability, last activity, and recent errors. Identity contains the owner-authored soul and backstory with edit history. Runtime provides structured controls for harness, model, limits, and safe restart. Diagnostics contains raw configuration and identifiers.

Remote and local minds share the same interface. Unsupported remote actions are disabled with a clear reason rather than represented as missing configuration files.

## Outposts

Outposts are installations on other machines that run one connected mind. The user manages them as machines, not Git repositories.

### Outpost List

Each row shows machine name, assigned mind, role, operating system, deployment type, connection state, version, and last contact. Filters cover online state, role, and update availability.

The empty state explains that an Outpost adds one mind on another machine and offers Add Outpost as the sole primary action.

### Add Outpost Flow

The flow should support Linux service, container, and Windows task deployments.

1. Name the machine and select operating system.
2. Choose Operator or Satellite with a direct permissions explanation. Operator receives host access; Satellite receives conversation and memory access without machine control.
3. Select an existing mind or begin the create-mind identity conversation.
4. Choose harness, provider, model, and conversation surfaces.
5. Generate a short-lived, single-use registration token and a platform-specific installer.
6. Show one primary installation command or downloadable package, plus an expandable manual method.
7. Wait for the Outpost to register and run automatic connectivity checks.
8. Review capabilities and finish.

The web application should not silently connect to or modify an arbitrary remote machine. The owner performs the bootstrap action on that machine; Hive then claims the registration through the one-time token.

### Outpost Detail

The detail view shows connection health, assigned mind, machine facts, version, deployment method, enabled surfaces, and recent events. Actions include Test connection, Rotate registration, Update, Restart, Reassign mind, and Remove.

Update and removal require a review dialog that states the affected machine and mind. Removal revokes Hive credentials first and provides explicit cleanup instructions for the remote machine.

## Hive Tools

Hive Tools should be presented as an optional capability service. Installation state and individual tool configuration are separate concepts.

### Tools Landing Screen

If the service is absent, show what it enables, its security boundary, prerequisites, and an Install Hive Tools action. Installation performs a readiness check, confirms the target host, records the source and version, starts the service, and verifies health.

If the service is present, show a catalog of capabilities. Each row has an icon, plain-language name, enabled state, credential state, allowed minds, last use, and health. Categories include Communication, Productivity, Web, Host Operations, and Local AI.

### Tool Detail

Each tool has Overview, Access, Credentials, Activity, and Diagnostics sections. Access uses an explicit allowlist of minds. Credentials explain where secrets are stored and never display secret values after saving. Test runs show the exact operation and result without granting broader access.

Global actions include Check for updates, Restart service, Export configuration, and Uninstall. Uninstall previews affected minds and capabilities before confirmation.

## Settings

Settings use a two-column layout: a narrow section list and a focused form area. Each page has one clear scope and sticky Save and Discard actions only when values have changed.

### General

Installation name, timezone, locale, date and time format, default landing page, and appearance.

### Owner and Access

Owner profile, password, active sessions, authentication policy, trusted origins, and future additional users. Session deletion belongs here, not under Hive Comms.

### Providers and Models

Provider connections, API credential status, available models, defaults, connection tests, and cost or usage limits. Per-mind overrides link back to the relevant mind.

### Connections

Hive Comms, Lucent, voice servers, public URLs, network endpoints, and federation when implemented. Each connection has status, test, and advanced details.

### Notifications

Owner notification channels, severity thresholds, quiet hours, and delivery tests.

### Security

Secret rotation, registration-token policy, audit retention, data exposure controls, and destructive-action confirmations.

### Data and Storage

Database locations, backup status, export, restore, retention, and storage usage. Raw paths are visible only here.

### Updates

Installed versions, update channel, available updates, release notes, and controlled update actions for Hive and optional services.

### Advanced

Raw configuration, feature flags, diagnostics bundle, downloads, and developer information. Unimplemented features do not appear as tabs; they remain outside the production navigation until usable.

## Visual System

Retain a dark-first technical identity, but make it quieter and more legible.

- Use a neutral charcoal background, near-black rail, and slightly lighter work surfaces.
- Keep cyan as the interactive accent, gold for attention and selected knowledge concepts, green for healthy state, and red only for actionable failure or destructive operations.
- Use a modern sans-serif interface font for navigation, forms, and prose. Reserve monospace for identifiers, logs, configuration, and terminal content.
- Use full-width page regions with restrained dividers. Cards are limited to repeated entities and genuinely grouped status summaries.
- Use eight-pixel or smaller radii, subtle borders, and no glow on routine text.
- Replace text-heavy buttons with familiar icons where the action is unambiguous, while preserving labels for primary commands.
- Use lucide icons consistently for navigation, status, actions, tools, and platforms.
- Keep page titles compact. This is an operational application, not a marketing site.
- Use plain sentence case throughout. Internal service names remain correctly capitalized where they are product names.

The interface should feel closer to a well-made infrastructure console than a themed terminal. Hive's personality comes from names, language, and deliberate color accents rather than visual noise.

## Interaction Standards

- Every asynchronous action has pending, success, empty, and failure states.
- Status labels pair color with text or icon; color is never the only signal.
- Destructive actions state the target and consequence, then require explicit confirmation.
- Forms validate inline and preserve entered values after recoverable failures.
- Secrets are masked, can be replaced but not revealed, and show last-updated metadata.
- Health is refreshed automatically with a visible timestamp and manual retry.
- Tables support keyboard navigation, responsive overflow, search, and useful empty states.
- Deep links preserve the selected section and filter state.
- Raw JSON and configuration are always secondary disclosures.
- Product copy explains outcomes and decisions, not implementation history or future placeholders.

## Responsive Behavior

Desktop uses the left rail and content area. Tablet collapses the rail while preserving labels in tooltips. Mobile uses a drawer and converts dense tables into labeled rows.

Setup flows become one column on mobile with a sticky Continue action. Detail pages use a horizontally scrollable tab row or a section menu. Graph, memory, canvas, and terminal retain specialized full-screen modes with direct return navigation.

All fixed controls use stable dimensions. Long names, models, URLs, and machine identifiers wrap or truncate with a reveal action without resizing the surrounding layout.

## Route Model

Suggested user-facing routes:

- `/overview`
- `/setup`
- `/minds`
- `/minds/{mind_id}`
- `/outposts`
- `/outposts/new`
- `/outposts/{outpost_id}`
- `/tools`
- `/tools/{tool_id}`
- `/activity`
- `/knowledge/graph`
- `/knowledge/memory`
- `/knowledge/rules`
- `/workspace/canvas`
- `/workspace/terminal`
- `/settings/general`
- `/settings/access`
- `/settings/providers`
- `/settings/connections`
- `/settings/notifications`
- `/settings/security`
- `/settings/data`
- `/settings/updates`
- `/settings/advanced`

Existing routes can redirect during migration so saved links continue to work.

## Delivery Sequence

### Phase 1: Application Shell and Overview

Build the left navigation, responsive shell, shared page headers, component tokens, Overview screen, global status model, and redirects. Remove product placeholders from visible navigation.

### Phase 2: Settings Foundation

Replace the current settings tab strip with scoped routes and structured forms. Move sessions to Access, connections to Connections, downloads to Advanced, and provider configuration into Providers and Models.

### Phase 3: Minds

Create the mind list and detail experience over the existing broker and local configuration data. Add structured status and diagnostics before exposing mutation controls.

### Phase 4: Outposts

Define the registration API, single-use token model, installer generation, registration heartbeat, Outpost list, add flow, and detail view. Integrate the existing Hive Outpost project as the machine-side runtime.

### Phase 5: Hive Tools

Define install and lifecycle APIs, then build the service setup flow, capability catalog, per-mind access control, credential state, health checks, and activity history.

### Phase 6: First Run

Compose existing readiness, provider, mind, surface, tools, and Outpost flows into the resumable setup experience. Ensure a clone with missing optional services remains understandable and usable.

### Phase 7: Knowledge and Workspace Polish

Place Graph, Memory, Rules, Canvas, and Terminal inside the new shell while retaining the full-screen space their specialized interfaces require. Normalize typography, controls, empty states, and mobile behavior.

## Success Criteria

A person unfamiliar with the repositories can clone Hive and reach a working first conversation without reading source code. They can tell what is healthy, understand the difference between a mind and an Outpost, add another machine through a guided bootstrap, install Hive Tools as an optional capability service, control which minds may use each tool, and find durable configuration in Settings without encountering implementation placeholders.
