"""What a Kokoro voice picker is allowed to offer.

The catalogue comes off the model repository's own file listing rather than a
table in our source, so what is guarded here is the decoding: which voices this
server's language can speak, what each one is called in words, and the
difference between an empty catalogue and one nobody could read.
"""

from __future__ import annotations

from voice import kokoro_catalogue

LISTING = [
    "config.json",
    "kokoro-v1_0.pth",
    "voices/af_heart.pt",
    "voices/af_bella.pt",
    "voices/am_michael.pt",
    "voices/bm_george.pt",
    "voices/bf_emma.pt",
    "voices/zf_xiaobei.pt",
    "voices/README.md",
    "samples/af_heart.wav",
]


class TestWhatTheServerCanSpeak:
    def test_offers_only_voices_in_the_running_language(self):
        """A voice outside the pipeline's language code produces nothing.

        The pipeline is built for one language, so offering a British voice on
        an American-English server is offering an option that silently fails.
        """
        names = [v.name for v in kokoro_catalogue.build_catalogue(LISTING, "a").voices]
        assert names == ["af_bella", "af_heart", "am_michael"]

    def test_a_british_server_offers_the_british_voices(self):
        names = [v.name for v in kokoro_catalogue.build_catalogue(LISTING, "b").voices]
        assert names == ["bf_emma", "bm_george"]

    def test_ignores_everything_in_the_repository_that_is_not_a_voice(self):
        assert kokoro_catalogue.voice_names_from_listing(LISTING) == [
            "af_bella",
            "af_heart",
            "am_michael",
            "bf_emma",
            "bm_george",
            "zf_xiaobei",
        ]


class TestLabels:
    def test_names_the_language_and_the_gender_in_words(self):
        """`bm_george` tells an operator nothing; the label has to say it."""
        voice = kokoro_catalogue.parse_voice_name("bm_george")
        assert (voice.language, voice.gender) == ("British English", "male")
        assert voice.label == "George — British English, male"

    def test_a_name_outside_kokoros_alphabet_is_not_a_voice(self):
        """Offered, it would be refused by the pipeline on every call."""
        assert kokoro_catalogue.parse_voice_name("qf_nobody") is None
        assert kokoro_catalogue.parse_voice_name("af-heart") is None


class TestUnreadable:
    def test_a_failed_listing_reports_unreadable_rather_than_empty(self):
        """Empty means this server has no voices, which is never true.

        The two states send the operator to different places — one to the
        network, one nowhere — so the fetch reports which it hit.
        """

        def explode(repo):
            raise OSError("no route to host")

        catalogue = kokoro_catalogue.fetch_catalogue("a", list_repo_files=explode)
        assert catalogue.readable is False
        assert catalogue.voices == []
        assert "no route to host" in catalogue.detail

    def test_a_readable_listing_reports_readable(self):
        catalogue = kokoro_catalogue.fetch_catalogue(
            "a", list_repo_files=lambda repo: LISTING
        )
        assert catalogue.readable is True
        assert [v.name for v in catalogue.voices] == [
            "af_bella",
            "af_heart",
            "am_michael",
        ]

    def test_the_wire_shape_carries_the_label_the_picker_renders(self):
        body = kokoro_catalogue.fetch_catalogue(
            "b", list_repo_files=lambda repo: LISTING
        ).as_dict()
        assert body["language_code"] == "b"
        assert {"name": "bm_george", "language": "British English",
                "gender": "male", "label": "George — British English, male"} in body["voices"]
