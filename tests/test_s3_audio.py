"""Tests for audio source resolution: local-disk fast path, MinIO fallback, temp cleanup."""

from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

from app.services import storage
import app.services.audio as audio_mod
from app.services.audio import _audio_file


class TestAudioFileLocalPath:
    @pytest.mark.asyncio
    async def test_yields_local_path_when_file_exists(self, tmp_path):
        audio_file = tmp_path / "test.mp3"
        audio_file.write_bytes(b"fake mp3")

        with patch.object(audio_mod, "AUDIO_DIR", tmp_path):
            episode = {"audio_filename": "test.mp3"}
            async with _audio_file(episode) as p:
                assert p == audio_file

    @pytest.mark.asyncio
    async def test_yields_none_when_no_filename(self):
        episode = {"audio_filename": None}
        async with _audio_file(episode) as p:
            assert p is None

    @pytest.mark.asyncio
    async def test_yields_none_when_local_missing_and_storage_off(self):
        episode = {"audio_filename": "not-on-disk.mp3"}
        with patch.object(storage, "is_configured", return_value=False):
            async with _audio_file(episode) as p:
                assert p is None


class TestAudioFileMinioFallback:
    @pytest.mark.asyncio
    async def test_downloads_from_minio_when_local_missing(self, tmp_path):
        tmp_dl = tmp_path / "downloaded.mp3"
        tmp_dl.write_bytes(b"fake remote mp3")

        with patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "download_audio",
                          new=AsyncMock(return_value=tmp_dl)):
            episode = {"audio_filename": "remote-only.mp3"}
            async with _audio_file(episode) as p:
                assert p == tmp_dl
                assert p.exists()

    @pytest.mark.asyncio
    async def test_temp_file_removed_after_context(self, tmp_path):
        tmp_dl = tmp_path / "to-clean.mp3"
        tmp_dl.write_bytes(b"fake remote mp3")

        with patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "download_audio",
                          new=AsyncMock(return_value=tmp_dl)):
            episode = {"audio_filename": "remote-only.mp3"}
            async with _audio_file(episode):
                pass
            assert not tmp_dl.exists(), "temp file should be cleaned up"

    @pytest.mark.asyncio
    async def test_temp_file_removed_even_when_body_raises(self, tmp_path):
        tmp_dl = tmp_path / "to-clean.mp3"
        tmp_dl.write_bytes(b"fake remote mp3")

        with patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "download_audio",
                          new=AsyncMock(return_value=tmp_dl)):
            episode = {"audio_filename": "remote-only.mp3"}
            with pytest.raises(RuntimeError):
                async with _audio_file(episode):
                    raise RuntimeError("boom")
            assert not tmp_dl.exists()

    @pytest.mark.asyncio
    async def test_yields_none_when_download_fails(self):
        with patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "download_audio",
                          new=AsyncMock(side_effect=ConnectionError("network down"))):
            episode = {"audio_filename": "remote-only.mp3"}
            async with _audio_file(episode) as p:
                assert p is None

    @pytest.mark.asyncio
    async def test_local_disk_wins_over_minio(self, tmp_path):
        """If both exist, local is used and MinIO is never called."""
        audio_file = tmp_path / "both-places.mp3"
        audio_file.write_bytes(b"local wins")

        download_mock = AsyncMock()
        with patch.object(audio_mod, "AUDIO_DIR", tmp_path), \
             patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "download_audio", new=download_mock):
            episode = {"audio_filename": "both-places.mp3"}
            async with _audio_file(episode) as p:
                assert p == audio_file
            download_mock.assert_not_called()


class TestStorageIsConfigured:
    def test_false_when_endpoint_missing(self):
        with patch.multiple(
            "app.services.storage",
            MINIO_ENDPOINT="",
            MINIO_BUCKET="b", MINIO_ACCESS_KEY="k", MINIO_SECRET_KEY="s",
        ):
            assert storage.is_configured() is False

    def test_false_when_secret_missing(self):
        with patch.multiple(
            "app.services.storage",
            MINIO_ENDPOINT="https://x", MINIO_BUCKET="b",
            MINIO_ACCESS_KEY="k", MINIO_SECRET_KEY="",
        ):
            assert storage.is_configured() is False

    def test_true_when_all_set(self):
        with patch.multiple(
            "app.services.storage",
            MINIO_ENDPOINT="https://x", MINIO_BUCKET="b",
            MINIO_ACCESS_KEY="k", MINIO_SECRET_KEY="s",
        ):
            assert storage.is_configured() is True
