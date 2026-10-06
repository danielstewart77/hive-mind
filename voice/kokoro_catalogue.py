"""What voices this Kokoro server can actually speak.

Kokoro's voices are not shipped with the package — each is a tensor fetched
from the model repository on first use — so the catalogue cannot be read off
local disk, and a table typed into this file would go stale the moment the
repository grew a voice. The repository's own file listing is the source.

A voice name encodes the two things an operator picking one needs to know and
cannot get from `af_aoede`: its language and whether it is a female or male
voice. Both are decoded here rather than left to a reader, and the pipeline is
built for exactly one language code, so a voice outside it is not offered at
all — a British voice selected on an American-English server produces nothing,
and an option that cannot work is worse than an option that is absent.

"Could not read the catalogue" is its own state, never an empty list. One sends
the operator to the network; the other says this server has no voices, which is
never true.
"""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: The model repository whose `voices/` directory is the catalogue.
VOICES_REPO = "hexgrad/Kokoro-82M"

#: First letter of a voice name. Kokoro's own `lang_code` alphabet.
LANGUAGES = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Brazilian Portuguese",
    "z": "Mandarin Chinese",
}

#: Second letter.
GENDERS = {"f": "female", "m": "male"}

#: `af_heart`, `bm_george`, `zf_xiaobei`. Anything else is not a voice name.
VOICE_NAME_RE = re.compile(r"^([a-z])([fm])_([a-z]+)$")


@dataclass
class Voice:
    """One selectable voice, named in full."""

    name: str
    language: str
    gender: str

    @property
    def label(self) -> str:
        """What the picker shows: the voice's own name plus what it is."""
        _, _, given = self.name.partition("_")
        return f"{given.title()} — {self.language}, {self.gender}"


@dataclass
class Catalogue:
    """The answer to "what can this server speak", including not knowing.

    `readable` false means the listing could not be fetched. It is not the same
    as a readable catalogue holding nothing, and the two are never folded.
    """

    language_code: str
    voices: list[Voice] = field(default_factory=list)
    readable: bool = True
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "language_code": self.language_code,
            "readable": self.readable,
            "detail": self.detail,
            "voices": [
                {
                    "name": v.name,
                    "language": v.language,
                    "gender": v.gender,
                    "label": v.label,
                }
                for v in self.voices
            ],
        }


def parse_voice_name(name: str) -> Voice | None:
    """A voice name decoded into language and gender, or None if it is not one.

    A name whose language letter is outside Kokoro's alphabet is rejected
    rather than labelled "unknown": the picker would offer it, and the pipeline
    would refuse it.
    """
    match = VOICE_NAME_RE.match(name)
    if not match:
        return None
    language, gender, _ = match.groups()
    if language not in LANGUAGES or gender not in GENDERS:
        return None
    return Voice(name=name, language=LANGUAGES[language], gender=GENDERS[gender])


def voice_names_from_listing(paths: list[str]) -> list[str]:
    """The voice names in a repository file listing, in sorted order.

    Takes whole repository paths — `voices/af_heart.pt` — because that is what
    the listing returns, and ignores everything that is not a voice tensor.
    """
    names = set()
    for path in paths:
        directory, filename = posixpath.split(str(path))
        if posixpath.basename(directory) != "voices":
            continue
        stem, extension = posixpath.splitext(filename)
        if extension != ".pt":
            continue
        names.add(stem)
    return sorted(names)


def build_catalogue(paths: list[str], language_code: str) -> Catalogue:
    """The voices in `paths` that a pipeline on `language_code` can speak."""
    wanted = (language_code or "a").strip().lower()[:1]
    voices = []
    for name in voice_names_from_listing(paths):
        voice = parse_voice_name(name)
        if voice is None:
            continue
        if name[0] != wanted:
            continue
        voices.append(voice)
    return Catalogue(language_code=wanted, voices=voices)


def unreadable_catalogue(language_code: str, detail: str) -> Catalogue:
    """The catalogue nobody could read, which is not the empty catalogue."""
    return Catalogue(
        language_code=(language_code or "a").strip().lower()[:1],
        voices=[],
        readable=False,
        detail=detail,
    )


def fetch_catalogue(language_code: str, *, list_repo_files=None) -> Catalogue:
    """Read the repository listing and build the catalogue from it.

    `list_repo_files` is the seam for the network; the default is
    huggingface_hub's own. A listing that cannot be fetched comes back as an
    unreadable catalogue rather than raising, because the page asking for it
    needs an answer either way.
    """
    if list_repo_files is None:  # pragma: no cover - the real network
        from huggingface_hub import list_repo_files as list_repo_files

    try:
        paths = list(list_repo_files(VOICES_REPO))
    except Exception as exc:
        log.warning("kokoro catalogue unreadable: %s", exc)
        return unreadable_catalogue(language_code, f"{type(exc).__name__}: {exc}")
    return build_catalogue(paths, language_code)
