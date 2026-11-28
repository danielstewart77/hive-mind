# Mind rename host support

Hive starts a rename using the permanent Mind UUID. Hive Tools reserves both
names, then its normal human approval gate authorizes the complete operation.
The website waits for the gateway's per-Mind idle gate before asking Tools to
continue. An attached terminal must be suspended or released first.

Configure HIVE_RENAME_PROJECT_DIR with the host Hive stack directory and apply
docker-compose.mind-renames.yml beside the normal Compose file. Precreate the
updates/mind-renames directory, mode 700, owned by the Tools service UID. Give
that UID write access to the managed minds directory, stack file and env file.
The override grants writes only to those resources, preserving the read-only
mount for the remaining Dev root. Hive and comms need the matching rename
support, and Hive needs hive-data's rename_mind/preflight_mind_rename operations.

Tools retains the stopped original container until the website commits the
registry, credential holders, graph label and provider binding. Startup or lost
responses are reconciled from both journals. The website resumes pending work
and keeps the gateway gate closed until commit or verified recovery. Tools
cannot continue without that durable gate and the operation's existing approval.

The image is retained exactly. No dependencies are restarted, no volumes are
deleted, and no key is issued or rotated. Old internal bind destinations stay
available for historical CWD and harness transcripts. The broker retargets its
cached routes to the new gateway URL; the old network name is then free. User
skill, soul and transcript contents are not searched/replaced.

If recovery is pending, use Retry recovery on the Mind's page. Do not delete
either journal or either container manually: the interrupted phase and exact
original image/configuration are needed to recover safely.
