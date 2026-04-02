"""Tests for audio path resolution."""

from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.audio import _resolve_audio_path


class TestResolveAudioPath:
    @pytest.mark.asyncio
    async def test_returns_local_path_when_exists(self, tmp_path):
        audio_file = tmp_path / "test.mp3"
        audio_file.write_bytes(b"fake mp3")

        import app.services.audio as audio_mod
        with patch.object(audio_mod, "AUDIO_DIR", tmp_path):
            episode = {"audio_filename": "test.mp3"}
            result = await _resolve_audio_path(episode)
            assert result == audio_file

    @pytest.mark.asyncio
    async def test_returns_none_when_no_filename(self):
        episode = {"audio_filename": None}
        result = await _resolve_audio_path(episode)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_file_missing_locally(self):
        episode = {"audio_filename": "not-on-disk.mp3"}
        result = await _resolve_audio_path(episode)
        assert result is None
