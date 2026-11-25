# The console and the terminal live in hive-mind

`modules/hive` (the console, with `hive_site`) and `modules/terminal` are
ordinary folders of hive-mind, history kept. They still deploy separately:
two compose files, two projects (`name: hive`, `name: terminal`), containers
`hive-site` (8710) and `terminal-site` (8711). Updating one never recreates
the other; `hive_site` reaches the terminal only when the terminal image is
rebuilt.

Build contexts are relative to each compose file: `../hive-data` and
`../hive/hive_site`. `HIVE_DATA_DIR` and `HIVE_SITE_DIR` are no longer read;
delete them from each site's `.env`.

## Operator steps

1. Copy each site's untracked `.env` and `data/` from the old checkout into
   `modules/hive/` and `modules/terminal/`. They are gitignored.
2. Mask them from every container that mounts the checkout. The installer
   and the tracked example compose do it with a tmpfs on
   `/usr/src/app/modules` (and `.git/modules`). A hand-kept compose (for
   example `minds/stash/container/compose.yaml`, which is gitignored) must
   get the same two tmpfs lines by hand.
3. `git pull` over an old clone of `modules/hive` or `modules/terminal` fails
   until the clone is moved aside; `installer/install.sh` does that when
   nothing is unsaved.
4. Untracked scripts naming `Dev/hive/.env` or `Dev/terminal/.env` (for
   example `.mint-services.py`) need the new paths.
5. Terminal image: tag `terminal-vX.Y.Z` on hive-mind publishes
   `ghcr.io/danielstewart77/terminal`; no other tag does.
