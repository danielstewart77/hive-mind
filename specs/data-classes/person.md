# Data Class: person

## Description
A named individual — recognizable by a person's name as the subject, with associated facts about their relationship to Daniel, role, personality, preferences, or notable details. Includes family, friends, colleagues, and acquaintances.

## Actions
- save-graph
- save-vector

## Notes
- Subject must be a specific named person, not a generic role (e.g., "Brian from soccer" yes, "a coworker" no)
- Include relationship context (how Daniel knows them) when present
- Family members (Xiaolan, Sloan, etc.) belong here
- Facts about Daniel himself belong here too
- Split multi-person chunks if each person warrants a separate node
