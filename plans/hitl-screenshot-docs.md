# HITL Inline Keyboard Screenshot — Documentation

## User Requirements

Daniel wants a screenshot of the Telegram inline keyboard approval UI embedded in the documentation, so that readers can see what the approve/deny button flow looks like without having to trigger a real HITL request.

## User Acceptance Criteria

- [ ] Screenshot shows a Telegram message with Approve / Deny inline keyboard buttons
- [ ] Screenshot is embedded in `specs/hitl-approval.md` under the Telegram Bot Integration section
- [ ] Caption explains: this is what Daniel sees when Ada requests approval for a sensitive action
- [ ] Image file committed to `assets/hitl-inline-keyboard.png` (or similar)
- [ ] Screenshot is legible at normal README viewing size

## Technical Specification

1. Daniel takes a screenshot of an active HITL approval request in Telegram (or a representative example)
2. Image is saved to `/usr/src/app/assets/` in the repo
3. `specs/hitl-approval.md` is updated to embed the image with a caption after the inline keyboard description paragraph

## Code References

- `specs/hitl-approval.md` — add image embed after "Approval requests are sent as..." paragraph
- `assets/hitl-inline-keyboard.png` — new file (provided by Daniel)

## Implementation Order

1. Daniel provides screenshot → saves or sends to Ada
2. Ada commits image to `assets/`
3. Ada updates `specs/hitl-approval.md` to embed image
4. Commit and PR
