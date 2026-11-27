FROM python:3.12-slim

# Install Docker CLI (for docker_ops tool), with buildx beside it. A mind the
# console creates is built from the published hive-mind tree, a git address,
# for one stage of its Dockerfile. Without buildx the CLI falls back to the
# classic builder, which needs a git of its own to fetch that address and
# then builds every stage above the one asked for, one of which wants a
# second build context a mind's build never names. With it, the engine
# fetches the tree and builds only the stage asked for.
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg && \
    install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
      https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      > /etc/apt/sources.list.d/docker.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
      docker-buildx-plugin && \
    apt-get purge -y curl gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# The app user, and a docker group whose GID is the host's, so the mounted
# socket is usable without root. All three are properties of the host the
# image runs on; the defaults are this workstation's.
ARG UID=1000
ARG GID=1000
ARG DOCKER_GID=983
RUN groupadd -o -g "$DOCKER_GID" docker && \
    groupadd -o -g "$GID" app && \
    useradd -u "$UID" -g app -G docker -m app

# Set up venv
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Install deps first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser + OS dependencies. Installed to a
# world-readable path so the unprivileged app user can launch the browser.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium && \
    chmod -R a+rX /ms-playwright

# The hive's data-access layer, from a build context of its own: hive-mind's
# modules/hive-data, beside this folder in the same checkout (docker-compose.yml
# names it). No git install and no credential in the build. `--no-deps` is
# deliberate - its dependencies are declared in requirements.txt above, where
# this image's resolver can see them beside everything else.
COPY --from=hive_data . /tmp/hive-data
RUN pip install --no-cache-dir --no-deps /tmp/hive-data && rm -rf /tmp/hive-data

# Copy application code
COPY server.py schemas.py hitl.py auth_api.py store.py hive_logging.py ./
COPY tools/ tools/

USER app

EXPOSE 9421

CMD ["python", "server.py"]
