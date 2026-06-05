The `exec_command` tool takes `cmd` as a string, not an array.

Run `python3 /usr/src/app/bots/scheduled_tasks/scripts/netsage_run.py --json`. The script returns `{count, services, first_line, anomalies}`. If `count` is 0, print `No anomalies in the last 15 minutes.` and exit.

Group the anomalies by their underlying error fingerprint. Near-duplicate lines from the same service with the same error pattern collapse into one group. Each group represents one distinct issue.

Fetch the catalog with `curl -s -H "Authorization: Bearer $EVENT_TRIAGE_BEARER_TOKEN" "$EVENT_TRIAGE_URL/event_classes"`. These slugs are the only legal classifications.

Fetch the approved auto-apply rules with `curl -s -H "Authorization: Bearer $EVENT_TRIAGE_BEARER_TOKEN" "$EVENT_TRIAGE_URL/response_rules?approval_state=approved"`. Build a lookup of `event_class_id` to `{rule_id, action_kind}`.

For each group, classify it against the catalog. Always set `source` to `bilby_netsage` and `severity` to one of `low`, `medium`, or `high`.

If the group matches a catalog slug AND that class has an approved auto-apply rule with `action_kind=record_only`, POST the event with that `event_class_id`, `status=ignored`, and `response_rule_id` set to the matching rule id. Do not dispatch to Skippy — the rule is the policy. Increment a local `R` counter.

If the group matches a catalog slug but the class has no approved auto-apply rule, POST the event with `status=awaiting_triage`, then dispatch a broker message to Skippy with the group's representative anomaly and `metadata.expects_reply=true` so he can decide the policy. Increment a local `D` counter.

If the group matches no catalog slug, do not invent one. Do not POST an event. Dispatch a broker message to Skippy with the representative anomaly and `metadata.expects_reply=true`, asking him to name a class. Increment `D`.

Dispatch broker messages with `POST $HIVEMIND_BROKER_URL/broker/messages` using header `Authorization: Bearer $HIVEMIND_BROKER_TOKEN`. From id `37cd48f9-1ed5-4875-91c1-a3b0464deafc`, to id `14cb820b-4a42-4f04-a593-54f532fd1d2f`. Use one `conversation_id` per netsage fire.

Print `OK: processed <N> groups from <M> anomalies, recorded silently <R>, dispatched <D> to Skippy.` and exit.
