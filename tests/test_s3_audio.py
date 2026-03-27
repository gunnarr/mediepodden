"""Tests for S3 audio storage integration."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from app.services import s3 as s3_mod
from app.services.audio import _resolve_audio_path, get_audio_path


async def _async_iter(items):
    for item in items:
        yield item


class TestS3IsConfigured:
    def test_not_configured_when_empty(self):
        with patch.object(s3_mod, "S3_AUDIO_BUCKET", ""):
            assert s3_mod.is_configured() is False

    def test_configured_when_set(self):
        with patch.object(s3_mod, "S3_AUDIO_BUCKET", "my-bucket"):
            assert s3_mod.is_configured() is True


class TestUploadAudio:
    @pytest.mark.asyncio
    async def test_upload_skipped_when_not_configured(self):
        with patch.object(s3_mod, "S3_AUDIO_BUCKET", ""):
            # Should not raise, just return
            await s3_mod.upload_audio(Path("/tmp/test.mp3"), "test.mp3")

    @pytest.mark.asyncio
    async def test_upload_calls_boto3(self):
        mock_client = MagicMock()
        with (
            patch.object(s3_mod, "S3_AUDIO_BUCKET", "my-bucket"),
            patch.object(s3_mod, "_get_client", return_value=mock_client),
        ):
            await s3_mod.upload_audio(Path("/tmp/test.mp3"), "mediepodden-1.mp3")
            mock_client.upload_file.assert_called_once_with(
                "/tmp/test.mp3", "my-bucket", "mediepodden-1.mp3"
            )


class TestAudioExistsS3:
    @pytest.mark.asyncio
    async def test_exists_returns_false_when_not_configured(self):
        with patch.object(s3_mod, "S3_AUDIO_BUCKET", ""):
            assert await s3_mod.audio_exists("test.mp3") is False

    @pytest.mark.asyncio
    async def test_exists_returns_true(self):
        mock_client = MagicMock()
        mock_client.head_object.return_value = {}
        with (
            patch.object(s3_mod, "S3_AUDIO_BUCKET", "my-bucket"),
            patch.object(s3_mod, "_get_client", return_value=mock_client),
        ):
            assert await s3_mod.audio_exists("mediepodden-1.mp3") is True

    @pytest.mark.asyncio
    async def test_exists_returns_false_on_error(self):
        mock_client = MagicMock()
        mock_client.head_object.side_effect = Exception("Not Found")
        with (
            patch.object(s3_mod, "S3_AUDIO_BUCKET", "my-bucket"),
            patch.object(s3_mod, "_get_client", return_value=mock_client),
        ):
            assert await s3_mod.audio_exists("missing.mp3") is False


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
    async def test_returns_none_when_no_local_and_no_s3(self):
        episode = {"audio_filename": "missing.mp3"}
        result = await _resolve_audio_path(episode)
        assert result is None

    @pytest.mark.asyncio
    async def test_downloads_from_s3_when_no_local(self, tmp_path):
        tmp_file = tmp_path / "downloaded.mp3"
        tmp_file.write_bytes(b"fake s3 mp3")

        with (
            patch("app.services.s3.is_configured", return_value=True),
            patch("app.services.s3.audio_exists", new_callable=AsyncMock, return_value=True),
            patch("app.services.s3.download_audio", new_callable=AsyncMock, return_value=tmp_file),
        ):
            episode = {"audio_filename": "missing-locally.mp3"}
            result = await _resolve_audio_path(episode)
            assert result == tmp_file


class TestTranscriptionS3Upload:
    @pytest.mark.asyncio
    async def test_save_results_uploads_to_s3(self):
        """_save_results uploads audio to S3 when not already there."""
        from app.services.transcription import _save_results, TranscriptionJob

        job = TranscriptionJob(
            episode_id=1,
            title="Test",
            episode_number=42,
            audio_url="https://example.com/test.mp3",
        )

        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.write(b"fake audio")
        tmp.close()
        tmp_path = Path(tmp.name)

        try:
            with (
                patch("app.services.transcription.save_segments", new_callable=AsyncMock),
                patch("app.services.transcription.update_episode", new_callable=AsyncMock),
                patch("app.services.transcription.invalidate_stats_cache"),
                patch("app.services.s3.upload_audio", new_callable=AsyncMock) as mock_upload,
            ):
                segments = [{"start": 0.0, "end": 5.0, "text": "hello"}]
                await _save_results(job, segments, "mediepodden-42.mp3", tmp_path, False)
                mock_upload.assert_called_once()
                assert mock_upload.call_args[0][1] == "mediepodden-42.mp3"
        finally:
            tmp_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_save_results_skips_upload_when_already_in_s3(self):
        """_save_results skips S3 upload when audio is already there."""
        from app.services.transcription import _save_results, TranscriptionJob

        job = TranscriptionJob(
            episode_id=1,
            title="Test",
            episode_number=42,
            audio_url="https://example.com/test.mp3",
        )

        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp.write(b"fake audio")
        tmp.close()
        tmp_path = Path(tmp.name)

        try:
            with (
                patch("app.services.transcription.save_segments", new_callable=AsyncMock),
                patch("app.services.transcription.update_episode", new_callable=AsyncMock),
                patch("app.services.transcription.invalidate_stats_cache"),
                patch("app.services.s3.upload_audio", new_callable=AsyncMock) as mock_upload,
            ):
                segments = [{"start": 0.0, "end": 5.0, "text": "hello"}]
                await _save_results(job, segments, "mediepodden-42.mp3", tmp_path, True)
                mock_upload.assert_not_called()
        finally:
            tmp_path.unlink(missing_ok=True)
