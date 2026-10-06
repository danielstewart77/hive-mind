"""What voices this Kokoro server can actually speak.

Kokoro's voices are not shipped with the package — each is a tensor fetched
from the model repository on first use — so the catalogue cannot be read off
local disk, and a table typed into this file would go stale the moment the
repository grew a voice. The repository's own file listing is the source.

A voice name encodes the two things an operator picking one needs to know and
cannot get from `af_aoede`: its language and whether it is a female or male
voice. Both are decoded here rather than left to a reader.

Every English voice is offered, American and British alike, and nothing else.
The server builds a pipeline per language on demand rather than one for the
whole process, so an accent is no longer a property of the deployment — which
is what used to make a British voice an option that silently failed. The other
seven languages stay out: this hive speaks English, and a picker holding sixty
options nobody will choose is a worse picker.

What a voice sounds like is not derivable from its name, so the repository's
own `VOICES.md` card travels with the listing: a published grade, how good the
reference recording was, and how much audio it was trained on. Nothing else —
the card's trait glyphs carry no legend, and naming them would be this file's
invention wearing the repository's authority. The card and the `voices/`
directory are two files that can disagree, so a voice missing from the card is
offered with no description rather than dropped.

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

#: The repository file describing each voice. Grades, not adjectives.
CARD_FILE = "VOICES.md"

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

#: The language codes this hive offers. English, in both its accents.
ENGLISH_CODES = ("a", "b")

#: Target Quality column. How good the reference recording was.
QUALITIES = {
    "A": "excellent source recording",
    "B": "good source recording",
    "C": "fair source recording",
    "D": "poor source recording",
    "F": "very poor source recording",
}

#: Training Duration column, in the card's own shorthand.
DURATIONS = {
    "HH hours": "ten to a hundred hours of training audio",
    "H hours": "one to ten hours of training audio",
    "MM minutes": "ten to a hundred minutes of training audio",
    "M minutes": "under ten minutes of training audio",
}

#: `af_heart`, `bm_george`, `zf_xiaobei`. Anything else is not a voice name.
VOICE_NAME_RE = re.compile(r"^([a-z])([fm])_([a-z]+)$")


@dataclass
class VoiceCard:
    """What the repository publishes about how one voice sounds.

    Grades rather than adjectives, because grades are what the card actually
    states. Nobody here has listened to twenty-eight voices, and a dropdown full
    of invented timbres would be twenty-eight small lies told confidently.

    The card's Traits column is glyphs with no legend anywhere in the file, so
    nothing is reported from it. Rendering a heart as "the flagship voice" or
    headphones as "recorded close-mic" is authorship presented to the operator
    as the repository's own words — which is the failure this class exists to
    avoid, not a smaller version of it. What a voice sounds like is answered by
    the Listen button.
    """

    grade: str = ""
    quality: str = ""
    duration: str = ""

    @property
    def description(self) -> str:
        """One short sentence an operator can choose from."""
        head = []
        if self.grade:
            head.append(f"Overall grade {self.grade}")
        if self.quality:
            head.append(QUALITIES.get(self.quality, "").strip())
        if self.duration:
            head.append(DURATIONS.get(self.duration, "").strip())
        sentence = ", ".join(piece for piece in head if piece)
        return f"{sentence}." if sentence else ""


@dataclass
class Voice:
    """One selectable voice, named in full."""

    name: str
    language: str
    gender: str
    card: VoiceCard = field(default_factory=VoiceCard)

    @property
    def given_name(self) -> str:
        _, _, given = self.name.partition("_")
        return given.title()

    @property
    def label(self) -> str:
        """What the picker shows: the voice's own name plus what it is."""
        return f"{self.given_name} — {self.language}, {self.gender}"

    @property
    def description(self) -> str:
        return self.card.description


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
    #: Whether the voice card was read. False means every description is empty
    #: because the card could not be fetched, not because the card is silent.
    described: bool = True

    def as_dict(self) -> dict:
        return {
            "language_code": self.language_code,
            "readable": self.readable,
            "detail": self.detail,
            "described": self.described,
            "voices": [
                {
                    "name": v.name,
                    "language": v.language,
                    "gender": v.gender,
                    "label": v.label,
                    "description": v.description,
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


def parse_voice_cards(card_text: str) -> dict[str, VoiceCard]:
    r"""Every voice row in `VOICES.md`, decoded into facts.

    The card is a set of markdown tables, one per language, whose rows carry a
    name, trait glyphs, a quality letter, a duration shorthand and an overall
    grade. Bold and escaped underscores are the file's own markup — `**af\_heart**`
    is the heart voice — and are stripped rather than treated as part of a name.

    Columns are located by reading each table's own header, never by position.
    A card that gains a column would otherwise shift every later cell by one and
    report a training duration as an overall grade — a confident sentence the
    operator is asked to choose on, which is worse than no sentence. A table
    whose header this function cannot read contributes nothing.

    A value that is not a grade or not a known quality letter is dropped rather
    than passed through, so a card that starts writing `B+` loses that clause
    instead of printing a phrase nothing defines. And the first row for a voice
    wins: an example row below the real table must not overwrite the data.
    """
    cards: dict[str, VoiceCard] = {}
    columns: dict[str, int] | None = None
    for line in (card_text or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            columns = None  # the table ended; the next one declares its own
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if columns is None:
            columns = _header_columns(cells)
            continue
        if not columns:
            continue
        name = _unmarkup(cells[columns["name"]]) if columns["name"] < len(cells) else ""
        if parse_voice_name(name) is None or name in cards:
            continue
        cards[name] = VoiceCard(
            grade=_grade(_cell(cells, columns, "grade")),
            quality=_quality(_cell(cells, columns, "quality")),
            duration=_normalise_duration(_cell(cells, columns, "duration")),
        )
    return cards


#: How each column this module reads is headed in the card.
_COLUMN_HEADINGS = {
    "name": "name",
    "quality": "target quality",
    "duration": "training duration",
    "grade": "overall grade",
}


def _header_columns(cells: list[str]) -> dict[str, int]:
    """Where each column this module reads sits in `cells`, or nothing."""
    headings = [_unmarkup(cell).lower() for cell in cells]
    found = {}
    for key, heading in _COLUMN_HEADINGS.items():
        if heading not in headings:
            return {}
        found[key] = headings.index(heading)
    return found


def _cell(cells: list[str], columns: dict[str, int], key: str) -> str:
    index = columns[key]
    if index >= len(cells):
        return ""
    return _unmarkup(cells[index]).replace("\u00a0", " ").strip()


#: `A`, `A-`, `B+`, `F+`. Anything else is not a grade this card is stating.
_GRADE_RE = re.compile(r"^[A-F][+-]?$")


def _grade(text: str) -> str:
    return text if _GRADE_RE.match(text) else ""


def _quality(text: str) -> str:
    """A quality letter `QUALITIES` can say something about, or nothing."""
    return text if text in QUALITIES else ""


def _unmarkup(cell: str) -> str:
    """A table cell with the file's own markdown taken off."""
    text = cell.replace("*", "").replace("\\", "")
    return " ".join(text.split()).strip()


def _normalise_duration(text: str) -> str:
    """The duration shorthand as `DURATIONS` keys it, or empty.

    The card marks a tiny training set with a glyph *inside* this column, so
    anything outside ASCII is dropped before matching — the duration itself
    already says "under ten minutes", and the glyph would stop it matching.
    """
    ascii_only = "".join(ch for ch in text if ch.isascii())
    collapsed = " ".join(ascii_only.replace("_", " ").split())
    for key in DURATIONS:
        if collapsed.lower() == key.lower():
            return key
    return ""


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


def build_catalogue(
    paths: list[str], language_code: str, card_text: str = "", described: bool = True
) -> Catalogue:
    """Every English voice in `paths`, described from `card_text`.

    `language_code` is the server's own default pipeline, reported for
    information; it no longer decides what is offered, because a pipeline is
    built per voice.
    """
    cards = parse_voice_cards(card_text)
    voices = []
    for name in voice_names_from_listing(paths):
        voice = parse_voice_name(name)
        if voice is None:
            continue
        if name[0] not in ENGLISH_CODES:
            continue
        voice.card = cards.get(name, VoiceCard())
        voices.append(voice)
    return Catalogue(
        language_code=(language_code or "a").strip().lower()[:1],
        voices=voices,
        described=described,
    )


def offers(catalogue: Catalogue, voice_name: str) -> bool:
    """Whether `voice_name` is one this server will speak in.

    An unreadable catalogue falls back to the name itself rather than refusing
    everything: a repository that cannot be listed should cost the picker its
    descriptions, not cost the operator the ability to hear a voice at all.
    """
    name = (voice_name or "").strip()
    if not name:
        return False
    if catalogue.readable:
        return any(v.name == name for v in catalogue.voices)
    parsed = parse_voice_name(name)
    return parsed is not None and name[0] in ENGLISH_CODES


def unreadable_catalogue(language_code: str, detail: str) -> Catalogue:
    """The catalogue nobody could read, which is not the empty catalogue."""
    return Catalogue(
        language_code=(language_code or "a").strip().lower()[:1],
        voices=[],
        readable=False,
        detail=detail,
    )


def read_voice_card() -> str:  # pragma: no cover - the real network
    """The repository's own `VOICES.md`, as text."""
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(VOICES_REPO, CARD_FILE)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def fetch_catalogue(
    language_code: str, *, list_repo_files=None, read_card=None
) -> Catalogue:
    """Read the repository listing and build the catalogue from it.

    `list_repo_files` and `read_card` are the seams for the network; the
    defaults are huggingface_hub's own. A listing that cannot be fetched comes
    back as an unreadable catalogue rather than raising, because the page asking
    for it needs an answer either way. A *card* that cannot be fetched costs the
    descriptions and nothing else — the voices are still speakable, and an
    operator picking by ear does not need the grades.
    """
    if list_repo_files is None:  # pragma: no cover - the real network
        from huggingface_hub import list_repo_files as list_repo_files
    if read_card is None:
        read_card = read_voice_card

    try:
        paths = list(list_repo_files(VOICES_REPO))
    except Exception as exc:
        log.warning("kokoro catalogue unreadable: %s", exc)
        return unreadable_catalogue(language_code, f"{type(exc).__name__}: {exc}")
    try:
        card_text = read_card()
        described = True
    except Exception as exc:
        log.warning("kokoro voice card unreadable: %s", exc)
        card_text, described = "", False
    return build_catalogue(paths, language_code, card_text, described=described)
