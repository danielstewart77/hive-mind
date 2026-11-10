"""What can be installed, what each piece needs, and what each choice asks.

Required pieces are always present: the database, comms, lucent, the
embedding service, the proxy, the dsh harness and one mind. The rest are
optional and the terminal starts ticked. Ticking a piece that needs another
ticks that one too (`closure`).

The database, the proxy and the embedding service are each installed here or
an existing one pointed at; voice is none, here, or existing. A choice adds
the questions it cannot answer itself (`questions`): the page asks for nothing
the installer can make.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HERE = "here"
EXISTING = "existing"
NONE = "none"


@dataclass(frozen=True)
class Piece:
    name: str
    label: str
    required: bool = False
    default: bool = False
    #: Pieces that must be present for this one to work.
    needs: tuple[str, ...] = ()
    #: Repositories whose code this piece is built from.
    repos: tuple[str, ...] = ()
    #: Where it may run; empty means "here", the only place.
    modes: tuple[str, ...] = ()


PIECES: dict[str, Piece] = {p.name: p for p in (
    Piece("postgres", "Database (Postgres + pgvector)", required=True,
          modes=(HERE, EXISTING)),
    Piece("comms", "Comms (gateway, broker, sessions)", required=True,
          needs=("postgres",), repos=("hive-mind", "hive-data")),
    # Lucent embeds through the proxy; the embedding service is the proxy's
    # local model backend.
    Piece("lucent", "Lucent (memory and knowledge graph)", required=True,
          needs=("postgres", "proxy"), repos=("hive-mind", "hive-data")),
    Piece("embeddings", "Embedding service (Ollama)", required=True,
          modes=(HERE, EXISTING)),
    Piece("proxy", "Inference proxy", required=True, needs=("postgres",),
          repos=("inference-proxy",), modes=(HERE, EXISTING)),
    Piece("dsh", "dsh harness", required=True, repos=("hive-harness-dsh", "hive-mind")),
    Piece("mind", "One mind", required=True,
          needs=("dsh", "comms", "lucent", "proxy"), repos=("hive-mind",)),
    # The terminal is built from the console's shared site code (hive_site),
    # which lives in the console's repository — not from the console service.
    Piece("terminal", "Web terminal", default=True, needs=("comms",),
          repos=("terminal", "hive", "hive-data")),
    Piece("console", "Console", needs=("comms", "lucent"),
          repos=("hive", "hive-data")),
    Piece("voice", "Voice server", needs=("comms",), repos=("hive-mind",),
          modes=(NONE, HERE, EXISTING)),
    Piece("tools", "Tools service", needs=("postgres", "embeddings"),
          repos=("hive-tools", "hive-data")),
    # A bot is claimed by its person on the console, which mints the pass
    # phrase it is claimed with; without the console nobody can claim it.
    Piece("telegram", "Telegram bot", needs=("comms", "console", "mind"),
          repos=("hive-mind",)),
    Piece("discord", "Discord bot", needs=("comms", "console", "mind"),
          repos=("hive-mind",)),
)}

#: The model provider kinds the proxy's `add-provider --kind` accepts.
PROVIDER_KINDS = ("anthropic", "openai", "ollama")

MIND_NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

#: The hive's own service and container names. A mind is a service named
#: after itself in a container named `hive-mind-<name>`, so a mind may take
#: none of these, nor the part after `hive-mind-` or `hive-` of any of them
#: (a mind named `voice` would be a second `hive-mind-voice`).
SERVICE_NAMES = (
    "hive-postgres", "hive-comms", "hive-lucent", "hive-ollama", "inference-proxy",
    "hive-site", "terminal-site", "hive-mind-voice", "hive-tools", "hive-telegram",
    "hive-discord",
)


def reserved_names() -> set[str]:
    out = {"shared", "mind", "example", *PIECES}
    for name in SERVICE_NAMES:
        out.add(name)
        for prefix in ("hive-mind-", "hive-"):
            if name.startswith(prefix):
                out.add(name[len(prefix):])
    return out


def defaults() -> dict:
    """The selection a first run starts from."""
    return {
        "pieces": sorted(p.name for p in PIECES.values() if p.required or p.default),
        "modes": {p.name: p.modes[0] for p in PIECES.values() if p.modes},
    }


def closure(selected) -> set[str]:
    """Every piece the selection needs: the ticked ones, what they need, and
    the required ones."""
    out = {p.name for p in PIECES.values() if p.required}
    pending = [*out, *selected]
    while pending:
        name = pending.pop()
        if name not in PIECES:
            raise ValueError(f"no such piece: {name!r}")
        out.add(name)
        pending.extend(n for n in PIECES[name].needs if n not in out)
    return out


def mode(answers: dict, piece: str) -> str:
    """Where a piece runs: here, existing, or (voice) none — the choice saved,
    else the piece's first."""
    p = PIECES[piece]
    if not p.modes:
        return HERE
    chosen = (answers.get("modes") or {}).get(piece)
    return chosen if chosen in p.modes else p.modes[0]


def installed(answers: dict) -> set[str]:
    """The pieces this answer set installs, after closure.

    A piece offered "none" is chosen by its mode alone: voice set to here or
    existing is installed, set to none it is not, whatever its tick says.
    """
    pieces = closure(answers.get("pieces") or ())
    for p in PIECES.values():
        if NONE in p.modes:
            if mode(answers, p.name) == NONE:
                pieces.discard(p.name)
            else:
                pieces |= closure([p.name])
    return pieces


def runs_here(answers: dict, piece: str) -> bool:
    return piece in installed(answers) and mode(answers, piece) == HERE


def repos(answers: dict) -> list[str]:
    """The repositories this install clones."""
    # hive-data and the console's repo always: the installer seeds the first
    # admin with the console's own site code (hive_site), whatever is ticked.
    names: set[str] = {"hive-mind", "hive-data", "hive"}
    for piece in installed(answers):
        if PIECES[piece].modes and mode(answers, piece) != HERE:
            continue
        names.update(PIECES[piece].repos)
    from installer.paths import REPOS
    return [r for r in REPOS if r in names]


@dataclass(frozen=True)
class Question:
    key: str
    label: str
    kind: str = "text"          # text | secret | int | choice
    choices: tuple[str, ...] = ()
    default: str = ""
    #: A secret asked again on a rerun may be left blank to keep the value
    #: the hive is using now.
    keep_blank: bool = field(default=False)
    optional: bool = False


def questions(answers: dict) -> list[Question]:
    """What the page must ask for this selection, in order."""
    qs = [
        Question("admin_username", "Console admin name"),
        Question("admin_password", "Console admin password", "secret", keep_blank=True),
        Question("mind_name", "The mind's name (lowercase, letters, digits, dashes)"),
    ]
    if mode(answers, "postgres") == EXISTING:
        qs += [
            Question("postgres_host", "Database host"),
            Question("postgres_port", "Database port", "int", default="5432"),
            Question("postgres_db", "Database name", default="hive"),
            Question("postgres_user", "Database user"),
            Question("postgres_password", "Database password", "secret", keep_blank=True),
        ]
    if mode(answers, "proxy") == EXISTING:
        qs += [
            Question("proxy_url", "Proxy address (e.g. http://10.0.0.5:8888)"),
            Question("proxy_key", "Proxy client key", "secret", keep_blank=True),
        ]
    else:
        # anthropic and openai need a key and default to their public
        # address; ollama needs an address and takes a key only if it has one.
        local = answers.get("provider_kind") == "ollama"
        qs += [
            Question("provider_kind", "Model provider", "choice", PROVIDER_KINDS),
            Question("provider_base_url", "Provider address"
                     + ("" if local else " (blank for the public one)"), optional=not local),
            Question("provider_api_key", "Provider key" + (" (if it has one)" if local else ""),
                     "secret", keep_blank=True, optional=local),
            Question("provider_model", "A model at that provider (its id)"),
            Question("provider_model_window", "That model's context window, in tokens",
                     "int", default="200000"),
        ]
    if mode(answers, "embeddings") == EXISTING:
        qs.append(Question("embeddings_url", "Embedding service address (Ollama)"))
    if mode(answers, "voice") == EXISTING:
        qs.append(Question("voice_url", "Voice server address"))
    pieces = installed(answers)
    if "tools" in pieces:
        qs.append(Question("tools_ollama_model", "Ollama model the tools service classifies with",
                           default="qwen3:4b"))
    if "telegram" in pieces:
        qs.append(Question("telegram_token", "Telegram bot token", "secret", keep_blank=True))
    if "discord" in pieces:
        qs.append(Question("discord_token", "Discord bot token", "secret", keep_blank=True))
    return qs


def validate(answers: dict, kept: set[str] = frozenset()) -> list[str]:
    """What is missing or malformed. `kept` names secrets the hive already has
    a value for, which may be left blank."""
    problems: list[str] = []
    try:
        installed(answers)
    except ValueError as exc:
        return [str(exc)]
    for q in questions(answers):
        value = str(answers.get(q.key) or q.default or "").strip()
        if not value:
            if q.optional or (q.keep_blank and q.key in kept):
                continue
            problems.append(f"{q.label}: required")
            continue
        if q.kind == "int" and not (value.isdigit() and int(value) > 0):
            problems.append(f"{q.label}: a whole number above zero")
        if q.kind == "choice" and value not in q.choices:
            problems.append(f"{q.label}: one of {', '.join(q.choices)}")
        if "'" in value or "\n" in value:
            problems.append(f"{q.label}: may not contain a single quote or a newline")
    name = str(answers.get("mind_name") or "")
    if name in reserved_names():
        problems.append(f"The mind's name: {name!r} is a name the hive uses itself")
    elif name and not MIND_NAME.match(name):
        problems.append("The mind's name: lowercase letters, digits and dashes, "
                        "starting with a letter, 2 to 31 characters")
    return problems
