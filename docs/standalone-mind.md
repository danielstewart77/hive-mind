# Standalone Mind

A mind that runs as a single bare-metal systemd service on its own host instead of as a container in the multi-mind compose stack. Useful when:

- You want a mind to stay available while the rest of the hive restarts.
- The host can't or shouldn't run Docker.
- You want a "super-mind" with full host access (filesystem, systemd, package manager) for self-administration.

## Reference deployment

**👉 [github.com/danielstewart77/hive_mind_skippy](https://github.com/danielstewart77/hive_mind_skippy)** *(private)*

Skippy is the canonical standalone mind. The repo branched from `hive_mind` and runs as one systemd unit hosting:

- the mind backend (`mind_server.app`)
- the gateway (`server.app`)
- the embedded broker + session manager
- the Telegram bot

…all inside one Python launcher (`run_standalone.py`) that uses `asyncio.gather()` to host every FastAPI app and the bot in-process. One PID, one journal stream, `systemctl start <name>`.

## Memory wiring

A standalone mind talks to the shared `hive_nervous_system` container the same way a containerized mind does — over HTTP+bearer. The only difference is the URL: standalone minds can't use `http://hive-lucent:8424` (Docker DNS), so they use `http://127.0.0.1:8425` (the host-side bind on the shared container).

```
LUCENT_URL=http://127.0.0.1:8425        # standalone (host)
LUCENT_URL=http://hive-lucent:8424      # containerized (docker network)
```

Identity convention is the same. A standalone mind without a registry-issued UUID uses a stable literal string as its `MIND_AGENT_ID` (Skippy uses the literal `"skippy"`).

## When NOT to use this pattern

- **First mind in a fresh deployment.** Use a containerized mind. It's simpler.
- **Multiple minds on one host.** Containerized minds give you isolation for free; standalone is more work for no benefit.
- **A mind that doesn't need always-on uptime independent of the hive.** The whole point of standalone is to decouple the lifecycle.
