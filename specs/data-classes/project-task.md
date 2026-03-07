# Data Class: project-task

## Description
A project management event or task record — recognizable by references to Planka cards, backlog items, story creation, card moves, or similar task-tracking activities. These are operational records with no lasting memory value.

## Actions
- `discard`

## Notes
- Planka card created, moved, or closed → discard
- Backlog item noted → discard
- Story started or completed → discard
- If a task event contains a significant decision or technical fact, that fact should be extracted as a separate chunk and classified as `technical-config`
