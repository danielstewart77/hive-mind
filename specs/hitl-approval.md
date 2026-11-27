# Human-in-the-Loop (HITL) Approval

## Purpose

Any mutating, destructive, or high-blast-radius action requires explicit human approval before execution. The confirmation uses an out-of-band channel — the approval signal arrives via Telegram, which is unreachable from within the tool execution environment.

## Where it lives

The request is held by the `hive-tools` service: it gates a tool call and **holds the mind's call open** while the request waits for a person (the request is recorded for the page and the waiting list), and tells the person where to approve. A person decides on a page of the hive site, `/approvals/{request id}`, signed in as themselves. Approving releases the same open call, which runs the tool and answers the mind with its result; denying, or nobody deciding within `HITL_WAIT_SECONDS` (default five minutes), answers the same call with a plain refusal. Nothing is replayed later and no mind's key is kept.

## Flow

```
A mind calls a HITL-gated hive-tools endpoint
  → hive-tools records the request and holds the call open (the mind waits on it)
  → the notice carries the link to the hive site
  → the person opens it, signs in (sent back to the same request), sees which mind asked,
    which tool, exactly what it will do, when, and Approve and Deny
  → the hive site sends the decision to hive-tools with that person's own key
  → hive-tools records the person's name and releases the held call: the tool runs and its
    result is the mind's reply. Deny, or no answer in time: the same call is answered no.
```

## Who can decide

The person who owns the mind that asked, and any administrator. Anyone else
signed in is told they may not and sees none of the content. A mind's key is
refused with 403 and the request stays pending. There is no Approve or Deny
button in Telegram or Discord and no approver credential. The link carries only
the request's identifier. `/approvals` lists what waits for the signed-in person.

## The record

`GET {hive_tools_url}/hitl/{request_id}` is the record of a request (who asked,
which tool, when, its status, who decided it), not a way to wait: the answer
to the call is the reply to the call itself.

## Actions Requiring HITL

- Sending, deleting, or modifying email
- Modifying calendar events
- Docker Compose operations (up, down, restart)
- Posting to social media
- Executing shell commands beyond tool scope

## Implementation Files

- hive-tools' `hitl.py` and `server.py` (`GET /hitl/{id}`, `GET /hitl/{id}/review`, `GET /hitl/waiting`, `POST /hitl/{id}/respond`) and `auth_api.require_person`
- the hive site's `/approvals` pages in `modules/hive/src/main.py` and `templates/approval.html`; see [docs/architecture/hive-tools.md](../docs/architecture/hive-tools.md)
