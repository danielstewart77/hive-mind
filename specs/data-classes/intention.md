# Data Class: intention

## Description
A stated plan or intention from Daniel that has not yet been acted on — recognizable by "Daniel wants to", "Daniel plans to", "we should", or similar forward-looking language tied to a near-term action with no existing project structure. Lighter than `future-project` (no architecture or design detail required).

## Actions
- save-vector

## Notes
- Distinguished from `future-project` (has architecture, hardware, or design detail) and `technical-config` (already implemented)
- If Daniel's intention evolves into a planned project with design decisions, reclassify to `future-project`
- If the intention is completed and becomes fact, reclassify to the appropriate class
- Examples: "Daniel wants to share his health docs folder", "Daniel plans to add TOTP to important accounts"
