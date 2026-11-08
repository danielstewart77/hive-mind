FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The accounts, sessions, store and authorization middleware this site
# shares with the console, from a build context of its own rather than from
# git: the repository is private, so installing it by URL would need a
# credential at build time. Installed, not bind-mounted: a mount would make
# this site run whatever branch the hive checkout has out, and a checkout of
# a branch without the package would stop it starting. A change to the
# shared auth reaches this site by a rebuild, deliberately.
# `--no-deps` is deliberate — fastapi, httpx, itsdangerous and bcrypt are
# declared in requirements.txt above, where this image's resolver can see
# them beside everything else.
COPY --from=hive_site . /tmp/hive-site
RUN pip install --no-cache-dir --no-deps /tmp/hive-site && rm -rf /tmp/hive-site

# The hive's data-access layer, needed by the shared store whenever this
# site is pointed at Postgres. Same reasoning: private repository, second
# build context, dependencies declared beside everything else.
COPY --from=hive_data . /tmp/hive-data
RUN pip install --no-cache-dir --no-deps /tmp/hive-data && rm -rf /tmp/hive-data

COPY src/ ./src/

ENV TERMINAL_HOST=0.0.0.0

CMD ["python", "src/main.py"]
