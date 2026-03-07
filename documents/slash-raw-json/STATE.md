# Story State Tracker

Story: [Bug] Slash command returns raw JSON to Telegram chat
Card: 1722252456479425985
Branch: story/slash-raw-json

## Progress

- [state 1][X] Pull story from Planka
- [state 2][X] Create implementation plan
- [state 3][X] Implement with TDD
- [state 4][X] Code review
- [state 5][ ] Ready for merge

## Acceptance Criteria

- [ ] Slash commands (`/remember`, `/plan`, etc.) no longer return raw JSON to the chat
- [ ] Commands either complete silently or send a brief, human-readable confirmation message
- [ ] No structured response payloads leak to the user-facing Telegram interface
- [ ] Session completion is internally tracked without exposing implementation details
