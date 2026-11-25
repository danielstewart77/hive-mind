# The release record

A release record says what a machine's hive runs and from exactly which source: every service, the image it runs, the commit its source is at, whether that source has uncommitted changes, and every deliberate local difference. It holds no secret value.

## Running it

On the host that runs the hive, as the operator:

    python3 tools/stateless/release_record/release_record.py --out STATE_DIR/records/<name>.json

It prints one JSON record on stdout and exits 0. It only reads: `docker compose config`, `docker ps`, `inspect`, `image inspect`, `git rev-parse`, `status`, `remote get-url` and `ls-files`, and nothing else. With `--read-installed-commits` it also runs one fixed read-only python snippet inside each running container (`docker exec`) to learn which commit a branch-named install fetched; without the flag that commit is null and the entry says so. It builds, starts, stops and pulls nothing, and writes only the file named by `--out`.

Options: `--root DIR` is the hive-mind checkout (default: the one the script is in); `--compose FILE`, repeatable, adds another stack, for the console, terminal and tools containers that start from their own compose files; `--repo DIR`, repeatable, records the commit and dirty state of a checkout outside the stack, such as the inference proxy; `--container NAME`, repeatable, adds another container name to look for.

## Where a machine keeps its records

In a state directory under the install, for example `<install>/state/release-records/`, one file per run. Never inside a source checkout: a record there would make the checkout dirty, and the tool refuses `--out` inside a git work tree (exit 2). The directory must already exist. Story 885's release script reads the `identity` key of the newest record to know the release it is leaving.

## Fields

`identity` is the release's name: `hive_mind_commit` (full commit id of the checkout), `image_ids_hash` (sha256 over the sorted lines `project/service=image id`) and `string`, the two joined with a plus. It never holds a branch name. `complete` is false and `string` is null when the commit, a compose config or any service's image id could not be read, so a failed run never looks like a release. Read this key back to compare two releases.

`information_only.branch` is the branch checked out, a label for people only.

`hive_mind` is the checkout's state: `commit`, `dirty`, `tracked_changes`, `untracked`, `remote` (any password removed).

`stacks` lists each compose project and the files it was read from. `compose_files` gives each file's path, sha256, whether git tracks it, and its role (compose, override or include).

`local_differences` names what differs from what git holds: `untracked_compose_files`, `override_files`, `include_files`, `mind_settings_files` (each `minds/*/runtime.yaml`, names of its `env:` entries only), `env_files` (each with the names of the variables set in it, never values), and `path_variables_outside_checkout` (a variable ending in DIR, PATH, HOME, ROOT or FILE whose path leaves the checkout; its path is shown, it is not a secret).

`services` has one entry per service, running or not: `stack`, `service`, `container`, `running`, `image_id` (sha256), `image_reference` and `image_digest` (the registry digest where one exists), `dockerfile`, `target`, `base_image` (reference and digest), `contexts` (each build context with `commit` and `dirty`), `extra_sources` (extra build contexts and `git+` installs; a branch-named install says so and gives `installed_commit`, read from the running container when `--read-installed-commits` is given), `mounts` (host source laid over the container: `host`, `container`, `read_only`, and the host path's `commit` and `dirty`), `env_var_names` and `env_files`. For services that mount the checkout over the image's code, the mount's commit is the truth about the running code, not the image.

`repos` holds each `--repo` checkout. `outside_stack` names what the record does not cover: the inference proxy and the edge minds. `missing_expected` lists the expected names found in no compose file and no container: `voice-server`, `voice-server-kokoro`, `hive-site`, `terminal-site`, `hive-tools`. `notes` says what could not be read and why.

## What it does not do

It does not judge a release good, go back to one, or say what an operator may do. No image carries a revision label today, so an image id cannot be traced to a commit after the fact; the record shows what is observable at the time it is taken.
