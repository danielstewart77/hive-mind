"""The one compose file and the one .env the installed hive runs from.

Generated under `$HIVE_HOME`, never the repositories' own compose files: those
carry one household's paths, cookie domain and GPU, and the hive on the
machine that wrote them mounts them live. This builds from the cloned
repositories' Dockerfiles instead.

Written as JSON, which is YAML, so the standard library can write it and
`docker compose config` reads it unchanged. Every secret is a `${NAME}`
reference into `.env`, so the live `.env` stays the one place a value lives
and a value changed there reaches every service that uses it.

Rules the rendered file keeps, each one a bug when it was not kept:
- project name `hive`, every volume named explicitly — a project or volume
  name derived from the folder changes when the folder does, and a Postgres
  volume that changes name is a new, empty database beside the old password;
- no service is rendered that the selection does not install, and nothing
  waits (`depends_on`) on one that is not rendered;
- nothing asks for a GPU;
- every bind mount's source lies under the install folder and is created
  before the first `up` (`prepare_mounts`), or Docker creates it root-owned;
- the host's web proxy reaches every build and container, with NO_PROXY
  covering every hive service so their own traffic never goes through it.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from installer import plan
from installer.adapters import dsh, mind_image, proxy as proxy_adapter, site, tools
from installer.paths import Home
from installer.host import Host

PROJECT = "hive"
NETWORK = "hivemind"

POSTGRES = "hive-postgres"
COMMS = "hive-comms"
LUCENT = "hive-lucent"
OLLAMA = "hive-ollama"
CONSOLE = "hive-site"
TERMINAL = "terminal-site"
VOICE = "hive-mind-voice"
TELEGRAM = "hive-telegram"
DISCORD = "hive-discord"

CONSOLE_PORT = 8710
TERMINAL_PORT = 8711
COMMS_HOST_PORT = 8426
LUCENT_HOST_PORT = 8425

COMMS_URL = f"http://{COMMS}:8424"
LUCENT_URL = f"http://{LUCENT}:8424"
OLLAMA_URL = f"http://{OLLAMA}:11434"
VOICE_URL = f"http://{VOICE}:8422"

POSTGRES_IMAGE = "pgvector/pgvector:pg17"
OLLAMA_IMAGE = "ollama/ollama:latest"
EMBEDDING_MODEL = "qwen3-embedding:8b"
EMBEDDING_DIMENSIONS = 4096

APP = "/usr/src/app"

#: Secrets the installer always makes.
BASE_SECRETS = (
    "COMMS_ADMIN_BEARER_TOKEN", "LUCENT_ADMIN_BEARER_TOKEN", "LUCENT_SOUL_BEARER_TOKEN",
    # The terminal's label token *is* comms' rename token: one value, two names.
    "COMMS_RENAME_TOKEN", "SMS_INBOUND_HMAC_SECRET",
    "HIVE_SECRET_KEY", "HIVE_SITE_ADMIN_TOKEN",
)

#: Comms holders minted for a service, by the piece that runs them. Role
#: `service`: each reads the mind registry, which a person holder may not.
COMMS_SERVICE_HOLDERS = {
    "console": CONSOLE, "terminal": TERMINAL, "voice": "voice-server",
    "telegram": "telegram-bot", "discord": "discord-bot", "tools": tools.SERVICE,
}
#: Comms itself reads lucent, as a service holder of its own.
LUCENT_SERVICE_HOLDERS = {"comms": COMMS, "console": CONSOLE}


def env_ref(name: str) -> str:
    return "${%s}" % name


def token_var(service: str, holder: str) -> str:
    return f"{service}_TOKEN_" + holder.upper().replace("-", "_")


def service_holders(answers: dict) -> dict[str, list[str]]:
    """{"comms": [...], "lucent": [...]}: the service holders this install mints."""
    pieces = plan.installed(answers)
    comms = [h for piece, h in COMMS_SERVICE_HOLDERS.items()
             if piece in pieces and (piece != "voice" or plan.mode(answers, "voice") == plan.HERE)]
    lucent = [h for piece, h in LUCENT_SERVICE_HOLDERS.items() if piece in pieces]
    return {"comms": comms, "lucent": lucent}


def secret_names(answers: dict) -> list[str]:
    """Every value the installer generates for this selection."""
    names = list(BASE_SECRETS)
    if plan.runs_here(answers, "postgres"):
        names.append("HIVE_PG_PASSWORD")
    if plan.runs_here(answers, "proxy"):
        names += ["PROXY_SESSION_SECRET", "PROXY_ADMIN_PASSWORD"]
    if "tools" in plan.installed(answers):
        names += list(tools.SECRETS)
    holders = service_holders(answers)
    names += [token_var("COMMS", h) for h in holders["comms"]]
    names += [token_var("LUCENT", h) for h in holders["lucent"]]
    mind = answers.get("mind_name")
    if mind:
        names += [token_var("COMMS", mind), token_var("LUCENT", mind)]
    return names


def database_url(answers: dict, values: dict) -> str:
    if plan.runs_here(answers, "postgres"):
        user, password, host, port, db = "hive", values["HIVE_PG_PASSWORD"], POSTGRES, "5432", "hive"
    else:
        user = answers["postgres_user"]
        password = values["POSTGRES_PASSWORD"]
        host, port, db = answers["postgres_host"], str(answers.get("postgres_port") or 5432), \
            answers.get("postgres_db") or "hive"
    q = urllib.parse.quote
    return f"postgresql://{q(user, safe='')}:{q(password, safe='')}@{host}:{port}/{q(db, safe='')}"


def proxy_url(answers: dict) -> str:
    if plan.runs_here(answers, "proxy"):
        return proxy_adapter.URL
    return str(answers["proxy_url"]).rstrip("/")


def embeddings_url(answers: dict) -> str:
    return OLLAMA_URL if plan.runs_here(answers, "embeddings") else \
        str(answers["embeddings_url"]).rstrip("/")


def voice_url(answers: dict) -> str | None:
    m = plan.mode(answers, "voice")
    if m == plan.HERE:
        return VOICE_URL
    if m == plan.EXISTING:
        return str(answers["voice_url"]).rstrip("/")
    return None


def links(answers: dict, host: Host) -> dict[str, str]:
    out = {}
    pieces = plan.installed(answers)
    if "console" in pieces:
        out["console"] = f"http://{host.address}:{CONSOLE_PORT}"
    if "terminal" in pieces:
        out["terminal"] = f"http://{host.address}:{TERMINAL_PORT}"
    return out


def env_values(home: Home, answers: dict, generated: dict[str, str], current: dict[str, str],
               extra: dict[str, str] | None = None) -> dict[str, str]:
    """Everything `.env` holds: generated secrets, what the operator gave,
    and values made by a later step (a proxy key, a tools token)."""
    values = {**current, **generated}

    def given(answer_key: str, env_name: str) -> None:
        value = str(answers.get(answer_key) or "").strip() or current.get(env_name, "")
        if value:
            values[env_name] = value

    given("admin_username", "HIVE_SEED_USERNAME")
    given("admin_password", "HIVE_SEED_PASSWORD")
    if not plan.runs_here(answers, "postgres"):
        given("postgres_password", "POSTGRES_PASSWORD")
    if not plan.runs_here(answers, "proxy"):
        given("proxy_key", "MIND_PROXY_KEY")
    pieces = plan.installed(answers)
    if "telegram" in pieces:
        given("telegram_token", "TELEGRAM_BOT_TOKEN")
    if "discord" in pieces:
        given("discord_token", "DISCORD_BOT_TOKEN")
    for name in ("MIND_PROXY_KEY", "HIVE_TOOLS_TOKEN"):
        if current.get(name) and name not in values:
            values[name] = current[name]
    if plan.runs_here(answers, "proxy"):
        values[proxy_adapter.ALLOW_CREATE] = current.get(proxy_adapter.ALLOW_CREATE, "true")
    values.update({k: v for k, v in (extra or {}).items() if v is not None})
    values["HIVE_DATABASE_URL"] = database_url(answers, values)
    values.setdefault("MIND_PROXY_KEY", "")
    values.setdefault("HIVE_TOOLS_TOKEN", "")
    values["TERMINAL_LABELS_TOKEN"] = values["COMMS_RENAME_TOKEN"]
    return values


@dataclass
class Rendered:
    compose: dict
    mounts: list[Path] = field(default_factory=list)
    #: Empty folders inside a read-only mount, for a nested mount to land on.
    mountpoints: list[Path] = field(default_factory=list)

    def text(self) -> str:
        return json.dumps(self.compose, indent=2, sort_keys=False) + "\n"


def render(home: Home, answers: dict, host: Host) -> Rendered:
    pieces = plan.installed(answers)
    services: dict[str, dict] = {}
    mounts: list[Path] = []
    mountpoints: list[Path] = []
    repo = home.repo
    hive_data = str(repo("hive-data"))
    dsn = env_ref("HIVE_DATABASE_URL")
    names = [POSTGRES, COMMS, LUCENT, OLLAMA, proxy_adapter.SERVICE, CONSOLE, TERMINAL, VOICE,
             tools.SERVICE, TELEGRAM, DISCORD]
    mind = answers.get("mind_name") or "mind"
    names.append(mind)
    web = host.proxy_env(names)
    tz = mind_image.tz(host)
    pg_here = plan.runs_here(answers, "postgres")
    voice = voice_url(answers)

    def bind(source: Path, target: str, mode: str = "rw") -> dict:
        mounts.append(source)
        return {"type": "bind", "source": str(source), "target": target,
                "read_only": mode == "ro"}

    def base(name: str, *, build: dict | None = None, image: str | None = None,
             env: dict | None = None, waits_on_db: bool = False, needs: tuple = ()) -> dict:
        svc: dict = {"container_name": name, "init": True, "restart": "unless-stopped",
                     "networks": [NETWORK],
                     "logging": {"driver": "json-file",
                                 "options": {"max-size": "20m", "max-file": "5"}}}
        if build is not None:
            build = dict(build)
            args = {**web, **build.pop("args", {})}
            if args:
                build["args"] = args
            svc["build"] = build
        if image:
            svc["image"] = image
        svc["environment"] = {**tz, **web, **(env or {})}
        deps = {}
        if waits_on_db and pg_here:
            deps[POSTGRES] = {"condition": "service_healthy"}
        for dep in needs:
            deps[dep] = {"condition": "service_started"}
        if deps:
            svc["depends_on"] = deps
        services[name] = svc
        return svc

    if pg_here:
        svc = base(POSTGRES, image=POSTGRES_IMAGE, env={
            "POSTGRES_DB": "hive", "POSTGRES_USER": "hive",
            "POSTGRES_PASSWORD": env_ref("HIVE_PG_PASSWORD"),
            "POSTGRES_INITDB_ARGS": "--data-checksums",
        })
        svc.pop("init")
        svc["volumes"] = [{"type": "volume", "source": "hive_postgres",
                           "target": "/var/lib/postgresql/data"}]
        svc["healthcheck"] = {
            "test": ["CMD-SHELL", "PGPASSWORD=\"$$POSTGRES_PASSWORD\" psql -h \"$$(hostname -i)\" "
                     "-U hive -d hive -c 'select 1' > /dev/null 2>&1"],
            "interval": "10s", "timeout": "5s", "retries": 12, "start_period": "30s"}

    ns = str(repo("hive-mind") / "nervous-system")
    svc = base(COMMS, image="hive/comms:local", waits_on_db=True, build={
        "context": ns, "dockerfile": "comms/Dockerfile",
        "additional_contexts": {"hive_data": hive_data}}, env={
        "COMMS_PORT": "8424", "COMMS_PROJECT_DIR": "/app", "HIVE_DATABASE_URL": dsn,
        "COMMS_ADMIN_BEARER_TOKEN": env_ref("COMMS_ADMIN_BEARER_TOKEN"),
        "COMMS_RENAME_TOKEN": env_ref("COMMS_RENAME_TOKEN"),
        "SMS_INBOUND_HMAC_SECRET": env_ref("SMS_INBOUND_HMAC_SECRET"),
        "LUCENT_URL": LUCENT_URL, "LUCENT_URL_HIVE": LUCENT_URL,
        "LUCENT_BEARER_TOKEN": env_ref(token_var("LUCENT", COMMS)),
        "LUCENT_SOUL_BEARER_TOKEN": env_ref("LUCENT_SOUL_BEARER_TOKEN"),
        "COMMS_PRIMARY_USER": env_ref("HIVE_SEED_USERNAME"),
    })
    svc["ports"] = [f"127.0.0.1:{COMMS_HOST_PORT}:8424"]
    svc["volumes"] = [bind(home.data / "comms", "/data")]

    lucent_env = {
        "LUCENT_API_PORT": "8424", "LUCENT_URL_SELF": "http://127.0.0.1:8424",
        "HIVE_DATABASE_URL": dsn, "OLLAMA_BASE_URL": embeddings_url(answers),
        "LUCENT_ADMIN_BEARER_TOKEN": env_ref("LUCENT_ADMIN_BEARER_TOKEN"),
        "LUCENT_SOUL_BEARER_TOKEN": env_ref("LUCENT_SOUL_BEARER_TOKEN"),
        "LUCENT_PRIMARY_USER": env_ref("HIVE_SEED_USERNAME"),
        "PRUNE_LOG_PATH": "/data/prune.log", "PRUNE_TIMEZONE": host.tz,
    }
    if "tools" in pieces:
        lucent_env.update({"HIVE_TOOLS_URL": tools.URL, "HIVE_TOOLS_TOKEN": env_ref("HIVE_TOOLS_TOKEN")})
    svc = base(LUCENT, image="hive/lucent:local", waits_on_db=True, build={
        "context": ns, "additional_contexts": {"hive_data": hive_data}}, env=lucent_env)
    svc["ports"] = [f"127.0.0.1:{LUCENT_HOST_PORT}:8424"]
    svc["volumes"] = [bind(home.data / "lucent", "/data")]

    if plan.runs_here(answers, "embeddings"):
        svc = base(OLLAMA, image=OLLAMA_IMAGE)
        svc["volumes"] = [{"type": "volume", "source": "hive_ollama", "target": "/root/.ollama"}]

    if plan.runs_here(answers, "proxy"):
        svc = base(proxy_adapter.SERVICE, image="hive/inference-proxy:local", waits_on_db=True,
                   build={"context": str(repo("inference-proxy"))},
                   env=proxy_adapter.environment(env_ref, dsn_ref=dsn))
        svc["healthcheck"] = {
            "test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen("
                     f"'http://127.0.0.1:{proxy_adapter.PORT}/health', timeout=3)"],
            "interval": "15s", "timeout": "5s", "retries": 8, "start_period": "20s"}

    link = links(answers, host)
    site_links = site.links(link.get("console", ""), link.get("terminal", ""))
    if "console" in pieces:
        env = {"HIVE_HOST": "0.0.0.0", "HIVE_PORT": str(CONSOLE_PORT), "HIVE_LOG_FORMAT": "json",
               "HIVE_DATABASE_URL": dsn, "HIVE_SECRET_KEY": env_ref("HIVE_SECRET_KEY"),
               "HIVE_SITE_ADMIN_TOKEN": env_ref("HIVE_SITE_ADMIN_TOKEN"),
               "COMMS_URL": COMMS_URL, "GATEWAY_API_URL": COMMS_URL,
               "COMMS_BEARER_TOKEN": env_ref(token_var("COMMS", CONSOLE)),
               "COMMS_ADMIN_BEARER_TOKEN": env_ref("COMMS_ADMIN_BEARER_TOKEN"),
               "LUCENT_API_URL": LUCENT_URL,
               "LUCENT_BEARER_TOKEN": env_ref(token_var("LUCENT", CONSOLE)),
               "LUCENT_SOUL_BEARER_TOKEN": env_ref("LUCENT_SOUL_BEARER_TOKEN"),
               "INFERENCE_PROXY_URL": proxy_url(answers),
               "INFERENCE_PROXY_KEY": env_ref("MIND_PROXY_KEY"),
               "OLLAMA_BASE_URL": embeddings_url(answers),
               "HOST_UID": str(host.uid), "HOST_GID": str(host.gid),
               **site_links, **site.seed(env_ref)}
        if voice:
            env["KOKORO_VOICE_URL"] = voice
        if "tools" in pieces:
            env.update({"HIVE_TOOLS_URL": tools.URL, "HIVE_TOOLS_TOKEN": env_ref("HIVE_TOOLS_TOKEN")})
        svc = base(CONSOLE, image="hive/console:local", waits_on_db=True, needs=(COMMS,), build={
            "context": str(repo("hive")), "additional_contexts": {"hive_data": hive_data}}, env=env)
        svc["ports"] = [f"0.0.0.0:{CONSOLE_PORT}:{CONSOLE_PORT}"]
        svc["volumes"] = [bind(home.data / "console", "/data"),
                          bind(home.minds, "/minds_hive_mind", "ro")]

    if "terminal" in pieces:
        env = {"TERMINAL_HOST": "0.0.0.0", "HIVE_DATABASE_URL": dsn,
               "HIVE_SECRET_KEY": env_ref("HIVE_SECRET_KEY"), "GATEWAY_API_URL": COMMS_URL,
               "COMMS_BEARER_TOKEN": env_ref(token_var("COMMS", TERMINAL)),
               "COMMS_ADMIN_BEARER_TOKEN": env_ref("COMMS_ADMIN_BEARER_TOKEN"),
               "TERMINAL_LABELS_TOKEN": env_ref("TERMINAL_LABELS_TOKEN"), **site_links}
        if voice:
            env["VOICE_API_URL"] = voice
        if "console" not in pieces:
            env.update(site.seed(env_ref))
        svc = base(TERMINAL, image="hive/terminal:local", waits_on_db=True, needs=(COMMS,), build={
            "context": str(repo("terminal")),
            "additional_contexts": {"hive_site": str(repo("hive") / "hive_site"),
                                    "hive_data": hive_data}}, env=env)
        svc["ports"] = [f"0.0.0.0:{TERMINAL_PORT}:{TERMINAL_PORT}"]

    mind_build = {"context": str(repo("hive-mind")), "args": mind_image.build_args(host)}
    if plan.mode(answers, "voice") == plan.HERE:
        svc = base(VOICE, image="hive/voice:local", needs=(COMMS,), build={
            **mind_build, "dockerfile": "Dockerfile.voice.kokoro"}, env={
            "TTS_ENGINE": "kokoro", "WHISPER_MODEL": "small", "COMMS_URL": COMMS_URL,
            "COMMS_BEARER_TOKEN": env_ref(token_var("COMMS", "voice-server"))})
        svc["working_dir"] = APP
        svc["volumes"] = [bind(repo("hive-mind"), APP, "ro"),
                          {"type": "volume", "source": "hive_voice_cache",
                           "target": "/home/hivemind/.cache"}]
        svc["tmpfs"] = ["/tmp"]
        svc["command"] = ["/opt/venv/bin/python3", "-m", "voice.voice_server"]

    if "tools" in pieces:
        svc = base(tools.SERVICE, image="hive/tools:local", waits_on_db=True, build={
            "context": str(repo("hive-tools")), "additional_contexts": {"hive_data": hive_data},
            "args": tools.build_args(host)}, env={
            **tools.environment(env_ref, dsn_ref=dsn, ollama_url=embeddings_url(answers),
                                model=str(answers.get("tools_ollama_model") or "qwen3:4b")),
            "COMMS_URL": COMMS_URL,
            "COMMS_BEARER_TOKEN": env_ref(token_var("COMMS", tools.SERVICE)),
            "LUCENT_URL": LUCENT_URL,
            "LUCENT_ADMIN_BEARER_TOKEN": env_ref("LUCENT_ADMIN_BEARER_TOKEN")})
        svc["volumes"] = [bind(home.data / "tools", "/app/data")]

    mind_dir = home.mind(mind)
    app_mind = f"{APP}/minds/{mind}"
    mountpoints.append(repo("hive-mind") / "minds" / mind)
    mind_env = {
        "MIND_NAME": mind, "MIND_ID": str(answers.get("mind_id") or ""),
        "HIVE_PROJECT_DIR": APP, "CLAUDE_CONFIG_DIR": f"{app_mind}/.claude",
        "CODEX_HOME": f"{app_mind}/.codex", "KEY_RING": f"{app_mind}/data/keyring",
        "PYTHONNOUSERSITE": "1", "PYTHONPATH": f"{APP}/vendor",
        "COMMS_URL": COMMS_URL, "HIVEMIND_BROKER_URL": COMMS_URL,
        "COMMS_BEARER_TOKEN": env_ref(token_var("COMMS", mind)),
        "HIVEMIND_BROKER_TOKEN": env_ref(token_var("COMMS", mind)),
        "COMMS_ADMIN_BEARER_TOKEN": env_ref("COMMS_ADMIN_BEARER_TOKEN"),
        "LUCENT_URL": LUCENT_URL, "LUCENT_URL_SELF": LUCENT_URL,
        "LUCENT_BEARER_TOKEN": env_ref(token_var("LUCENT", mind)),
        "SPECS_DIR": f"{APP}/specs/data-classes",
        "AUTO_REMEMBER_LOG_DIR": f"{app_mind}/data/auto-remember",
        **dsh.mind_environment(app_mind_dir=app_mind, proxy_url=proxy_url(answers),
                               key_ref=env_ref("MIND_PROXY_KEY")),
    }
    if "tools" in pieces:
        mind_env.update({"HIVE_TOOLS_URL": tools.URL, "HIVE_TOOLS_TOKEN": env_ref("HIVE_TOOLS_TOKEN")})
    svc = base(mind, image="hive/mind:local", needs=(COMMS, LUCENT), build=mind_build, env=mind_env)
    svc["container_name"] = f"hive-mind-{mind}"
    svc["working_dir"] = APP
    svc["volumes"] = [bind(repo("hive-mind"), APP, "ro"),
                      bind(mind_dir, app_mind),
                      bind(home.dsh, dsh.MOUNT, "ro")]
    svc["tmpfs"] = ["/tmp:exec,mode=1777"]
    svc["security_opt"] = ["no-new-privileges:true"]
    svc["command"] = ["/opt/venv/bin/python3", "-m", "minds.mind_server"]

    for piece, name, module, var in (("telegram", TELEGRAM, "surfaces.telegram", "TELEGRAM_BOT_TOKEN"),
                                     ("discord", DISCORD, "surfaces.discord", "DISCORD_BOT_TOKEN")):
        if piece not in pieces:
            continue
        holder = COMMS_SERVICE_HOLDERS[piece]
        env = {"MIND_ID": str(answers.get("mind_id") or ""), "COMMS_URL": COMMS_URL,
               "HIVE_MIND_SERVER_URL": COMMS_URL,
               "COMMS_BEARER_TOKEN": env_ref(token_var("COMMS", holder)),
               var: env_ref(var), "PYTHONNOUSERSITE": "1",
               "MIND_BACKEND_URL": f"http://{mind}:8420",
               "MIND_PROACTIVE_TOKEN": env_ref(token_var("COMMS", mind))}
        if voice:
            env.update({"VOICE_SERVER_URL": voice, "VOICE_SERVER_URL_KOKORO": voice})
        needs = (COMMS, mind) + ((VOICE,) if plan.mode(answers, "voice") == plan.HERE else ())
        svc = base(name, image="hive/mind:local", needs=needs, build=mind_build, env=env)
        svc["working_dir"] = APP
        svc["volumes"] = [bind(repo("hive-mind"), APP, "ro"),
                          bind(mind_dir, app_mind, "ro")]
        svc["tmpfs"] = ["/tmp"]
        svc["security_opt"] = ["no-new-privileges:true"]
        svc["command"] = ["/opt/venv/bin/python3", "-m", module]

    volumes = {}
    for svc in services.values():
        for vol in svc.get("volumes", []):
            if vol.get("type") == "volume":
                volumes[vol["source"]] = {"name": vol["source"]}
    doc = {"name": PROJECT, "services": services,
           "networks": {NETWORK: {"external": True, "name": NETWORK}}}
    if volumes:
        doc["volumes"] = volumes
    return Rendered(doc, mounts, mountpoints)


def prepare_mounts(rendered: Rendered) -> None:
    """Create every bind source and nested mountpoint before compose does.

    A missing source is created by the Docker daemon, as root, and then the
    service — running as the operator's uid — cannot write its own folder.
    """
    for path in [*rendered.mounts, *rendered.mountpoints]:
        path.mkdir(parents=True, exist_ok=True)


#: What a compose command may inherit from the installer's own environment.
#: Compose interpolates `${NAME}` from the shell before `.env`, so a variable
#: the operator happens to have exported — a bot token, a database URL from
#: another project — would silently replace the hive's own value.
COMPOSE_ENV_KEEP = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "XDG_RUNTIME_DIR",
                    "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "DOCKER_CERT_PATH",
                    "DOCKER_TLS_VERIFY", "DOCKER_BUILDKIT", "BUILDKIT_PROGRESS")


def compose_env() -> dict[str, str]:
    import os
    return {k: os.environ[k] for k in COMPOSE_ENV_KEEP if k in os.environ}


def compose_cmd(home: Home) -> list[str]:
    return ["docker", "compose", "-p", PROJECT, "-f", str(home.compose_file),
            "--env-file", str(home.env_file), "--project-directory", str(home.root)]
