"""A mind's picture, and the letter it wears until Daniel gives it one.

Daniel's protected behaviour, in his own words: an agent that has an icon
shows that agent's icon. The two halves of that are here — what a mind with
a picture is served, and what a mind without one is served instead — through
the route the chart actually asks, against real files on a real directory.

The only stub is the directory itself, which is the filesystem root and the
one place the standard allows one. Nothing here impersonates `mind_images`.
"""

from __future__ import annotations

import struct
import zlib

import pytest
from starlette.testclient import TestClient

import accounts
import auth
import main as main_mod
import mind_images
from hive_data import core
from hive_site import testing as suite

NOW = 1_800_000_000.0
MIND_ID = "3d3ae243-989b-41f0-87bd-5a79ef42c70c"


def _png(width: int = 8, height: int = 8) -> bytes:
    """A real PNG, built here so the header this is checked against is the
    one a decoder would read rather than a blob with a plausible prefix."""
    def chunk(tag: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _jpeg(width: int = 8, height: int = 8) -> bytes:
    """A JPEG whose frame header says this size.

    Hand-built because `_jpeg_size` walks the segment chain rather than
    reading a fixed offset, and that walk is the thing under test.
    """
    sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
           + struct.pack(">HH", height, width) + b"\x03"
           + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01")
    return b"\xff\xd8" + sof + b"\xff\xd9"


def _webp(width: int = 8, height: int = 8) -> bytes:
    """An extended-format WEBP whose canvas is this size. The stored size is
    one less than the real one, which is exactly the sort of detail a parser
    gets wrong silently."""
    body = (b"WEBP" + b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
            + (width - 1).to_bytes(3, "little")
            + (height - 1).to_bytes(3, "little"))
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.fixture(autouse=True)
def _image_dir(tmp_path, monkeypatch):
    """The store, pointed at a scratch directory.

    The filesystem root is the boundary of what is ours, so this is the one
    thing replaced. On the host that runs the console `IMAGE_DIR` is a real
    mounted path, and a suite that inherited it would write into it.
    """
    monkeypatch.setattr(mind_images, "IMAGE_DIR", tmp_path / "mind-images")


@pytest.fixture
def signed_in(monkeypatch):
    monkeypatch.setenv("HIVE_SECRET_KEY", "test-secret")
    user = accounts.make_user("image-admin", "secret-pass", is_admin=True,
                              replace=True)
    client = TestClient(main_mod.app)
    client.cookies.set(auth.SESSION_COOKIE_NAME, accounts.session_token(user))
    return client


@pytest.fixture
def registered():
    async def rows():
        await core.upsert_mind(
            mind_id=MIND_ID, name="Cypher",
            gateway_url="http://localhost:8426", model="m", harness="dsh",
            registered_at=NOW, last_seen=NOW)
    suite.run(rows())
    return MIND_ID


# ------------------------------------------------- a mind that has a picture


def test_a_mind_with_a_picture_is_served_that_picture(signed_in, registered):
    """The upload is what comes back, byte for byte, as its own type."""
    uploaded = _png()

    stored = signed_in.post(f"/api/minds/{registered}/image",
                            files={"file": ("face.png", uploaded, "image/png")})
    assert stored.status_code == 200, stored.text

    served = signed_in.get(f"/api/minds/{registered}/image")

    assert served.status_code == 200
    assert served.content == uploaded
    assert served.headers["content-type"] == "image/png"


def test_a_replacement_changes_the_version_the_card_asks_for(signed_in,
                                                             registered):
    """One URL per mind is a URL a browser may keep, so the version is what
    makes a replaced face actually appear.

    It replaces, rather than uploading once and checking the number is not
    zero: "a file exists" would satisfy a version that was any constant, and
    a constant is precisely the bug this guards.
    """
    assert mind_images.image_version(registered) == 0

    signed_in.post(f"/api/minds/{registered}/image",
                   files={"file": ("face.png", _png(8, 8), "image/png")})
    first = mind_images.image_version(registered)

    signed_in.post(f"/api/minds/{registered}/image",
                   files={"file": ("face.png", _png(16, 16), "image/png")})
    second = mind_images.image_version(registered)

    assert first != 0
    assert second != first


def test_replacing_a_png_with_a_gif_leaves_one_picture_not_two(signed_in,
                                                               registered):
    """Two files for one mind would leave the server choosing which face."""
    gif = (b"GIF89a" + struct.pack("<HH", 4, 4)
           + b"\x80\x00\x00\x00\x00\x00\xff\xff\xff,\x00\x00\x00\x00"
           + b"\x04\x00\x04\x00\x00\x02\x02D\x01\x00;")

    signed_in.post(f"/api/minds/{registered}/image",
                   files={"file": ("face.png", _png(), "image/png")})
    signed_in.post(f"/api/minds/{registered}/image",
                   files={"file": ("face.gif", gif, "image/gif")})

    directory = mind_images.IMAGE_DIR
    assert sorted(p.name for p in directory.iterdir()) == [f"{MIND_ID}.gif"]
    assert signed_in.get(
        f"/api/minds/{registered}/image").headers["content-type"] == "image/gif"


# ---------------------------------------------- a mind that has no picture


def test_a_mind_with_no_picture_is_served_its_initial_letter(signed_in,
                                                             registered):
    """The other half of Daniel's protection. Generated here rather than on
    the page, because the chart can only be handed an image."""
    served = signed_in.get(f"/api/minds/{registered}/image")

    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/svg+xml")
    # One letter, which is what Daniel asked for. Two — the convention on the
    # rest of this console — would render ">Cy<" and fail here.
    assert ">C<" in served.text, served.text


def test_the_letter_is_the_mind_s_own_initial(registered):
    """Called directly, so the letter is read off the registry name rather
    than off whatever the page happened to pass."""
    assert mind_images.initial_of("Nagatha") == "N"
    assert mind_images.initial_of("  stash") == "S"
    assert mind_images.initial_of("") == "?"


def test_a_name_carrying_markup_cannot_escape_the_generated_letter():
    """`core.minds.name` is unconstrained text and the letter is written into
    an SVG that is served to a browser."""
    svg = mind_images.initial_svg(MIND_ID, '"><script>alert(1)</script>')

    # The quote is what matters: the letter is written into an attribute, and
    # one character can never spell "<script" however badly it is handled.
    assert b"&quot;" in svg


# ------------------------------------------- which uploads become a picture
#
# Requirement added in phase 2 rather than one of Daniel's four protections,
# and named as such at ship time. It is here because the refusal is the
# control itself: minds on this hive are owned by people other than Daniel,
# and an accepted SVG executes script from this origin.


def test_an_svg_is_refused_as_a_mind_s_picture(signed_in, registered):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'

    refused = signed_in.post(f"/api/minds/{registered}/image",
                             files={"file": ("face.svg", svg, "image/png")})

    assert refused.status_code == 400
    assert "svg" in refused.text.lower()
    assert mind_images.stored_image(registered) is None


def test_the_format_comes_from_the_bytes_not_the_browser(signed_in,
                                                         registered):
    """The content type is caller-supplied, so it decides nothing."""
    refused = signed_in.post(
        f"/api/minds/{registered}/image",
        files={"file": ("face.png", b"not an image at all", "image/png")})

    assert refused.status_code == 400
    assert mind_images.stored_image(registered) is None


def test_an_image_too_many_pixels_a_side_is_refused(signed_in, registered):
    """A byte cap does not bound the decode: the chart decodes every card's
    picture on every redraw, and a phone tab dies rather than slows."""
    oversized = _png(width=mind_images.MAX_PIXELS + 1, height=4)

    refused = signed_in.post(
        f"/api/minds/{registered}/image",
        files={"file": ("big.png", oversized, "image/png")})

    assert refused.status_code == 400
    assert mind_images.stored_image(registered) is None


@pytest.mark.parametrize("name,build", [
    ("big.jpg", _jpeg),
    ("big.webp", _webp),
])
def test_the_pixel_cap_holds_for_every_format_not_just_png(signed_in,
                                                           registered,
                                                           name, build):
    """Each format stores its size differently and each has its own parser, so
    a cap proved against a PNG says nothing about the other three."""
    refused = signed_in.post(
        f"/api/minds/{registered}/image",
        files={"file": (name, build(mind_images.MAX_PIXELS + 1, 4),
                        "image/png")})

    assert refused.status_code == 400
    assert mind_images.stored_image(registered) is None


@pytest.mark.parametrize("name,good", [
    ("face.jpg", _jpeg), ("face.webp", _webp),
])
def test_an_ordinary_picture_in_every_format_is_accepted(signed_in, registered,
                                                         name, good):
    """The other side of the cap: a parser that read nothing would refuse
    everything, and a refusal-only test cannot tell that from working."""
    stored = signed_in.post(f"/api/minds/{registered}/image",
                            files={"file": (name, good(64, 48), "image/png")})

    assert stored.status_code == 200, stored.text
    assert mind_images.stored_image(registered) is not None


def test_a_picture_whose_header_cannot_be_measured_is_refused(signed_in,
                                                              registered):
    """The module's loudest rule: "I could not measure it" must never resolve
    to "it is fine". A PNG signature with no IHDR behind it is unmeasurable,
    and storing it unbounded is the failure."""
    headerless = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    refused = signed_in.post(
        f"/api/minds/{registered}/image",
        files={"file": ("broken.png", headerless, "image/png")})

    assert refused.status_code == 400
    assert mind_images.stored_image(registered) is None


def test_a_picture_over_the_byte_cap_is_refused(signed_in, registered):
    """Three megabytes, spelled out. Reading the cap off the module would
    build a payload that grew with it, so raising the cap would make this
    test raise its own bar and pass — which is the bug, not the guard."""
    oversized = _png() + b"\x00" * (3 * 1024 * 1024)

    refused = signed_in.post(
        f"/api/minds/{registered}/image",
        files={"file": ("huge.png", oversized, "image/png")})

    assert refused.status_code == 400
    assert mind_images.stored_image(registered) is None


def test_a_picture_for_a_mind_that_is_not_registered_is_refused(signed_in):
    """Otherwise the file is written and nothing will ever serve it."""
    stranger = "11111111-2222-3333-4444-555555555555"

    refused = signed_in.post(
        f"/api/minds/{stranger}/image",
        files={"file": ("face.png", _png(), "image/png")})

    assert refused.status_code == 404
    assert mind_images.stored_image(stranger) is None


def test_an_id_shaped_like_a_path_is_not_a_mind_id():
    """The id reaches a filename, so it is checked for being an id at all
    rather than cleaned up into something that looks like one."""
    assert not mind_images.valid_mind_id("../../etc/passwd")
    assert not mind_images.valid_mind_id("/absolute")
    assert mind_images.valid_mind_id(MIND_ID)
