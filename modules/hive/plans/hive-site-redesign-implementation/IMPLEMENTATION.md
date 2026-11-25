# Hive Site Redesign Implementation

1. Add authenticated console routes for overview, setup, minds, outposts, tools, activity, and scoped settings.
2. Add overview, Outpost, and Hive Tools API data using existing broker, service-health, and mounted-repository sources.
3. Redact secrets from all mind configuration returned to the browser.
4. Add a validated Outpost bootstrap generator that never embeds credentials or creates identity.
5. Replace the global navigation with a responsive application shell and focused page templates.
6. Replace the settings tab strip with scoped settings navigation and working controls only.
7. Preserve existing graph, memory, rules, canvas, login, and terminal access through the new shell.
8. Run the full test, type, and lint suites, then rebuild and health-check the deployed container.
> Historical terminology note (2026-07-26): this implementation plan predates
> the current taxonomy. "Outposts" are now **Edge Minds**, and external tools
> are organized as **Capability Nodes**. Legacy route names remain only as
> compatibility redirects.
