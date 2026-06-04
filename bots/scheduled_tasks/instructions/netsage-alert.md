# NetSage Alert

Your only job on this fire is to execute one Python script. The script does the entire NetSage pass — Loki query, anomaly filter, Telegram notification, broker dispatch — and prints a one-line status to stdout.

## Run this command exactly

```
python3 /usr/src/app/bots/scheduled_tasks/scripts/netsage_run.py
```

Do not compose bash variables, heredocs, or shell substitutions. Do not draft or invoke `notify.py` directly. Do not call `apply_patch`. The script is already committed at the path above and mounted into your container — you only need to run it.

## Report back

Reply with exactly the script's stdout line. Typical outputs are "No anomalies in the last 15 minutes." or "OK: notified Daniel and Skippy about N anomalies". Nothing else.
