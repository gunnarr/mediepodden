"""Tests for RSS feed parsing utilities (app/services/feed.py)."""

import hashlib

import pytest

from app.services.feed import make_audio_filename, parse_episode_number, strip_html


class TestMakeAudioFilename:
    """Tests for deterministic audio filename generation."""

    def test_with_episode_number_and_date(self):
        """Standard case: episode number + published date."""
        result = make_audio_filename(175, "2025-06-01")
        assert result == "2025-06-01-mediepodden-175.mp3"

    def test_with_episode_number_no_date(self):
        """Episode number present but no date."""
        result = make_audio_filename(42, None)
        assert result == "mediepodden-42.mp3"

    def test_with_episode_number_empty_date(self):
        """Episode number present, empty string date treated as falsy."""
        result = make_audio_filename(42, "")
        assert result == "mediepodden-42.mp3"

    def test_without_episode_number_uses_guid_hash(self):
        """When no episode number, use short md5 hash of guid."""
        guid = "patreon-guid-abc123"
        expected_hash = hashlib.md5(guid.encode()).hexdigest()[:8]
        result = make_audio_filename(None, "2025-06-01", guid=guid)
        assert result == f"2025-06-01-mediepodden-{expected_hash}.mp3"

    def test_without_episode_number_no_date_uses_guid_hash(self):
        """No episode number, no date, but guid available."""
        guid = "some-unique-id"
        expected_hash = hashlib.md5(guid.encode()).hexdigest()[:8]
        result = make_audio_filename(None, None, guid=guid)
        assert result == f"mediepodden-{expected_hash}.mp3"

    def test_without_episode_number_or_guid_uses_zero(self):
        """Fallback to '0' when neither episode number nor guid."""
        result = make_audio_filename(None, "2025-06-01")
        assert result == "2025-06-01-mediepodden-0.mp3"

    def test_without_anything(self):
        """Absolute fallback: no number, no date, no guid."""
        result = make_audio_filename(None, None)
        assert result == "mediepodden-0.mp3"

    def test_episode_number_zero_is_valid(self):
        """Episode number 0 should be treated as a valid number, not falsy."""
        result = make_audio_filename(0, "2025-01-01")
        assert result == "2025-01-01-mediepodden-0.mp3"

    def test_different_guids_produce_different_filenames(self):
        """Two different guids should produce different filenames."""
        name_a = make_audio_filename(None, "2025-06-01", guid="guid-a")
        name_b = make_audio_filename(None, "2025-06-01", guid="guid-b")
        assert name_a != name_b

    def test_same_guid_produces_same_filename(self):
        """Same guid should always produce the same filename (deterministic)."""
        name1 = make_audio_filename(None, "2025-06-01", guid="stable-guid")
        name2 = make_audio_filename(None, "2025-06-01", guid="stable-guid")
        assert name1 == name2

    def test_episode_number_takes_precedence_over_guid(self):
        """When episode_number is present, guid should be ignored."""
        result = make_audio_filename(100, "2025-06-01", guid="some-guid")
        assert result == "2025-06-01-mediepodden-100.mp3"
        assert "some-guid" not in result

    def test_filename_ends_with_mp3(self):
        """All filenames should end with .mp3."""
        assert make_audio_filename(1, "2025-01-01").endswith(".mp3")
        assert make_audio_filename(None, None).endswith(".mp3")
        assert make_audio_filename(None, None, guid="x").endswith(".mp3")


class TestParseEpisodeNumber:
    """Existing function — ensure no regressions from feed.py changes."""

    def test_mediepodden_format(self):
        assert parse_episode_number("Mediepodden 175 - AI och medier") == 175

    def test_avsnitt_format(self):
        assert parse_episode_number("Avsnitt 72: Nyheter") == 72

    def test_episod_format(self):
        assert parse_episode_number("Episod 25: Intervju") == 25

    def test_no_number(self):
        assert parse_episode_number("EXTRA - Bonusavsnitt") is None


class TestStripHtml:
    """Existing function — ensure no regressions."""

    def test_removes_tags(self):
        assert strip_html("<p>Hello</p>") == "Hello"

    def test_decodes_entities(self):
        assert strip_html("A &amp; B") == "A & B"
