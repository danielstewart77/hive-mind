FROM ubuntu:24.04

WORKDIR /usr/src/app

# System deps + Node.js (for Claude Code CLI) + GitHub CLI
# jq required by the per-mind bash hooks under minds/<name>/.claude/hooks/ and .codex/hooks/.
# tmux hosts the browser terminal — a session's harness CLI runs inside it
# (minds/pty_attach.py), so a tile is a client rather than the conversation.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev gcc libpq-dev curl jq \
    nodejs npm \
    ffmpeg git tmux \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

# Non-root user — UID 1000 matches typical host user for bind-mount perms
# Ubuntu 24.04 ships with uid 1000 as 'ubuntu', so rename it
RUN usermod -l hivemind -d /home/hivemind -m ubuntu \
    && groupmod -n hivemind ubuntu \
    && mkdir -p /home/hivemind/.cache \
    && chown -R hivemind:hivemind /home/hivemind

# Claude Code CLI — baked into the image's default (root-owned) global npm
# dir; rebuild the image to update.
RUN npm install -g @anthropic-ai/claude-code

# Codex CLI — installs to a hivemind-owned prefix instead of npm's default
# root-owned global dir, so `npm install -g @openai/codex` also works at
# runtime as the non-root hivemind user. A docker-compose volume mounted at
# this exact path (see minds/nagatha and minds/bilby's container/compose.yaml)
# shares one codex install across every codex-harness container on a host —
# Docker seeds a fresh named volume from whatever's already here in the
# image, so this RUN doubles as the fallback baseline if that volume is
# ever stale or missing.
ENV NPM_CONFIG_PREFIX=/home/hivemind/.npm-global
ENV PATH="/home/hivemind/.npm-global/bin:${PATH}"
RUN npm install -g @openai/codex \
    && chown -R hivemind:hivemind /home/hivemind/.npm-global

# Python venv + deps — installed to /opt/venv so bind mounts don't clobber it
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip "setuptools<81" wheel
COPY requirements.txt .
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Playwright browsers (installed as root before USER switch, shared path)
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN /opt/venv/bin/playwright install --with-deps chromium

# App code (overridden by bind mount in dev, baked in for production)
COPY . .

RUN mkdir -p /usr/src/app/data \
    && chown -R hivemind:hivemind /usr/src/app

USER hivemind

EXPOSE 8420
CMD ["/opt/venv/bin/python3", "server.py"]
