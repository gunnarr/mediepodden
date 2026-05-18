"""Tests for audio source resolution: local-disk fast path, MinIO fallback, temp cleanup."""

from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from app.services import storage
import app.services.audio as audio_mod
from app.services.audio import _audio_file, _prepare_audio_for_clip


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


class TestDownloadAudioRange:
    """Range requests for partial audio downloads."""

    async def test_calls_get_object_with_range_header(self, tmp_path):
        storage.reset_client_for_tests()
        body = MagicMock()
        body.read.side_effect = [b"fake-mp3-data", b""]
        fake_client = MagicMock()
        fake_client.get_object.return_value = {"Body": body}

        with patch.multiple(
            "app.services.storage",
            MINIO_ENDPOINT="https://x", MINIO_BUCKET="b",
            MINIO_ACCESS_KEY="k", MINIO_SECRET_KEY="s",
        ), patch.object(storage, "_get_client", return_value=fake_client):
            path = await storage.download_audio_range("ep.mp3", 1000, 5000)

        try:
            fake_client.get_object.assert_called_once_with(
                Bucket="b", Key="ep.mp3", Range="bytes=1000-5000"
            )
            assert path.exists()
            assert path.read_bytes() == b"fake-mp3-data"
        finally:
            path.unlink(missing_ok=True)

    async def test_raises_when_not_configured(self):
        with patch.object(storage, "is_configured", return_value=False):
            with pytest.raises(RuntimeError, match="not configured"):
                await storage.download_audio_range("ep.mp3", 0, 100)

    async def test_cleans_up_temp_on_error(self):
        fake_client = MagicMock()
        fake_client.get_object.side_effect = ConnectionError("network")

        created_paths = []
        real_named_temp = storage.tempfile.NamedTemporaryFile

        def capture_tempfile(*args, **kwargs):
            f = real_named_temp(*args, **kwargs)
            created_paths.append(f.name)
            return f

        with patch.multiple(
            "app.services.storage",
            MINIO_ENDPOINT="https://x", MINIO_BUCKET="b",
            MINIO_ACCESS_KEY="k", MINIO_SECRET_KEY="s",
        ), patch.object(storage, "_get_client", return_value=fake_client), \
           patch.object(storage.tempfile, "NamedTemporaryFile", side_effect=capture_tempfile):
            with pytest.raises(ConnectionError):
                await storage.download_audio_range("ep.mp3", 0, 100)

        for p in created_paths:
            assert not Path(p).exists(), f"orphaned tmp file: {p}"


class TestPrepareAudioForClip:
    """Three-tier resolution: local disk → MinIO Range → MinIO full."""

    async def test_local_path_used_with_zero_offset(self, tmp_path):
        audio_file = tmp_path / "test.mp3"
        audio_file.write_bytes(b"fake mp3")

        with patch.object(audio_mod, "AUDIO_DIR", tmp_path):
            episode = {"audio_filename": "test.mp3", "duration_seconds": 7800}
            async with _prepare_audio_for_clip(episode, 100.0, 130.0) as (p, off):
                assert p == audio_file
                assert off == 0.0

    async def test_range_path_when_local_missing_and_duration_known(self, tmp_path):
        tmp_dl = tmp_path / "ranged.mp3"
        tmp_dl.write_bytes(b"partial-mp3")

        with patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "audio_size", new=AsyncMock(return_value=125_000_000)), \
             patch.object(storage, "download_audio_range",
                          new=AsyncMock(return_value=tmp_dl)) as mock_range, \
             patch.object(storage, "download_audio",
                          new=AsyncMock()) as mock_full:
            episode = {"audio_filename": "ep.mp3", "duration_seconds": 7800.0}
            async with _prepare_audio_for_clip(episode, 1000.0, 1030.0) as (p, off):
                assert p == tmp_dl
                assert off == pytest.approx(995.0, abs=0.1)
            mock_range.assert_called_once()
            mock_full.assert_not_called()
        assert not tmp_dl.exists()

    async def test_byte_range_calculation_within_file_bounds(self, tmp_path):
        captured = {}

        async def fake_range(key, byte_start, byte_end):
            captured["start"] = byte_start
            captured["end"] = byte_end
            f = tmp_path / "x.mp3"
            f.write_bytes(b"x")
            return f

        with patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "audio_size", new=AsyncMock(return_value=1_000_000)), \
             patch.object(storage, "download_audio_range", new=fake_range):
            episode = {"audio_filename": "ep.mp3", "duration_seconds": 600.0}
            async with _prepare_audio_for_clip(episode, 595.0, 600.0) as (p, off):
                assert off == pytest.approx(590.0, abs=0.1)
                assert captured["end"] < 1_000_000

    async def test_falls_back_to_full_when_no_duration(self, tmp_path):
        tmp_dl = tmp_path / "full.mp3"
        tmp_dl.write_bytes(b"full-mp3")

        with patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "download_audio",
                          new=AsyncMock(return_value=tmp_dl)) as mock_full, \
             patch.object(storage, "download_audio_range",
                          new=AsyncMock()) as mock_range:
            episode = {"audio_filename": "ep.mp3"}
            async with _prepare_audio_for_clip(episode, 100.0, 130.0) as (p, off):
                assert p == tmp_dl
                assert off == 0.0
            mock_range.assert_not_called()
            mock_full.assert_called_once()

    async def test_falls_back_to_full_when_size_unavailable(self, tmp_path):
        tmp_dl = tmp_path / "full.mp3"
        tmp_dl.write_bytes(b"full-mp3")

        with patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "audio_size", new=AsyncMock(return_value=None)), \
             patch.object(storage, "download_audio",
                          new=AsyncMock(return_value=tmp_dl)) as mock_full, \
             patch.object(storage, "download_audio_range",
                          new=AsyncMock()) as mock_range:
            episode = {"audio_filename": "ep.mp3", "duration_seconds": 7800.0}
            async with _prepare_audio_for_clip(episode, 100.0, 130.0) as (p, off):
                assert p == tmp_dl
                assert off == 0.0
            mock_range.assert_not_called()
            mock_full.assert_called_once()

    async def test_falls_back_to_full_when_range_download_fails(self, tmp_path):
        tmp_dl = tmp_path / "full.mp3"
        tmp_dl.write_bytes(b"full-mp3")

        with patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "audio_size", new=AsyncMock(return_value=125_000_000)), \
             patch.object(storage, "download_audio_range",
                          new=AsyncMock(side_effect=ConnectionError("network"))), \
             patch.object(storage, "download_audio",
                          new=AsyncMock(return_value=tmp_dl)) as mock_full:
            episode = {"audio_filename": "ep.mp3", "duration_seconds": 7800.0}
            async with _prepare_audio_for_clip(episode, 100.0, 130.0) as (p, off):
                assert p == tmp_dl
                assert off == 0.0
            mock_full.assert_called_once()

    async def test_yields_none_when_no_audio_filename(self):
        async with _prepare_audio_for_clip({}, 0.0, 10.0) as (p, off):
            assert p is None
            assert off == 0.0

    async def test_yields_none_when_storage_off(self):
        with patch.object(storage, "is_configured", return_value=False):
            episode = {"audio_filename": "ep.mp3", "duration_seconds": 7800}
            async with _prepare_audio_for_clip(episode, 100.0, 130.0) as (p, off):
                assert p is None
                assert off == 0.0

    async def test_range_tmp_cleaned_up(self, tmp_path):
        tmp_dl = tmp_path / "ranged.mp3"
        tmp_dl.write_bytes(b"partial")

        with patch.object(storage, "is_configured", return_value=True), \
             patch.object(storage, "audio_size", new=AsyncMock(return_value=125_000_000)), \
             patch.object(storage, "download_audio_range",
                          new=AsyncMock(return_value=tmp_dl)):
            episode = {"audio_filename": "ep.mp3", "duration_seconds": 7800.0}
            async with _prepare_audio_for_clip(episode, 100.0, 130.0):
                assert tmp_dl.exists()
        assert not tmp_dl.exists()
