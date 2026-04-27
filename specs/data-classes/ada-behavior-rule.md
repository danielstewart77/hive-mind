# Data Class: ada-behavior-rule

## Description
A prescriptive behavioral rule for Ada — derived from session feedback, corrections, and pattern observations. Each rule is written in actionable "when X, do Y" or imperative form and is intended to be retrieved semantically to influence Ada's decisions in future sessions.

## Actions
- save-vector

## Notes
- Must be prescriptive, not merely descriptive — it must suggest a specific action or decision
- Derived from HOLDS_VALUE and HAS_PREFERENCE graph nodes that were confirmed as actionable
- Distinguished from `preference` (Daniel's or Ada's stable tendencies) and `ada-identity` (who Ada is)
- Examples: "When a skill requires asking the user, execute that step as written — do not infer around it.", "Never embed credentials in skill files; use keyring lookups only."
