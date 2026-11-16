"""The picture a mind is drawn with, and the letter it wears until it has one.

Daniel uploads a mind's image on that mind's own page; the Chief of Staff
chart draws it in the upper-right corner of every card the mind is assigned
to. Until he uploads one the card shows the mind's initial letter instead,
and that fallback is generated *here* rather than on the page, so the page
has exactly one URL per mind and never has to decide what to draw when there
is no file. A client-side fallback would be a second implementation of the
same rule, in the one place that cannot test it.

Keyed by `mind_id`, never by name. The identity convention is explicit that
the short name is a display label: `core.upsert_mind` conflicts on `mind_id`
and rewrites `name`, so a rename would orphan a name-keyed file and leave the
card silently back on a letter — and a later mind registered under the freed
name would inherit a stranger's face. The id is also a uuid, which is why
there is no question here about a registry name reaching the filesystem;
`core.minds.name` is unconstrained text and would raise one.

SVG is refused. An uploaded SVG served from this origin executes script when
its URL is opened directly, and minds on this hive are owned by people other
than Daniel. The accepted formats are decided by the file's own first bytes
rather than by the browser's `Content-Type`, which is caller-supplied and
means nothing.

Pixel dimensions are read out of the file header and capped. A byte cap alone
does not bound them: a two-megabyte PNG can declare thirty thousand pixels a
side, and the chart decodes every card's image on every redraw, which is a
crashed tab on a phone rather than a slow one. A format whose header cannot
be parsed is refused rather than stored unbounded — "I could not measure it"
must never resolve to "it is fine".
"""

from __future__ import annotations

import os
import re
import struct
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

#: Where the uploaded files live. The console already mounts its own `./data`
#: read-write at `/data` for canvas state and harness runs, so this needs no
#: new mount and no container recreate. A module-level constant because the
#: suite points it at a temporary directory.
IMAGE_DIR = Path("/data/mind-images")

#: The largest upload accepted, before any decoding. Ten megabytes, and the
#: number is not load-bearing: nothing outside this module constrains it.
#: There is no reverse-proxy body limit in front of the console, no framework
#: cap, and no database column to fit — the bytes go to a file. The first
#: value here was two megabytes, chosen as "generous for a face", which it
#: was not: an ordinary exported PNG lands just over it and Daniel could not
#: upload a single one. What this cap is actually worth is small, so it is
#: set loosely. The decode cost of the chart is bounded by `MAX_PIXELS`
#: below, which is the rail that matters and is unchanged; the format check
#: in `sniff` is what keeps a non-image out. Raising this costs at most this
#: many bytes of transient memory per upload and this many bytes on disk per
#: mind, against a versioned, year-cached URL the browser fetches once per
#: replacement.
MAX_BYTES = 10 * 1024 * 1024

#: The largest image accepted, per side. A card draws the image at 28 pixels;
#: this is two orders of magnitude of headroom and still bounds the decode.
MAX_PIXELS = 4096

#: A mind id as `setup.sh` mints it and `core.minds` stores it: a uuid, with
#: or without its dashes. Checked before the id reaches a path at all, so a
#: traversal or an absolute path is refused as "not a mind id" rather than
#: sanitised into something that looks like one.
_MIND_ID_RE = re.compile(r"[0-9a-fA-F]{8}-?(?:[0-9a-fA-F]{4}-?){3}[0-9a-fA-F]{12}")

#: Extension and served content type per accepted format. The extension is
#: part of the stored filename so the served type is decided by what was
#: actually sniffed at upload rather than re-sniffed on every read.
_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
}


class ImageRefused(ValueError):
    """The upload is not an image this console will store or serve."""


def _mind_dir() -> Path:
    return IMAGE_DIR


def valid_mind_id(mind_id: Any) -> bool:
    """Whether this is a mind id, rather than something shaped like a path."""
    return bool(_MIND_ID_RE.fullmatch(str(mind_id or "")))


# ---------------------------------------------------------------- sniffing


def _png_size(data: bytes) -> tuple[int, int] | None:
    # The IHDR chunk is mandatory and must come first, so width and height
    # are at fixed offsets. 8 bytes of signature, 4 of length, 4 of type.
    if len(data) < 24:
        return None
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10:
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _webp_size(data: bytes) -> tuple[int, int] | None:
    # Three encodings behind one RIFF container, each storing the canvas
    # size differently. Returning None for an encoding this does not know is
    # what makes an unparseable WEBP a refusal rather than an unbounded one.
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        # A lossy keyframe: a three-byte start code, then two 16-bit fields
        # whose top two bits are a scale factor rather than size.
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    # Walk the segment chain to the frame header. The size is not at a fixed
    # offset in a JPEG: EXIF, ICC profiles and thumbnails all sit in front of
    # it, and a phone photo carries all three.
    index = 2
    end = len(data)
    while index + 9 <= end:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        # `0xFF` before a marker is legal fill and several encoders emit it.
        # Read as a marker it takes the next two bytes for a length, the walk
        # jumps past the frame header, and a photo every other tool opens is
        # refused as unmeasurable.
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9 or marker == 0xDA:
            return None
        length = int.from_bytes(data[index + 2:index + 4], "big")
        if length < 2:
            return None
        # Any of the start-of-frame markers; the four that are not frames
        # (DHT, JPG, DAC, and the restart-interval marker) are skipped.
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        index += 2 + length
    return None


def sniff(data: bytes) -> tuple[str, int, int]:
    """The format, width and height of an upload, from its own bytes.

    Raises `ImageRefused` for anything that is not one of the four accepted
    raster formats, and for an accepted format whose header cannot be read.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        kind, size = "png", _png_size(data)
    elif data[:3] == b"\xff\xd8\xff":
        kind, size = "jpg", _jpeg_size(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind, size = "webp", _webp_size(data)
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        kind, size = "gif", _gif_size(data)
    elif data[:5] == b"<?xml" or b"<svg" in data[:256].lower():
        raise ImageRefused(
            "an SVG is refused as a mind's image. Served from this origin it "
            "runs script when its own URL is opened. Upload a PNG, JPEG, "
            "WEBP or GIF.")
    else:
        raise ImageRefused(
            "that file is not a PNG, JPEG, WEBP or GIF. The format is read "
            "from the file's own bytes, so renaming it does not help.")

    if size is None:
        raise ImageRefused(
            f"the dimensions of that {kind.upper()} could not be read from "
            "its header, so there is nothing to check them against. An image "
            "this cannot measure is refused rather than stored.")
    width, height = size
    if width < 1 or height < 1:
        raise ImageRefused(f"that image reports no size ({width}x{height}).")
    if width > MAX_PIXELS or height > MAX_PIXELS:
        raise ImageRefused(
            f"that image is {width}x{height}; the limit is "
            f"{MAX_PIXELS}x{MAX_PIXELS} per side. Every card on the chart "
            "decodes it on every redraw.")
    return kind, width, height


def content_type(path: Path) -> str:
    """What a stored file is served as, from the extension it was stored under."""
    return _TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream")


# ------------------------------------------------------------------ storage


def stored_image(mind_id: str) -> Path | None:
    """This mind's uploaded image, or None when it has none."""
    if not valid_mind_id(mind_id):
        return None
    directory = _mind_dir()
    found = []
    for extension in _TYPES:
        candidate = directory / f"{mind_id}.{extension}"
        try:
            found.append((candidate.stat().st_mtime_ns, candidate))
        except OSError:
            continue
    if not found:
        return None
    # Newest, not first by extension. Two files for one mind is supposed to
    # be impossible, but the cleanup that guarantees it can fail — a dead
    # process, a read-only mount — and picking by declaration order would
    # then serve the *replaced* picture for good, with a version that never
    # changes, so nothing on the page could ever notice. Newest-wins makes
    # that failure invisible instead of permanent.
    found.sort()
    return found[-1][1]


def image_version(mind_id: str) -> int:
    """A number that changes when this mind's image does, or 0 when it has none.

    Carried in the URL the chart asks for, because one fixed URL per mind is
    a URL a browser is entitled to keep: a replaced face would otherwise go
    on showing the old one for as long as the heuristic cache felt like it,
    with nothing on the page to say so.
    """
    path = stored_image(mind_id)
    if path is None:
        return 0
    try:
        return path.stat().st_mtime_ns
    except OSError:
        # The file went between the listing and the stat — a replacement in
        # another format removes the old one, and that is a real window. This
        # is called once per mind per task while building the whole tree, so
        # an exception here fails the entire page rather than one corner of
        # one card. No version reads the same as no picture.
        return 0


def save_image(mind_id: str, data: bytes) -> Path:
    """Store an upload as this mind's image, replacing whatever it had.

    A replacement, unlike a voice clip, which is refused: Daniel's
    requirement is that he uploads *or replaces* the picture, and there is
    only ever one of them. Any file this mind had under another format is
    removed in the same breath, so a JPEG replacing a PNG cannot leave two
    on disk for the reader to choose between.

    Staged under a dotted name and renamed into place. The browser fetches
    these on every redraw of the chart, and a file written in place is one a
    reader can get half of.
    """
    if not valid_mind_id(mind_id):
        raise ImageRefused(f"not a mind id: {mind_id!r}")
    if len(data) > MAX_BYTES:
        raise ImageRefused(
            f"that file is {len(data)} bytes; the limit is {MAX_BYTES}.")
    if not data:
        raise ImageRefused("that file is empty.")

    kind, _, _ = sniff(data)
    directory = _mind_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{mind_id}.{kind}"
    staging = directory / f".{mind_id}.{os.getpid()}.{uuid.uuid4().hex}.incoming"
    try:
        staging.write_bytes(data)
        os.replace(staging, target)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    # After the rename, and deliberately not inside its try: the new picture
    # is already in place and is the one that counts. A failure to remove the
    # old one is untidy, not a failed upload, and `stored_image` picks the
    # newest so the reader still sees what was just uploaded.
    for extension in _TYPES:
        if extension == kind:
            continue
        with suppress(OSError):
            directory.joinpath(f"{mind_id}.{extension}").unlink(missing_ok=True)
    return target


# ----------------------------------------------------------------- fallback

#: The palette the generated letters are drawn from. Picked by the mind's own
#: id so one mind keeps one colour across reloads and two minds beside each
#: other are usually told apart by more than their letter.
_INK = (
    ("#16353a", "#4cc9d9"), ("#163326", "#5bc88a"), ("#3b1d1e", "#ef746f"),
    ("#2e1065", "#a78bfa"), ("#1e1b4b", "#818cf8"), ("#3a2a08", "#d6ad54"),
)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("'", "&#39;"))


def initial_of(name: str) -> str:
    """The one letter a mind wears until Daniel gives it a picture.

    Daniel's requirement says the mind's initial letter, singular. The rest
    of this console draws two (`name.slice(0, 2)` on the minds, edge-minds
    and overview pages); that disagreement is deliberate rather than
    overlooked, because it is his word for it and the letter is temporary by
    construction - it is what a mind shows only until it has a face.
    """
    stripped = "".join(
        c for c in str(name or "") if c.isprintable()).strip()
    # A control character, a combining mark or a bidi override is not a
    # letter anyone can read: one of them as the whole glyph draws an empty
    # badge, or in the case of U+0000 an SVG that is not well-formed and
    # draws nothing at all, which reads as the chart being broken.
    return stripped[0].upper() if stripped else "?"


def initial_svg(mind_id: str, name: str) -> bytes:
    """The generated stand-in: one letter, on a colour this mind keeps.

    The letter is escaped. `core.minds.name` is unconstrained text, and a
    name carrying an angle bracket would otherwise open a tag inside the
    document this is served as. It lands in element content rather than in an
    attribute — worth being exact about, because the next person deciding
    what else may go into this template will read this sentence first.
    """
    letter = _escape(initial_of(name))
    seed = sum(str(mind_id or "").encode()) if mind_id else 0
    back, fore = _INK[seed % len(_INK)]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" '
        'viewBox="0 0 96 96" role="img">'
        f'<rect width="96" height="96" rx="20" fill="{back}"/>'
        f'<text x="48" y="48" fill="{fore}" font-size="54" font-weight="600" '
        'font-family="DM Sans, system-ui, sans-serif" text-anchor="middle" '
        f'dominant-baseline="central">{letter}</text>'
        "</svg>"
    ).encode()
