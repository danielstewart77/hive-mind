# Data Class: future-project

## Description
A planned or future project that Daniel is designing or intending to build — not yet implemented, not yet on Planka. Recognizable by forward-looking project descriptions, architecture plans, hardware requirements, constraints, and goals for systems Daniel intends to build. Once a project is active on Planka, individual tasks become `project-task` and implemented facts become `technical-config`.

## Actions
- `save-vector`
- `save-graph`

## Notes
- Use for projects in the concept or planning phase — "we're going to build X" not "X is built"
- Covers hardware plans, architectural decisions, integration goals, and known constraints for future systems
- When the project moves to implementation, reclassify its durable technical facts as `technical-config`
- Examples: NetSage (home lab network monitoring), new integrations, homelab expansion plans
- Tier: reviewable — prune when the project is shipped, cancelled, or superseded
