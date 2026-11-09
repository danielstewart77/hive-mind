"""One module per interface another repository delivers.

Each states the interface it assumes in its docstring and keeps every name
that interface defines in one place, so when the real one lands differently
the change is here and nowhere else:

- `migrate`  (A) hive-data's `python -m hive_data.migrate check|apply --dsn`
- `site`     (B) hive_site's env: cookie, cross-links, first admin
- `tools`    (C) hive-tools' build args, env and token mint
- `proxy`    (D) inference-proxy's `python -m app.cli`
- `dsh`      (E) the dsh tree build, the profile generator, a dsh mind's keys
- `mind_image` (G) the mind image's build args and time zone
"""
