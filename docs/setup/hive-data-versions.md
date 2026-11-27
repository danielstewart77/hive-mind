# Versions of the modules that live in hive-mind

hive-mind holds several installable packages. They share one repository and so
one tag namespace; each is named by a prefix so a tag says what it releases.

| Package | Folder | Tag |
|---|---|---|
| hive-data | `modules/hive-data` | `hive-data-vX.Y.Z` |
| hive-surfaces | `modules/hive-surfaces` | `hive-surfaces-vX.Y.Z` |

Later modules that move in (the surfaces, the console, the terminal, the
tools) use the same form, `<name>-vX.Y.Z`, with their own prefix. A bare
`vX.Y.Z` is never used for a module, and a module's tag never triggers
another module's publish workflow.

The rule, in full, for a package:

1. The version is in the package's `pyproject.toml` and is `MAJOR.MINOR.PATCH`.
2. A change to the package's code raises it in the same pull request.
3. A release is a tag `<name>-vX.Y.Z` on the merge commit, and the tag's
   number equals the `pyproject.toml` number at that commit.
4. Anything that installs the package from git names a tag, with the
   subdirectory form:

   ```
   pip install "hive-data @ git+https://github.com/danielstewart77/hive-mind.git@hive-data-v0.5.0#subdirectory=modules/hive-data"
   ```

5. Because the version moves, `pip install --upgrade` does what it says. No
   `--force-reinstall` is needed.

## Cutting a hive-data release

From a clean checkout of the merge commit, with `pyproject.toml` at the new
number:

```
git tag -a hive-data-vX.Y.Z -m "hive-data X.Y.Z"
git push origin hive-data-vX.Y.Z
```

then refresh `modules/hive-data/tests/previous_release_columns.json` from that
release's `hive_data.schema.COLUMNS`.

## Moving to the in-repository package

A machine that cloned hive-data beside hive-mind, or whose `.env` sets
`HIVE_DATA_DIR`, no longer uses either: the compose file builds and mounts
`modules/hive-data`. Delete the `HIVE_DATA_DIR` line from `.env`, and run
`installer/install.sh` or `git pull` as usual (the installer moves an old
`modules/hive-data` clone aside first, and refuses if it holds unsaved work).
A per-machine `docker-compose.yml` (it is not tracked) takes the three
`${HIVE_DATA_DIR:-../hive-data}` lines from `docker-compose.example.yml`.

## Installing from a wheel (hive-surfaces)

pip's git subdirectory form initialises every submodule of hive-mind, so a
machine holding only pip and git can be better served by the wheel a tag
publishes. `scripts/build-package-release.py <package>-vX.Y.Z` builds the wheel
and sdist locally (refusing unless the tag's number is the `pyproject.toml`
number), and `.github/workflows/hive-surfaces-release.yml` attaches them to the
release when `hive-surfaces-vX.Y.Z` is pushed. Both install lines are in
`modules/hive-surfaces/README.md`.

A machine that cloned hive-surfaces beside hive-mind no longer uses it: the
compose file builds `modules/hive-surfaces` (the installer moves an old
`modules/hive-surfaces` clone aside first, as for hive-data). Until the edge
minds move (a later story) they keep installing from the old repository's
`main`, which stays in place.
