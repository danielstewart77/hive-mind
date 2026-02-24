FROM ubuntu:24.04

WORKDIR /usr/src/app

# System deps + Node.js (for Claude Code CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev gcc libpq-dev curl \
    nodejs npm \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/bin/python

# Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Non-root user — UID 1000 matches typical host user for bind-mount perms
# Ubuntu 24.04 ships with uid 1000 as 'ubuntu', so rename it
RUN usermod -l hivemind -d /home/hivemind -m ubuntu \
    && groupmod -n hivemind ubuntu \
    && mkdir -p /home/hivemind/.claude \
    && chown -R hivemind:hivemind /home/hivemind

# Python venv + deps — installed to /opt/venv so bind mounts don't clobber it
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --upgrade pip
COPY requirements.txt .
RUN /opt/venv/bin/pip install --no-cache-dir --force-reinstall agent_tooling
RUN /opt/venv/bin/pip install --no-cache-dir --force-reinstall -r requirements.txt

# Pre-download spaCy model (Kokoro/misaki needs it; can't pip-install at runtime as non-root)
RUN /opt/venv/bin/python -m spacy download en_core_web_sm

# App code (overridden by bind mount in dev, baked in for production)
COPY . .
RUN mkdir -p /usr/src/app/data \
    && chown -R hivemind:hivemind /usr/src/app

USER hivemind

EXPOSE 8420
CMD ["/opt/venv/bin/python3", "server.py"]
