# One repository: bringing a machine onto it, checking it, and going back

hive-mind now holds the data package, the chat surfaces, the console, the
terminal and the tools. Only inference-proxy and hive-harness-dsh
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
on that release, checks out the two outside repositories at the commits the
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
   inference-proxy and hive-harness-dsh to the commits the release
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
    `hive-data-v0.5.0` and `hive-surfaces-v0.2.0` (tag equals the package's
    `pyproject.toml`), copies `docs/setup/workflows/*.yml.txt` to
    `.github/workflows/` (the push token cannot write there), then tags the
    release. The edge minds' pin moves to the surfaces wheel after that.

## Part 3. Verify

- `scripts/release.sh status` shows the commit and the one it would go back to.
- `git -C <checkout> rev-parse HEAD` equals the tag's commit; each of the two
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
out, puts the two outside modules on the commits that tree records, rewrites
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
  the console's report (`HIVE_SITE_URL`, `HIVE_OPERATOR_KEY`); if the console
  is down, check by hand (`python3 tools/stateless/key_migration/key_migration.py
  report`) and pass `--minds-checked`.
- Put each moved mind back first:
  `python3 tools/stateless/key_migration/key_migration.py undo <mind>`, then
  recreate that mind. It registers the old values again from the file where they
  were kept, and removes the mind's key file (the recreated mind reads its old
  per-service values again).
- **After the old keys are retired (`key_migration.py retire`, item 842) undo is
  impossible and the previous release's minds have no accepted keys.** Going back
  then does not work for them. Retire only when you will not go back.

## Part 5. Moving minds onto their one key, and retiring the old keys

This is the operator work the code stories left. Do it after Part 2 and Part 3
hold, a mind at a time.

1. In your shell set `HIVE_SITE_URL` and `HIVE_OPERATOR_KEY`, an administrator's
   own key (the console has no setting for it; it recognises the key from the
   hive's record of keys, and a mind's key or a non-administrator's is refused).
2. `python3 tools/stateless/key_migration/key_migration.py report`. It lists every
   holder on the gateway, memory store and tools and every mind with a state.
   Names plainly any mind on another machine not yet registered (paste its key
   on its page), any mind sharing a key and any holder it cannot place. It shows digests only, never a value.
3. `... move <mind>` for the pilot mind. A container mind made before key files
   has no key folder mounted: the move writes its key to its own key file under
   the key root and adds the two compose lines (`MIND_KEY_FILE` and the read-only
   mount of its folder), and then **recreate it once** so it mounts the folder.
   Check it works and an untouched control mind still does. Then the rest. An
   installer-made install gets the key root (`<install>/keys`, one folder per
   mind) and `HOST_KEY_DIR` in its `.env` from the installer; a hand-kept install
   sets `HOST_KEY_DIR` to a folder outside the checkout and `HIVE_KEY_ROOT` on
   the console (read-write) to the same folder.
4. `... tools-credential hive-site` and one per surface bot. Each is written
   once to `TOOLS_TOKEN_<HOLDER>` in the shared env file. The gate cannot see
   what a consumer presents: repoint each bot's and the console's compose tools
   token at that variable and recreate them yourself.
5. `... retire` only when the report's `ready_to_retire` is true. It stops the
   shared tools token and the old per-service values. It cannot be undone.
6. A mind's key is replaced afterward by pasting a new one on its page; the
   services take it at once and the console writes it to the mind's key file,
   which the mind reads at its next request: **no restart and no recreate**. A
   mind on another machine is changed by a person placing the new key in the
   mind's key file there. The old key still bills at the proxy until whoever runs
   the proxy revokes it. Retirement also removes the `MIND_KEY_<NAME>` lines an
   earlier console kept in the shared `.env` (the file wins either way) once the
   mind's own compose mounts its key folder.

7. **The one-time step that ends every person's old key (story 914).** Before
   story 913 a person's key was a file under the key root (`.people`) and an
   entry in the hive's record of keys. Run this once, **after** this machine has
   been switched to the hive's own sign-in (the old settings `AUTH_SERVICE_URL`,
   `AUTH_SERVICE_SITE` and `HIVE_AUTH_BACKEND` removed, database step 20 applied,
   the sites, gateway, memory store and tools restarted together) and every
   person has an account and password in the hive database:
   `python3 tools/stateless/key_migration/key_migration.py end-person-keys`.
   It ends every person's key made the old way in the record, deletes the files
   under `.people`, and prints each person it ended. It is safe to run twice (the
   second run ends nobody). A mind's key and its folder are never touched.
   **Everyone signs in again afterwards**, and no old person's key works.
   Do not run it on a machine still signing in the old way: it would end keys
   that machine still uses.

### Limits worth knowing before you move a mind

- **A moved mind's bot presents the mind's own key.** The bot mounts the mind's
  key folder read-only and reads the key from it at each request, for the
  gateway, the voice servers and its mind's `/proactive` route. It holds no
  credential of its own (`MIND_PROACTIVE_TOKEN` and the bots' `COMMS_TOKEN_*`
  lines are read nowhere); a bot of a mind not yet moved is given the mind's
  gateway value as `MIND_KEY`.
- **A replacement changes the key everywhere.** A mind has one key. Replacing it
  on the mind's page rewrites its key file and every line of the install's
  `.env` that holds the same value (the installer's `MIND_PROXY_KEY`, and the
  hive's `SERVICE_PROXY_KEY` where it equals the mind's), in the same operation,
  and removes the console's own pre-write copies of the old value. The
  installer's `state/secrets.env` never keeps the mind's key. A replacement
  stops, changing nothing, if the mind's `runtime.yaml` or its container
  compose file holds the key literally: take it out there first. After it, no
  file under the install holds the old key.
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
- **A replaced key is not revoked everywhere at once. Say so plainly to
  whoever replaces one.** The old key survives, and still works where it is
  accepted, in four places until each is dealt with: (1) in a container that has
  not been recreated since it was created with the key in its environment (an
  older mind that has not had its key folder mounted yet keeps the creation-time
  value for as long as it runs; one recreate gives it the folder and ends that);
  (2) in any process already running that read it (a mind process reads the file
  at the next request, but a hive service that took the key from its environment,
  such as the services that share the mind's key as `SERVICE_PROXY_KEY`, keeps it
  in memory until it is restarted once); (3) at the inference proxy itself, which
  the console never calls: the old key bills there until whoever runs the proxy
  revokes it; (4) anywhere outside this install that somebody copied it. The
  replacement refuses the old key at the gateway, the memory store and the tools
  the moment it lands; it cannot reach the other four.
- **A refused request while a key is being replaced.** Between the services
  taking the new key and the mind's file being written, a request the mind makes
  with the old key can be refused once; a mind that cannot ask the gateway
  whether a presented key is its owner's answers 503 for that request and is
  asked again on the next. A caller presenting unknown keys to a mind is limited:
  a value the gateway said no to is answered no for thirty seconds without asking
  again, and the mind asks the gateway at most thirty such questions a minute,
  answering 503 beyond that (its own key and a key already accepted never wait on
  the gateway).
- **A name belongs to one mind identity.** A mind registering under a name that
  another identity (`mind_id`) already holds is refused, in any capitalisation;
  only the admin renames or removes a row.
- **The console runs as one process.** The lock that serialises key
  replacement, cut-off, move and retirement, and the key check and write inside
  mind creation (`_ROTATION_LOCK`)
  is an in-process lock. Do not run the console as several processes or
  replicas: two of them would not exclude one another and two operations on one
  key could interleave. This is how the console is deployed today.

## Part 6. What a mind may mount (requirement 33)

A mind that does not hold the operator role can read its own mind folder, its
own key folder and a clean copy of the code, and nothing else that is a secret:
not the install's settings file (`.env`, and any `*.env`), not the hive site's
or the tools', not the shared keyring in `data/`,
not the memory store's or the gateway's data, not another mind's folder, not
another mind's key folder, not the host's Docker socket or a login on the host.
It is handed no credential in its environment: its one key is the file named by
`MIND_KEY_FILE`. An operator mind may be given whatever its operator chooses,
and is never limited by this.

What produces it: the installer's container for a mind (`HOST_CODE_DIR` in the
`.env` is its own clean clone, `repos/hive-mind`), the console's container for a
mind made on the page (its own folder and key folder, and no second secret file),
and the tracked
example every hand-made mind is copied from (`minds/example/container/compose.yaml`:
the code from `HOST_CODE_DIR` read-only, its own folder, its own key folder, no
credential line). A mind that works on the hive's own code still has all of it,
read-only, without the secrets.

**The check.** From a clean checkout, as the operator:

```
python3 tools/stateless/mind_mounts/mind_mounts.py check --root <install folder>
```

It reads every mind's container file (`minds/*/container/compose.yaml` and the
installer's stack file), works out each mount with `${VARIABLES}` filled in from
the environment and `<root>/.env`, and lists every mind without the operator role
whose container mounts the install folder or its parent, a settings file, a key
folder that is not its own, another mind's folder, the Docker socket or a login
on the host, mounts a `session_token` file, takes an `env_file`, or is handed a
credential by name (a token, a key, a database address). A place the check cannot
work out is itself listed, never passed. It names the mind, the file, the line
and the reason, never a value. A secret that something hides inside the mount (an
empty file mounted over it with `/dev/null`, or a `tmpfs` on the folder) does not
count. It exits 1 when a mind without the operator role is listed, 0 when none is,
2 when it cannot run. Which minds hold the operator role is read from the hive
database (`HIVE_DATABASE_URL`: `board.mind_roles` joined to the role with slug
`operator`); `--operators <name or mind id>,...` names them instead; with neither
it stops rather than guess. `--map HOST=LOCAL` says where a host folder the files
name is on this machine; `HOST_PROJECT_DIR` is taken as the root and `HOST_DEV_DIR`
as its parent. `--secret <path>` adds a file of yours to what is guarded.
Nothing is `pending` now: a bot presents its mind's own key.

**The operator step for an existing machine** (Skippy applies it to this host
inside item 889; nothing here recreates or restarts a mind):

1. Run the check. Its list is the work.
2. Add `HOST_CODE_DIR` to the machine's `.env`: a clone of hive-mind that holds no
   `.env`, no keyring and no other minds' state (`scripts/modules.sh` and a plain
   `git clone` both make one; an installer-made install already has
   `repos/hive-mind`). Give it an empty `minds/<name>` folder for each mind, for the
   mind's own folder to be mounted on.
3. For each mind without the operator role, edit its untracked
   `minds/<name>/container/compose.yaml`: mount `${HOST_CODE_DIR}` read-only at
   `/usr/src/app` instead of the checkout root or the development folder; keep
   `minds/<name>` read-write at `/usr/src/app/minds/<name>`; set `KEY_RING` to
   `/usr/src/app/minds/<name>/data/keyring`; take out the `HOST_DEV_DIR` mount, the
   `session_token` mounts, every `*_BEARER_TOKEN`, `HIVE_TOOLS_TOKEN`,
   `COMMS_ADMIN_BEARER_TOKEN`, `BOARD_PUSH_TOKEN`, `HIVE_DATABASE_URL` and
   per-service proxy key line (the key folder carries the one key; Part 5
   moves the mind onto it first, or it will not be able to reach the hive), and any
   `env_file`.
4. Run the check again: it must exit 0 for every mind without the operator role.
5. Recreate each edited mind once, a mind at a time, and check it answers. This is
   the only point a container changes.

A mind that does not hold the operator role and works on the hive's own code
(Cypher, today) is covered by step 3: it sees the code through `HOST_CODE_DIR`,
read-only; its changes go through its own branch checkout under its own folder,
not through the install's.

Limits: `compose.override.yaml` files, `volumes_from` and `extends` are not merged
(the last two are listed as findings; an override file is passed to `--compose`
to be checked). The check reads container files, not running containers, so a container
started from an older file passes until it is recreated; it does not follow a
symlink inside a mounted folder it has no access to; and it does not move the
install's settings file (a later item).

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

- A person's key is made when they sign in (story 912) and lives only in their
  own browser, for twelve hours; its one-way scramble is a row in the hive
  database's record of keys. The hive site and the terminal site take the key
  that arrives with each click, use it for that request to the gateway, the
  memory store and the tools, and keep nothing: no file, nothing in memory
  after the request, nothing across a restart (story 913). Signing out, or the
  twelve hours passing, ends it everywhere at once; "Sign out everywhere" under
  Account settings ends every sign-in of the person's (an administrator can do
  it for anyone on Users and groups). Work that needs a person who is not there
  (finishing a started rename, taking back a mind whose creation was cut short,
  finishing a cut-short replacement of a mind's key, sending a saved design
  submission) waits, is shown as waiting with who it waits for, and is finished
  on the next click of that person or of any administrator, with that click's
  key. No code reads or writes a key file for a person any more (story 914);
  the old ones are ended and deleted by the one-time step in Part 5 (step 7).
- A person's change to a mind goes to the gateway, not to the mind: the hive
  site sends it with that person's own key to
  `/broker/minds/{mind}/forward/{path}`, the gateway decides (the mind's owner,
  an administrator, an operator mind of the same owner, the mind itself) and
  delivers it with the credential it presents to that mind on every call. A
  mind's own server accepts that credential and its own key, nothing else: a
  person's key or another mind's key sent straight to it is a plain 401 and the
  mind asks nobody (the old owner-key question route, `POST
  /broker/owner-key-check`, is gone). A mind that cannot establish that
  credential refuses with a plain 503; a mind with no key of its own still takes
  the gateway's delivery and refuses every other caller.
- So a change to a mind on another machine works without the console ever
  holding that mind's key, provided the gateway can reach the mind's registered
  address and the mind runs this release (an older mind answers its owner "could
  not ask the gateway"; update it first). A person whose key is replaced
  (`POST /api/account/key/replace`, the signed-in person's own) has the old key
  refused by the gateway at its next request.
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
- The gateway lets an operator mind change a mind owned by the same person as
  itself (the forward route; a mind's own server no longer sees that key).
  Decision (Daniel did not say; the smallest safe reading): an operator mind
  reaches only minds owned by the same person as itself, as the registry
  records, never another person's mind and never an ownerless one.
- The gateway refuses (403, plain message) an ordinary mind's own key that
  re-registers a different `gateway_url` for an already registered mind, its
  own or another's, because the console sends a person's key to the registered
  address. A first registration, the same address again (boot registration) and
  a session-token refresh keep working. The admin and an operator mind of the
  same owner may change an address; an operator mind may not rename a mind.
- Addresses compare as the same place when only case, the scheme's default port or a trailing slash differs. The role is global: whoever can grant the `operator` role to a mind gives it this reach over every mind of that mind's owner, and an operator mind may also move its own address. A role that cannot be read counts as not an operator.
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
