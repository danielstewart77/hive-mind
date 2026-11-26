# One repository: bringing a machine onto it, checking it, and going back

hive-mind now holds the data package, the chat surfaces, the console, the
terminal and the tools. Only inference-proxy, auth-service and hive-harness-dsh
stay in their own repositories. This page is for whoever runs a machine: a shell,
Docker and `git` are all it needs. It does not need a roster mind.

Nothing in it has been run on a real machine by its author (there was no Docker
where it was written). The first real run is the proof; where a step is a guess
about a live machine it says so.

## Words

- **A release** is a commit of hive-mind, named by a tag (or the commit itself).
  Never by a branch: a branch moves.
- **Going back** is a checkout of the old tree plus a restart. It is not an image
  swap: the gateway, memory store, console, terminal, every mind and the bots run
  source that is bind-mounted from the checkout over their image, so the checkout
  is what runs. Images are rebuilt from the same tree so the two agree.
- **The state directory** is where `scripts/release.sh` keeps what it needs to go
  back: `<install>/state/release/` (`current`, `previous`, `history.log`) and
  `<install>/state/release-records/`. It is outside the checkout, on purpose.

## What this procedure never touches

`.env` files, `minds/*`, generated compose files (including the
`# BEGIN GENERATED VOLUMES` block), `nervous-system/data`, the bind-mounted data
folders, and the named volumes `hive-postgres-data`, `whisper-cache`,
`kokoro-cache` and `codex-global`. `scripts/release.sh` never writes, moves or
deletes any of them. It does not run a database migration, does not recreate a
container unless told to with `--restart`, and does not push, tag or merge.

**Keep the compose project name.** Named volumes are called
`<project>_<volume>`. A fresh clone in a folder with a different name starts a
different project and sees an empty database. Check before anything else:

```
docker volume ls | grep hive-postgres-data
```

The part before `_` is the project. If the checkout's folder is not that name,
run every `docker compose` command for the stack with `-p <that name>` (or set
`COMPOSE_PROJECT_NAME` in the shell). The installer's own stack is project
`hive`; `release.sh` passes `-p hive` itself when `HIVE_HOME` is set. The console
and terminal have their project names pinned in their compose files (`hive`,
`terminal`); the tools' is pinned too (`hive-tools`). Do not change any of them.

## Part 1. A fresh machine (the installer)

```
curl -fsSL <published address>/install.sh | HIVE_RELEASE=<release-tag> bash
```

Without `HIVE_RELEASE` a fresh clone is the default branch's tip at that
moment, which is not a release; name one. The installer clones hive-mind, puts it
on that release, checks out the three outside repositories at the commits the
release records, writes `minds/.release-context` (below), and builds every
service from that one clone. A build needs network access for what it downloads:
base images, apt, npm, pip from PyPI and the training image's llama.cpp. Those
are not pinned by this work.

The tracked `docker-compose.example.yml` is not the whole hive: it holds the
gateway, memory store, voice, the two bots and the database. The console,
terminal, tools and every mind are the installer's output (and the folders'
own compose files). For a hand-made stack, copy the example to
`docker-compose.yml` and also build the console, terminal and tools from their
folders (Part 2, step 8).

Before the first `up` on a machine that has never run the stack, two things
must exist, because the compose files declare them `external`:

```
docker network create hivemind
docker volume create codex-global
```

The installer creates the network itself. Create the volume only if
`docker volume ls` does not list it; never recreate one that exists.

## Part 2. Bringing an existing install onto the one repository

Do these in order, one machine at a time. Steps 1 to 4 change nothing that runs.

1. **Record what is running.** From the checkout:
   `python3 tools/stateless/release_record/release_record.py --read-installed-commits --out <install>/state/release-records/before.json`.
   Keep the file. It names every service, the image it runs, the commit its
   source is at and every local difference. It holds no secret.
2. **Note the release you can go back to.** The record's `identity.string` is it.
   If the old tree is only ever a branch, write down its commit
   (`git rev-parse HEAD`) and push it somewhere a remote has: `release.sh` refuses
   a commit no remote publishes.
3. **Check the volumes and the project name** (see above). Also check that
   the console's `/data` is a named volume or a bind mount, not the container's
   own layer: the key journal that makes replacing a mind's key safe after a
   crash lives in `/data/key-journal` (or `HIVE_KEY_JOURNAL_DIR`). If `/data`
   is not mounted, add the mount first.
4. **Deal with old clones.** Five repositories became folders of hive-mind:
   `hive-data`, `hive-surfaces`, `hive` (the console), `terminal` and `hive-tools`.
   An old `modules/<name>` clone of any of them cannot be pulled over. `release.sh`
   (and `installer/install.sh`) look for one, refuse if it has uncommitted work or
   commits no remote holds, and otherwise move it to
   `modules/.<name>.moved-aside-<time>`. Carry the untracked files out of it into
   the new folder before deleting it: `modules/hive/.env` and `data/`,
   `modules/terminal/.env` and `data/`. **The tools are the exception:** their
   `.env`, `credentials/` and `data/` go to `~/.config/hive-tools` (or
   `HIVE_TOOLS_STATE_DIR`), never into the checkout, and the console's compose
   mounts the tools `.env` from there. Create `~/.config/hive-tools/.env` as a
   file before the console starts, or Docker makes a directory of it. Sibling
   checkouts beside hive-mind (`hive`, `terminal`, `hive-data`, `hive-tools`,
   `hive-surfaces`) are left in place and are no longer read; delete them when
   everything has run for a while.
5. **Delete the old variables** from every `.env` and shell: `HIVE_DATA_DIR`,
   `HIVE_SITE_DIR`, `HIVE_TOOLS_DIR`. They pointed at sibling checkouts that can
   be a different schema from this code, so the new compose files ignore them.
   (The console's other host paths, `HIVE_MIND_DIR`, `HOST_DEV_DIR`,
   `SKIPPY_DIR`, `MORDECAI_DIR`, `INFERENCE_PROXY_DIR`, `HIVE_INIT_DIR`, now
   default to places inside the checkout or the home folder instead of one
   person's folders; a machine that keeps them elsewhere sets them in
   `modules/hive/.env`. If `~/.git-credentials`, `~/.config/gh/hosts.yml` or an edge mind's `.env` does not exist, create it empty first, or Docker makes a root-owned directory of it. On a machine that already sets them nothing changes.)
6. **Edit the compose files hive-mind does not track.** They cannot ride a pull
   request. Per machine: `docker-compose.yml` (and any `.pre-*` copies), each
   `minds/*/container/compose.yaml` that is hand-kept (for example
   `minds/stash/container/compose.yaml`), the surface-bot deploy scripts.
   - Every `hive_data` build context and the read-only overlay of the data
     package become `./modules/hive-data` (relative to the file; for the stash
     fragment `../../../modules/hive-data`). Drop the `HOST_DEV_DIR/hive-data`
     mount.
   - Every service built from the root `Dockerfile` (the bots, minds) gets
     `additional_contexts: hive_surfaces: ./modules/hive-surfaces`
     (`../../../modules/hive-surfaces` in a mind's fragment).
   - Every container that mounts the checkout needs the two tmpfs lines that
     hide the modules from it: `/usr/src/app/modules:ro` and
     `/usr/src/app/.git/modules:ro`. Without them a mind can read the tools'
     `.env`, the console's `.env` and `data/`. The installer's output and the
     tracked example have them; hand-kept fragments do not.
   - Scripts that name `Dev/hive/.env` or `Dev/terminal/.env` (for example
     `.mint-services.py`) need `modules/hive/.env` and `modules/terminal/.env`.
7. **Move the checkout onto the release.**
   ```
   scripts/release.sh --state-dir <install>/state --compose <your docker-compose.yml> --env-file <your .env> --dry-run to <release-tag>
   scripts/release.sh ...same... to <release-tag>
   ```
   It refuses, and changes nothing, if tracked files are modified, the tag
   names a branch or no commit, no remote publishes the commit, or an old clone
   holds unsaved work. Otherwise it checks the tree out detached, brings
   inference-proxy, auth-service and hive-harness-dsh to the commits the release
   records, writes `minds/.release-context` (the console builds a new mind from
   exactly this commit), and rebuilds the stack's images. It does not recreate
   anything: bind-mounted source is already the new tree, and a running process
   keeps running the old code until it is restarted.
8. **Build the sites and the tools.** `release.sh` builds the console and the
   terminal if `modules/hive/.env` and `modules/terminal/.env` exist, and the
   tools if `~/.config/hive-tools/.env` exists. By hand:
   `docker compose -f modules/hive/docker-compose.yml build`,
   `docker compose -f modules/terminal/docker-compose.yml build`, and
   `modules/hive-tools/deploy.sh build`. The tools build from `git archive` of the
   committed tree, and `deploy.sh` refuses a commit no remote has.
9. **Apply the database step once** (a release that carries one; the release
   notes say so). The migrations add only and apply once, under a lock; the
   previous release keeps working against the changed database:
   `docker exec hive-comms python -m hive_data.migrate check` then `apply`.
10. **The operator restart.** Recreate the services that run new code. The
    console and the terminal are separate projects, so recreating one never
    restarts the other, and an open terminal session survives a console update.
    Order: tools (`modules/hive-tools/deploy.sh`), gateway and memory store,
    console, terminal, bots, minds. This step is yours; `release.sh --restart`
    does the stack's `up -d` only.
11. **Tools credentials.** Put `TOOLS_ADMIN_BEARER_TOKEN` in the tools' `.env`
    and in the console's environment. Until both have it the console refuses to
    create a mind (loudly, and undoes the creation), because creating one
    registers its key with the tools.
12. **Tag the packages.** After merge, someone with push rights cuts
    `hive-data-v0.4.0` and `hive-surfaces-v0.2.0` (tag equals the package's
    `pyproject.toml`), copies `docs/setup/workflows/*.yml.txt` to
    `.github/workflows/` (the push token cannot write there), then tags the
    release. The edge minds' pin moves to the surfaces wheel after that.

## Part 3. Verify

- `scripts/release.sh status` shows the commit and the one it would go back to.
- `git -C <checkout> rev-parse HEAD` equals the tag's commit; each of the three
  modules is detached at the commit `git ls-tree HEAD modules` shows.
- Run the release record again and compare `identity.hive_mind_commit`.
- `docker volume ls`: `hive-postgres-data`, `whisper-cache`, `kokoro-cache`,
  `codex-global` are the same volumes as before (same names, same creation dates).
- `<install>/.env` and the `minds/*` files are byte-identical to what they were
  (take a checksum before step 7 and after).
- The console's Add a Mind page builds from `minds/.release-context`; with that
  file missing and no `HIVE_MIND_BUILD_CONTEXT`, it refuses with the reason. It
  never builds from a branch.
- From inside a mind's container, `ls /usr/src/app/modules` is empty and the
  tools' `.env` cannot be read.
- One owner can attach to their own terminal session and another is refused.

## Part 4. Going back

```
scripts/release.sh --state-dir <install>/state --compose <your docker-compose.yml> --env-file <your .env> back
```

`back` is `to` the release recorded as the previous one. It checks the old tree
out, puts the three outside modules on the commits that tree records, rewrites
`minds/.release-context` to that commit, and rebuilds the images from it. Then
recreate the services (step 10). Running `back` again goes forward again.

**It skips the database migration step, on purpose.** An older data package
refuses a migration check against a database that records a step it does not
know (`hive_data/migrate.py`), and nothing checks at start. So going back never
runs `migrate check` or `apply`, and never undoes a migration. The changed
database stays as the newer release left it; the previous release keeps working
against it because every step only adds (a new nullable column or table it does
not name).

**The boundary: going back holds only until the old keys are retired.** Moving a
mind onto its one key (story 884) replaces its old per-service keys at the
gateway and memory store. An older release's mind presents the old values, so:

- `release.sh` refuses to go to an older release while any mind is moved. It reads
  the console's report (`HIVE_SITE_URL`, `KEY_MIGRATION_TOKEN`); if the console
  is down, check by hand (`python3 tools/stateless/key_migration/key_migration.py
  report`) and pass `--minds-checked`.
- Put each moved mind back first:
  `python3 tools/stateless/key_migration/key_migration.py undo <mind>`, then
  recreate that mind. It registers the old values again from the file where they
  were kept.
- **After the old keys are retired (`key_migration.py retire`, item 842) undo is
  impossible and the previous release's minds have no accepted keys.** Going back
  then does not work for them. Retire only when you will not go back.

## Part 5. Moving minds onto their one key, and retiring the old keys

This is the operator work the code stories left. Do it after Part 2 and Part 3
hold, a mind at a time.

1. On the console set `KEY_MIGRATION_TOKEN` (any long random value); in your
   shell set the same and `HIVE_SITE_URL`.
2. `python3 tools/stateless/key_migration/key_migration.py report`. It lists every
   holder on the gateway, memory store and tools and every mind with a state.
   Names plainly any mind on another machine not yet registered (paste its key
   on its page), any mind sharing a key and any holder it cannot place. It shows digests only, never a value.
3. `... move <mind>` for the pilot mind, then recreate it, then check it works
   and an untouched control mind still does. Then the rest.
4. `... tools-credential hive-site` and one per surface bot. Each is written
   once to `TOOLS_TOKEN_<HOLDER>` in the shared env file. The gate cannot see
   what a consumer presents: repoint each bot's and the console's compose tools
   token at that variable and recreate them yourself.
5. `... retire` only when the report's `ready_to_retire` is true. It stops the
   shared tools token and the old per-service values. It cannot be undone.
6. A mind's key is replaced afterward by pasting a new one on its page; the
   services take it at once and the mind must be recreated to read it. The old
   key still bills at the proxy until whoever runs the proxy revokes it.

### Limits worth knowing before you move a mind

- **A moved mind's bot needs its own credential.** A mind on its one key no longer
  accepts the old gateway value from its bot on `/proactive`. Before you retire,
  give the mind and its bots one shared `MIND_PROACTIVE_TOKEN` (a new value, not
  the key; fresh installs already do). Without it the bot's polls are refused and
  the mind's unsolicited messages are never delivered.
- **The inference names are the mind's key.** The key pasted is
  the mind's inference key. A moved mind has `MIND_PROXY_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY` and `DSH_API_KEY` set to it at start,
  and the `runtime.yaml` `env:` block's values for those names are ignored
  (those are the names the claude, codex and dsh harnesses read to reach the
  proxy, so they cannot also carry something else). A mind that needs a real
  outside OpenAI or Anthropic key for something that is not its inference
  (a tool script, say) must keep that key under another name, for example
  `OUTSIDE_OPENAI_API_KEY`, and the script must read that name. A mind not yet
  moved is untouched. Move such a mind only after renaming.
- **The console runs as one process.** The lock that serialises key
  replacement, cut-off, move and retirement, and the key check and write inside
  mind creation (`_ROTATION_LOCK`)
  is an in-process lock. Do not run the console as several processes or
  replicas: two of them would not exclude one another and two operations on one
  key could interleave. This is how the console is deployed today.

## The work machine (the other machine)

Written, not performed. There, there is no installer stack from this repository
and no roster mind to do it, so the same parts apply by hand: Part 2 steps 1 to 8
and 11 on that machine's own compose files; Part 4 if something goes wrong; and
Part 5 for its minds. A mind there is moved by running Part 5 step 3 against
that machine's console, from that machine, with the key in hand. The hive-data
package and the chat surfaces install alone at a named tag, so a mind on another
machine needs only them:
`pip install "hive-data @ git+https://github.com/danielstewart77/hive-mind.git@hive-data-vX.Y.Z#subdirectory=modules/hive-data"`.

## A mind on another machine, and a person's own key

Daniel, 2026-11-25: "We should have a key for a user, just as we do for minds.
So if the user owns the mind, the user's key should be accepted when making
changes to the mind it owns." Built as feature 834 requirement 19.

- A person has one key, held like a mind's: a credential at the gateway with
  their username as holder and kind `user`, kept by the console in the shared
  env file as `USER_KEY_<NAME>`, made at random on their first need and shown to
  nobody. It opens no route at the gateway.
- When a signed-in person changes a mind the console already lets them change,
  the console presents their key to that mind. A mind accepts its own key (as
  before) and the key of its owner: it asks the gateway "is this key held by a
  user who owns me?" (`POST /broker/owner-key-check`, authenticated by the
  mind's own key, answered yes or no, owner as on the mind's registry row).
  A user's key is refused by every mind that user does not own, on this host or
  another machine, in-repo or edge. There is no administrator key.
- So a change to a mind on another machine works without the console ever
  holding that mind's key. It does need that machine to be able to reach the
  gateway (a mind that cannot ask answers 503, never open), and the mind to
  run this release. A person whose key is replaced
  (`POST /api/account/key/replace`, the signed-in person's own) has the old
  key refused by every mind at its next request.
- Retirement: a mind on another machine counts as moved when its pasted key
  (pasting it on the mind's page registers it at the gateway, the memory store
  and the tools; the console never stores it) is registered at all three. The
  report says plainly "registered, key not stored here". Until it is pasted,
  such a mind is "not moved" and `retire` stays blocked. The old per-service
  values of such a mind stay refused after `retire`, as for any mind.
- Limit: the console remembers "registered, key not stored here" in a small
  record beside the key journal (`/data/key-journal`, a volume), holding the
  mind's name and no key.

## An operator mind's key, and a mind's address (requirement 20)

Daniel, 2026-11-26: a mind's key is rejected for another mind's settings
"except if that mind is an operator"; the owner's key and an operator mind's key
are both kept at the gateway and accepted when making changes to other minds'
settings.

- An operator mind is a mind that holds the role with slug `operator`, read
  from the roles tables at every request (no cache). Its key is an ordinary
  mind key, held, revoked and cut off like any other. Remove the role and its
  key is an ordinary mind's key on its next request.
- The question a mind puts to the gateway ("is this key one I should accept?",
  `POST /broker/owner-key-check`) now answers yes for the owner's key and for
  the live key of an operator mind owned by the same person as the asking mind.
  Decision (Daniel did not say; the smallest safe reading): an operator mind
  reaches only minds owned by the same person as itself, as the registry
  records, never another person's mind and never an ownerless one.
- The gateway refuses (403, plain message) an ordinary mind's own key that
  re-registers a different `gateway_url` for an already registered mind, its
  own or another's, because the console sends a person's key to the registered
  address. A first registration, the same address again (boot registration) and
  a session-token refresh keep working. The admin and an operator mind of the
  same owner may change an address; an operator mind may not rename a mind.
- When a mind legitimately moves to a new address: the operator changes it
  with the admin credential (the console's mind page, or `POST /broker/minds`
  with the admin token) or from an operator mind of the same owner, before or
  at the move; then starts the mind at the new address. Until that, the mind's
  own boot registration with the new address is refused (403, in its log) and
  the gateway keeps sending to the old address.

## Things this does not do

- It does not pin base images, apt, npm, PyPI or llama.cpp; a build needs the
  network for them.
- It does not archive the old repositories (hive, terminal, hive-data,
  hive-surfaces, hive-tools): they stay, read-only to everything, until the
  work machine has moved and Daniel says so.
- It does not move the edge minds: the edge repository names no hive-mind
  release and needs nothing from this story. Its surfaces pin moves to the
  published wheel after the tag (Part 2, step 12).
