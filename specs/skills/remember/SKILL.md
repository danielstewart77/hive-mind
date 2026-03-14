---
name: remember
description: >
  Save a specific piece of information to memory. Trigger when the user says
  "remember [something]", "remember this", or "/remember [something]".
  Pass the content as the argument to memory-manager.
user-invocable: true
---

# remember

Run the `memory-manager` skill with:
- trigger: manual
- content: the thing the user wants remembered (everything after "remember")
