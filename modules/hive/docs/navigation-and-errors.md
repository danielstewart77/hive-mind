# Feature visibility and Hive Errors

Implemented on `feature/navigation-and-hive-errors`, with component feeds on
`hive-mind` branch `feature/hive-error-feeds`.

Settings → Features lists every sidebar destination except Settings, including
Kanban's subpages, Setup guide, Terminal, and Hive Errors. Save writes the
signed-in account's choices to the existing `core.settings` table. All features
default to visible. Settings remains accessible even when everything is hidden.
Visibility does not change authorization or disable a service.

Hive Errors is an admin page. It polls every five seconds after the preceding
request finishes. Leaving the page aborts its request and cancels its timer;
hidden browser tabs pause polling. No diagnostic polling runs on other pages.

The site, registered minds, Comms, and Lucent provide recent error feeds.
Configured voice, terminal, and tools endpoints receive health checks, and the
database receives a read-only query. Those health checks do not expose server
logs. An unreachable component, refused credential, unavailable feed, or failed
mind registry is shown as incomplete diagnostics, never as an empty healthy feed.

Each feed retains at most 100 ERROR/CRITICAL records from the last hour in its
process. It includes timestamps, messages, and exception reasons. Restarting
clears the feed. Credentials are redacted; stack traces are not returned.

## Agreed protections

Source: Daniel Stewart's requests dated 2026-11-13 in this conversation.
Protection list version 1, explicitly accepted by the user: “yes to both of your
earlier questsions”.

- P1: Saved feature choices hide navigation after reload and keep Settings
  accessible. Route tests execute the real account, database, and template paths;
  account-isolation checks cover members as well as admins.
- P2: Component failures, including a missing model context window, reach Hive
  Errors, and polling stops when the page closes. Python tests execute logging,
  the diagnostics collector, and HTTP handlers. Node tests import the production
  poller and verify abort, late-response handling, and sequential polling.

## Validation

- Hive: 75 tests passed across feature/error routes, existing settings routes,
  and access middleware tests, using a disposable pgvector/PostgreSQL container.
- Browser: signed in to an isolated site; saved unchecked Graph and Kanban,
  verified navigation removal and persistence after reload, verified Settings,
  and rendered Hive Errors component results.
- Node: both production polling lifecycle tests passed.
- Hive Mind: 108 targeted tests passed in its Linux image, including the real
  mind error endpoint and existing mind-server/access checks. One database check
  initially skipped without a DSN; it was rerun with the disposable test database
  and passed.
- Python compilation, focused Ruff checks, and template parsing passed.

Screenshots are in `C:\Users\dastewart\Documents\Hive\Feature-validation`.
No live Hive container was updated. The disposable test containers and network
were removed after verification. Source/test archives remain under `/tmp` for
attributable evidence.

## Deployment

Deploy the `hive-mind` feed changes to Comms, Lucent, and each mind, then deploy
the site changes. No new database schema or Docker socket mount is needed. The
site uses its existing admin credentials for component diagnostics. Until a
component is updated its feed is marked unavailable. Historical Docker errors
from before the updated process started are not imported.
