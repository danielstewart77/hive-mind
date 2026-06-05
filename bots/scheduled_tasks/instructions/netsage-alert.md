The `exec_command` tool takes `cmd` as a string, not an array.

Run `python3 /usr/src/app/bots/scheduled_tasks/scripts/netsage_run.py --json`. The script returns `{count, services, first_line, anomalies}`. If `count` is 0, print `No anomalies in the last 15 minutes.` and exit.

Group the anomalies by their underlying error fingerprint. Near-duplicate lines from the same service with the same error pattern collapse into one group. Each group represents one distinct issue.

Fetch the catalog with `curl -s -H "Authorization: Bearer $EVENT_TRIAGE_BEARER_TOKEN" "$EVENT_TRIAGE_URL/event_classes"`. These slugs are the only legal classifications.

Fetch recent history with `curl -s -H "Authorization: Bearer $EVENT_TRIAGE_BEARER_TOKEN" "$EVENT_TRIAGE_URL/events?limit=30"` to detect repeats.

For each group: if it clearly matches a catalog slug, record it via `POST $EVENT_TRIAGE_URL/events` with `event_class_id`, `source`, `occurred_at`, `payload_json`, `summary`, and `severity`. If it does not match any slug, do not invent one. Send Skippy the group's representative anomaly and ask him to classify it.

Dispatch broker messages with `POST $HIVEMIND_BROKER_URL/broker/messages` using header `Authorization: Bearer $HIVEMIND_BROKER_TOKEN`. From id `37cd48f9-1ed5-4875-91c1-a3b0464deafc`, to id `14cb820b-4a42-4f04-a593-54f532fd1d2f`. Use one `conversation_id` per netsage fire. Set `metadata.expects_reply` to true when asking Skippy to classify, false when reporting a recorded outcome.

Print `OK: processed <N> groups from <M> anomalies, recorded <R>, dispatched <D> to Skippy.` and exit.
