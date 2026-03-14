# Testing Guidelines

## What Makes a Test Worth Keeping

A test earns its place in the codebase by satisfying **all three** of these:

1. **It can fail.** A test that always passes is not a test — it's noise. If the production code it covers were deleted or broken, the test must fail.
2. **It traces to a requirement.** Every test should correspond to an acceptance criterion, a task, or a documented edge case. "Test exists for its own sake" is not a reason.
3. **It asserts observable behavior.** Return values, raised exceptions, side effects, API responses, database state. Not internal variables, private methods, or implementation details.

## What Does NOT Belong in the Codebase

- **Absence tests** — asserting that a removed library, module, or feature is no longer present. These pass trivially once the thing is gone and add zero value forever after.
- **Migration tests** — written to verify a one-time refactor or removal. Once the migration is done, delete them.
- **State tests** — asserting the current state of the world rather than expected behavior. Example: "the config file contains X" when nothing in the system guarantees X must stay.
- **Line-count chasing** — tests written to hit a coverage number, not to verify a behavior.

## Test Levels

| Type | File Pattern | When to Use |
|------|-------------|-------------|
| **Unit** | `tests/unit/test_<module>.py` | Pure logic, utility functions, class methods. Mock all I/O and external deps. |
| **Integration** | `tests/integration/test_<feature>.py` | Real DB queries, HTTP calls, file I/O, multi-component workflows. |
| **API** | `tests/api/test_<endpoint>.py` | FastAPI routes via `TestClient`. Auth, error responses, request/response shape. |

Rule of thumb: if it needs real I/O or talks to another real component, it's an integration test. If it only calls functions and checks return values with mocked deps, it's a unit test.

## TDD Cycle

**Red → Green → Refactor. Always.**

1. Write a failing test that defines the behavior.
2. Write the minimum production code to make it pass.
3. Refactor — only after tests are green.

Never write production code without a failing test that demands it.

## Bug Fixes

Every bug fix must include a **regression test** that:
- Reproduces the bug in its original broken state (fails without the fix)
- Passes after the fix
- Will catch the same bug if it ever regresses

No exceptions.

## Test Hygiene

- Delete tests that cover removed features — they will always pass and mislead future developers.
- When a feature is removed, search for its tests and remove them in the same commit.
- Review tests during code review with the same scrutiny as production code.
