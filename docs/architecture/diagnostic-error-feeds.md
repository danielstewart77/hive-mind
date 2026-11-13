# Diagnostic error feeds

The mind server, Comms, and Lucent expose `GET /diagnostics/errors` behind their
existing admin authorization. The Hive site's admin-only Hive Errors page reads
these feeds on demand; no background poller is added to these components.

The response contains `started_at` and `errors`, newest first. Each error has
`timestamp`, `level`, `message`, and `detail` (the exception reason when present).
The existing structured log formatter records ERROR/CRITICAL events in a locked,
bounded in-memory feed: at most 100 events, expiring after one hour. Restarting
clears the feed. Inline bearer credentials and configured secrets are redacted
from diagnostic messages; stack traces remain in logs and are not in the feed.

Branch: `feature/hive-error-feeds`. Consumer: `hive` branch
`feature/navigation-and-hive-errors`. No database migration or new credential is
needed. These changes have not been deployed to the running installation.

The user's accepted P2 protection is that real component failures, such as the
terminal refusal for missing model context metadata, appear on Hive Errors.
`tests/unit/test_diagnostic_errors.py` calls the live logging code for minds and
the nervous system, verifies retention/redaction, and requests the real mind
endpoint with and without admin authentication. The site tests cover collection,
rendered navigation and account isolation; its Node tests cover polling lifetime.

Validation: 108 targeted Linux tests passed across diagnostic feeds, structured
logging, the mind server, and Comms route classification. The database-dependent
classification case was rerun with a disposable PostgreSQL DSN after initially
skipping. Only the isolated validation containers were modified.
