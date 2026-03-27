"""Tests for the slugify function and Swedish character handling."""

from app.database import slugify


class TestSlugify:
    def test_basic_text(self):
        assert slugify("Hello World") == "hello-world"

    def test_swedish_characters(self):
        assert slugify("Döden i Venedig") == "doden-i-venedig"
        assert slugify("Ångström och Ölänning") == "angstrom-och-olanning"
        assert slugify("Äventyr i Malmö") == "aventyr-i-malmo"

    def test_special_characters_removed(self):
        assert slugify("Avsnitt #42: Döden!") == "avsnitt-42-doden"

    def test_strips_leading_trailing_hyphens(self):
        assert slugify("---hello---") == "hello"

    def test_collapses_multiple_hyphens(self):
        assert slugify("hello   world") == "hello-world"

    def test_truncates_long_slugs(self):
        long_text = "a" * 200
        assert len(slugify(long_text)) <= 120

    def test_empty_string(self):
        assert slugify("") == ""

    def test_numbers_preserved(self):
        assert slugify("Avsnitt 175") == "avsnitt-175"

    def test_mixed_case(self):
        assert slugify("Kalle Lind Och Fredrik") == "kalle-lind-och-fredrik"
