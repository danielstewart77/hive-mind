# Container Hardening

## Ring 3: Runtime Restrictions

All Python services in `docker-compose.yml` run with:
- `security_opt: no-new-privileges`
- `cap_drop: ALL`
- `read_only: true`
- `tmpfs: /tmp`

### Compatibility Exceptions

**Server container:**
- Adds `tmpfs: /home/hivemind:uid=1000,gid=1000` — Claude Code writes `.claude.json` at startup. Without this, the container hangs silently on an EROFS error.
- The `.claude` bind mount layers on top of the tmpfs, preserving keyring access.

**Voice server:**
- Uses `whisper-cache` named volume at `/home/hivemind/.cache` for Whisper model downloads. Without this, the first STT request crash-loops trying to download models to a read-only filesystem.
- Omits `cap_drop: ALL` to preserve NVIDIA GPU runtime access (`--gpus all`).

### When Modifying Docker Config
- Never remove `no-new-privileges` or `read_only` without documenting why
- New services must include all four base restrictions
- If a service needs write access, prefer `tmpfs` or named volumes over removing `read_only`
- Test that containers start cleanly after changes — silent hangs are common with `read_only`

## Ring 4: Named Volumes (Production)

The default `docker-compose.yml` includes host bind mounts for development (hot reload).

A separate `docker-compose.production.yml` (gitignored) removes host bind mounts — code is baked into the image. Usage:
```
docker compose -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

The `.claude` bind mount stays in both environments for keyring access.

