-- Persistent gateway watches use stable session row IDs, not conversation IDs.
SELECT pg_catalog.set_config('search_path', '', false);

CREATE TABLE core.session_watches (
    watched_session_id text NOT NULL REFERENCES core.sessions(id) ON DELETE CASCADE,
    sender_session_id text NOT NULL REFERENCES core.sessions(id) ON DELETE CASCADE,
    task text NOT NULL,
    PRIMARY KEY (watched_session_id, sender_session_id)
);

CREATE INDEX session_watches_sender ON core.session_watches (sender_session_id);
