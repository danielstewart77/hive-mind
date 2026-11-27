# Human-in-the-Loop (HITL) Approval

## Purpose

Any mutating, destructive, or high-blast-radius action requires explicit human approval before execution. The confirmation uses an out-of-band channel — the approval signal arrives via Telegram, which is unreachable from within the tool execution environment.

## Where it lives

The request is held by the `hive-tools` service: it gates a tool call, persists the pending request (database-backed, survives a restart), and tells the mind and the person where to approve. A person decides on a page of the hive site, `/approvals/{request id}`, signed in as themselves. What approving does afterwards (`hitl.respond_to_hitl` hands back the saved call and the tools replay it) is in the tools, in one place.

## Flow

```
A mind calls a HITL-gated hive-tools endpoint
  → hive-tools persists the request, answers "pending" with the link
  → the notice (and the conversation, wherever it is shown) carries the link to the hive site
  → the person opens it, signs in (sent back to the same request), sees which mind asked,
    which tool, exactly what it will do, when, and Approve and Deny
  → the hive site sends the decision to hive-tools with that person's own key
  → hive-tools records the person's name, then runs the saved call
```

## Who can decide

The person who owns the mind that asked, and any administrator. Anyone else
signed in is told they may not and sees none of the content. A mind's key is
refused with 403 and the request stays pending. There is no Approve or Deny
button in Telegram or Discord and no approver credential. The link carries only
the request's identifier. `/approvals` lists what waits for the signed-in person.

## Polling

A caller (or this repo's own retry logic) can also check status directly:
`GET {hive_tools_url}/hitl/{request_id}` on hive-tools.

## Actions Requiring HITL

- Sending, deleting, or modifying email
- Modifying calendar events
- Docker Compose operations (up, down, restart)
- Posting to social media
- Executing shell commands beyond tool scope

## Implementation Files

- hive-tools' `hitl.py` and `server.py` (`GET /hitl/{id}`, `GET /hitl/{id}/review`, `GET /hitl/waiting`, `POST /hitl/{id}/respond`) and `auth_api.require_person`
- the hive site's `/approvals` pages in `modules/hive/src/main.py` and `templates/approval.html`; see [docs/architecture/hive-tools.md](../docs/architecture/hive-tools.md)
