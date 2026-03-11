---
name: planka
description: Manage Planka Kanban board cards and projects
user-invocable: false
---

# Planka Tool

Interact with the Planka Kanban board.

## Usage

```bash
python /usr/src/app/tools/stateless/planka/planka.py <command> [args]
```

## Commands

- `list-projects` -- List all projects and boards
- `get-board --board-id <id>` -- Get board with lists and cards
- `get-card --card-id <id>` -- Get card details
- `move-card --card-id <id> --list-id <id>` -- Move card to list
- `add-comment --card-id <id> --text "<text>"` -- Add comment
- `update-card --card-id <id> [--name "..."] [--description "..."]` -- Update card
- `assign-label --card-id <id> --label-id <id>` -- Assign label
- `create-card --list-id <id> --name "..." [--description "..."] [--card-type story]` -- Create card

## Output

JSON with operation results.
