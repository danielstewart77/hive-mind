# Data Class: person

## Description
A named individual — recognizable by a person's name as the subject, with associated facts about their relationship to Daniel, role, personality, preferences, or notable details. Includes family, friends, colleagues, and acquaintances.

## Actions
- save-graph
- save-vector

## Conventional Properties

Store whatever is known using these field names. None are required — include only what you have.

- `first_name` — given name (e.g. `"Manny"`)
- `last_name` — surname (e.g. `"Vark"`)
- `title` — honorific or address form (e.g. `"Coach"`, `"Dr."`)
- `relationship` — **always a JSON array**, even for one value. How this person relates to Daniel (e.g. `["wife"]`, `["coworker", "friend"]`, `["son", "child"]`). Supports multiple values for people with overlapping roles.
- `aliases` — JSON array of other names Daniel uses for this person (e.g. `["Coach", "Manny"]`)

The canonical node `name` should be the fullest known form (e.g. `"Wil Vark"`, `"Coach Manny"`).
These fields exist so `search_person` can find nodes by any known fragment.

## Notes
- Subject must be a specific named person, not a generic role (e.g., "Brian from soccer" yes, "a coworker" no)
- Include relationship context (how Daniel knows them) when present
- Family members (Xiaolan, Sloan, etc.) belong here
- Facts about Daniel himself belong here too
- Split multi-person chunks if each person warrants a separate node
