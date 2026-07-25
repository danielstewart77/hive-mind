# Container Hardening

## Current state — partial, not uniform

`docker-compose.example.yml` applies `security_opt: no-new-privileges`, `cap_drop: ALL`, and `read_only: true` to the surface bots (`telegram-bot`, `discord-bot`) and `tmpfs: /tmp` plus `no-new-privileges` + `read_only` (no `cap_drop` — see below) to the voice servers.

**Not hardened at all today:** mind containers (`minds/example/container/compose.yaml` has none of these), `lucent`, and `comms`. There is no "server container" anymore — `server.py` is deleted; the hardened bot services and the unhardened mind/lucent/comms services are simply different service definitions in the same compose file, not exceptions layered on one shared base.

### Voice servers — the one real compatibility exception

- Uses the `whisper-cache` (or `kokoro-cache`) named volume at `/home/hivemind/.cache` for model downloads. Without it, the first STT/TTS request crash-loops trying to write to a read-only filesystem.
- Omits `cap_drop: ALL` to preserve NVIDIA GPU runtime access (`--gpus all` / the `deploy.resources.reservations.devices` block).

### When Modifying Docker Config

- Never remove `no-new-privileges` or `read_only` from a hardened service without documenting why
- A newly-hardened service needs all three restrictions (`no-new-privileges`, `cap_drop: ALL`, `read_only: true`) plus `tmpfs: /tmp` if it writes anything at runtime
- If a service needs write access, prefer `tmpfs` or a named volume over removing `read_only`
- Test that containers start cleanly after changes — silent hangs are common with `read_only`

## Extending hardening to mind/lucent/comms

Open work, not yet done — see the tension noted in [docs/security/security-usability-tradeoffs.md](../docs/security/security-usability-tradeoffs.md#docker-hardening): mind containers, `lucent`, and `comms` are edited live far more often than the bots, and a restrictive read-only root would break that bind-mount-based workflow. No `docker-compose.production.yml` (named-volumes-only) exists today — production and development both run from the same bind-mounted compose file.
