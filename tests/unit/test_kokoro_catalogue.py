"""What a Kokoro voice picker is allowed to offer.

The catalogue comes off the model repository's own file listing and its own
voice card rather than a table in our source, so what is guarded here is the
decoding: that English means both accents and nothing else, what each voice is
called in words, what the card says about it, and the difference between an
empty catalogue and one nobody could read.
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
    "voices/ef_dora.pt",
    "voices/README.md",
    "samples/af_heart.wav",
]

#: A faithful excerpt of the repository's own `VOICES.md`, bold, escaped
#: underscores, trait glyphs and italic duration intact — the markup is what
#: the parser is for.
CARD = """# Voices

| Name | Traits | Target Quality | Training Duration | Overall Grade | SHA256 |
| ---- | ------ | -------------- | ----------------- | ------------- | ------ |
| **af\\_heart** | \U0001f6ba\u2764\ufe0f | | | **A** | `0ab5709b` |
| af_bella | \U0001f6ba\U0001f525 | **A** | **HH hours** | **A-** | `8cb64e02` |
| af_nicole | \U0001f6ba\U0001f3a7 | B | **HH hours** | B- | `c5561808` |
| am_santa | \U0001f6b9 | C | _M minutes_ \U0001f90f | D- | `7f2f7582` |
| bm_george | \U0001f6b9 | B | MM minutes | C | `f1bc8122` |
| zf_xiaobei | \U0001f6ba | | | C | `9b76be63` |
"""


class TestWhatTheServerCanSpeak:
    def test_offers_every_english_voice_in_both_accents(self):
        """Accent is a property of the voice, not of the deployment.

        A pipeline is built per voice, so a British voice on a server configured
        for American English speaks through a British front end rather than
        being hidden from the picker.
        """
        names = [v.name for v in kokoro_catalogue.build_catalogue(LISTING, "a").voices]
        assert names == ["af_bella", "af_heart", "am_michael", "bf_emma", "bm_george"]

    def test_offers_the_same_english_voices_whatever_the_server_language(self):
        for language in ("a", "b"):
            names = [
                v.name
                for v in kokoro_catalogue.build_catalogue(LISTING, language).voices
            ]
            assert names == [
                "af_bella", "af_heart", "am_michael", "bf_emma", "bm_george"
            ]

    def test_offers_nothing_from_the_other_languages(self):
        """This hive speaks English. Sixty options nobody will pick is worse."""
        names = [v.name for v in kokoro_catalogue.build_catalogue(LISTING, "a").voices]
        assert "zf_xiaobei" not in names
        assert "ef_dora" not in names

    def test_ignores_everything_in_the_repository_that_is_not_a_voice(self):
        assert kokoro_catalogue.voice_names_from_listing(LISTING) == [
            "af_bella",
            "af_heart",
            "am_michael",
            "bf_emma",
            "bm_george",
            "ef_dora",
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

        catalogue = kokoro_catalogue.fetch_catalogue(
            "a", list_repo_files=explode, read_card=lambda: CARD
        )
        assert catalogue.readable is False
        assert catalogue.voices == []
        assert "no route to host" in catalogue.detail

    def test_a_readable_listing_reports_readable(self):
        catalogue = kokoro_catalogue.fetch_catalogue(
            "a", list_repo_files=lambda repo: LISTING, read_card=lambda: CARD
        )
        assert catalogue.readable is True
        assert [v.name for v in catalogue.voices] == [
            "af_bella",
            "af_heart",
            "am_michael",
            "bf_emma",
            "bm_george",
        ]

    def test_the_wire_shape_carries_the_label_the_picker_renders(self):
        body = kokoro_catalogue.fetch_catalogue(
            "b", list_repo_files=lambda repo: LISTING, read_card=lambda: CARD
        ).as_dict()
        assert body["language_code"] == "b"
        assert {
            "name": "bm_george",
            "language": "British English",
            "gender": "male",
            "label": "George — British English, male",
            "description": (
                "Overall grade C, good source recording, ten to a hundred "
                "minutes of training audio."
            ),
        } in body["voices"]


class TestWhatTheCardSays:
    """Grades, because grades are what the repository actually publishes.

    Nobody here has listened to twenty-eight voices, so a described timbre
    would be invention. What the card states is real and is enough to pick on.
    """

    def test_reads_the_grade_the_quality_and_the_duration_into_words(self):
        card = kokoro_catalogue.parse_voice_cards(CARD)["af_bella"]
        assert card.grade == "A-"
        assert card.description == (
            "Overall grade A-, excellent source recording, ten to a hundred "
            "hours of training audio. The most popular voice."
        )

    def test_reads_a_bold_escaped_name_as_the_voice_it_names(self):
        """`**af\\_heart**` is the heart voice; the markup is not its name."""
        cards = kokoro_catalogue.parse_voice_cards(CARD)
        assert "af_heart" in cards
        assert cards["af_heart"].description == (
            "Overall grade A. The project's flagship voice."
        )

    def test_says_the_traits_in_words_rather_than_the_cards_glyphs(self):
        card = kokoro_catalogue.parse_voice_cards(CARD)["af_nicole"]
        assert card.description.endswith(
            "Recorded close-mic, best heard on headphones."
        )
        assert all(glyph not in card.description for glyph in kokoro_catalogue.TRAITS)

    def test_reads_an_italic_duration_carrying_the_cards_glyph(self):
        """The tiny-training glyph sits inside the duration cell itself."""
        card = kokoro_catalogue.parse_voice_cards(CARD)["am_santa"]
        assert card.duration == "M minutes"
        assert "under ten minutes of training audio" in card.description

    def test_a_voice_the_card_does_not_mention_is_still_offered(self):
        """The listing and the card are two files that can disagree.

        Dropping the voice would make a card lagging behind the repository look
        like a model that lost a voice.
        """
        catalogue = kokoro_catalogue.build_catalogue(LISTING, "a", CARD)
        emma = [v for v in catalogue.voices if v.name == "bf_emma"]
        assert emma and emma[0].description == ""

    def test_an_unreadable_card_costs_the_descriptions_and_nothing_else(self):
        def explode():
            raise OSError("no route to host")

        catalogue = kokoro_catalogue.fetch_catalogue(
            "a", list_repo_files=lambda repo: LISTING, read_card=explode
        )
        assert catalogue.readable is True
        assert [v.name for v in catalogue.voices] == [
            "af_bella", "af_heart", "am_michael", "bf_emma", "bm_george"
        ]
        assert all(v.description == "" for v in catalogue.voices)


class TestWhatMayBeSampled:
    def test_a_voice_in_the_catalogue_may_be_sampled(self):
        catalogue = kokoro_catalogue.build_catalogue(LISTING, "a", CARD)
        assert kokoro_catalogue.offers(catalogue, "bm_george") is True

    def test_a_voice_outside_the_catalogue_may_not(self):
        """Answering in the default tells the operator the wrong thing."""
        catalogue = kokoro_catalogue.build_catalogue(LISTING, "a", CARD)
        assert kokoro_catalogue.offers(catalogue, "zf_xiaobei") is False
        assert kokoro_catalogue.offers(catalogue, "af_nobody") is False
        assert kokoro_catalogue.offers(catalogue, "") is False

    def test_an_unreadable_catalogue_still_lets_an_english_voice_be_heard(self):
        """A repository that cannot be listed should not cost the Listen button."""
        catalogue = kokoro_catalogue.unreadable_catalogue("a", "no route to host")
        assert kokoro_catalogue.offers(catalogue, "bm_lewis") is True
        assert kokoro_catalogue.offers(catalogue, "zf_xiaobei") is False
