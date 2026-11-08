"""Accounts, sessions and the data store, shared by the console and the terminal.

Two modules, installed into both sites rather than copied into each:
`hive_site.store` for everything either site does to the hive's database,
and `hive_site.auth` for accounts and sessions on top of it. See README.md
for why the whole store moved rather than its accounts half.

Nothing is imported here. `store` opens a database and `auth` imports
fastapi, and a package `__init__` that pulled either in would make a bare
`import hive_site` cost both.
"""
