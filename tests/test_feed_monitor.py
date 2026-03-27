"""Tests for RSS feed monitor (nightly auto-transcription)."""

from unittest.mock import patch

import pytest

from app.database import (
    create_episode,
    get_episode_by_guid,
    get_episode_by_number,
    get_setting,
    set_setting,
    update_episode,
)
from app.services.feed_monitor import check_feed, _seconds_until
from app.services.transcription import _active_jobs, get_active_jobs


@pytest.fixture(autouse=True)
def cleanup_jobs():
    """Clear job queue between tests."""
    _active_jobs.clear()
    yield
    _active_jobs.clear()


FAKE_FEED = [
    {
        "title": "Mediepodden 200 - Ny film",
        "episode_number": 200,
        "guid": "guid-200",
        "audio_url": "https://example.com/200.mp3",
        "published": "2025-06-01",
        "description": "Om en ny film",
    },
    {
        "title": "Mediepodden 201 - Annan film",
        "episode_number": 201,
        "guid": "guid-201",
        "audio_url": "https://example.com/201.mp3",
        "published": "2025-06-15",
        "description": "Om en annan film",
    },
]


class TestCheckFeed:
    @pytest.mark.asyncio
    async def test_no_feed_url_returns_zero(self):
        """When no feed_url is saved, check_feed does nothing."""
        result = await check_feed()
        assert result == 0

    @pytest.mark.asyncio
    @patch("app.services.feed_monitor.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_new_episodes_get_queued(self, mock_worker, mock_parse):
        """New episodes should be created in DB and queued for transcription."""
        mock_worker.return_value = None
        mock_parse.return_value = FAKE_FEED
        await set_setting("feed_url", "https://example.com/feed.xml")

        result = await check_feed()

        assert result == 2
        assert len(get_active_jobs()) == 2

        # Verify episodes created in DB
        ep200 = await get_episode_by_number(200)
        assert ep200 is not None
        assert ep200["feed_guid"] == "guid-200"
        assert ep200["audio_url"] == "https://example.com/200.mp3"
        assert ep200["transcription_status"] == "queued"

        ep201 = await get_episode_by_number(201)
        assert ep201 is not None
        assert ep201["transcription_status"] == "queued"

    @pytest.mark.asyncio
    @patch("app.services.feed_monitor.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_existing_episodes_skipped(self, mock_worker, mock_parse):
        """Episodes already in DB should not be re-queued."""
        mock_worker.return_value = None

        # Pre-create episode 200 as completed
        ep_id = await create_episode("Mediepodden 200 - Ny film", 200, None, None, None)
        await update_episode(
            ep_id,
            transcription_status="completed",
            feed_guid="guid-200",
        )

        mock_parse.return_value = FAKE_FEED
        await set_setting("feed_url", "https://example.com/feed.xml")

        result = await check_feed()

        # Only episode 201 should be new
        assert result == 1
        assert len(get_active_jobs()) == 1

    @pytest.mark.asyncio
    @patch("app.services.feed_monitor.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_existing_by_number_skipped(self, mock_worker, mock_parse):
        """Episodes matched by episode_number (no guid) should be skipped."""
        mock_worker.return_value = None

        # Pre-create episode 200 without guid
        await create_episode("Mediepodden 200 - Ny film", 200, None, None, None)

        mock_parse.return_value = FAKE_FEED
        await set_setting("feed_url", "https://example.com/feed.xml")

        result = await check_feed()

        # Only episode 201 should be new
        assert result == 1

    @pytest.mark.asyncio
    @patch("app.services.feed_monitor.parse_feed")
    async def test_episodes_without_audio_skipped(self, mock_parse):
        """Episodes without audio_url should be skipped."""
        mock_parse.return_value = [
            {
                "title": "Bonus utan ljud",
                "episode_number": None,
                "guid": "guid-bonus",
                "audio_url": "",
                "published": "2025-07-01",
            },
        ]
        await set_setting("feed_url", "https://example.com/feed.xml")

        result = await check_feed()
        assert result == 0

    @pytest.mark.asyncio
    @patch("app.services.feed_monitor.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_all_existing_returns_zero(self, mock_worker, mock_parse):
        """When all feed episodes already exist, return 0."""
        mock_worker.return_value = None

        for ep in FAKE_FEED:
            ep_id = await create_episode(
                ep["title"], ep["episode_number"], None, None, None
            )
            await update_episode(ep_id, feed_guid=ep["guid"])

        mock_parse.return_value = FAKE_FEED
        await set_setting("feed_url", "https://example.com/feed.xml")

        result = await check_feed()
        assert result == 0
        assert len(get_active_jobs()) == 0

    @pytest.mark.asyncio
    @patch("app.services.feed_monitor.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_failed_episodes_get_retried(self, mock_worker, mock_parse):
        """Episodes with transcription_status='failed' should be retried."""
        mock_worker.return_value = None

        # Pre-create episode 200 as FAILED
        ep_id = await create_episode("Mediepodden 200 - Ny film", 200, None, None, None)
        await update_episode(
            ep_id,
            transcription_status="failed",
            feed_guid="guid-200",
        )

        mock_parse.return_value = [FAKE_FEED[0]]  # Only episode 200
        await set_setting("feed_url", "https://example.com/feed.xml")

        result = await check_feed()

        # Failed episode should be retried
        assert result == 1
        assert len(get_active_jobs()) == 1

        # Verify transcription was re-queued (status set to queued by start_transcription)
        ep = await get_episode_by_number(200)
        assert ep["transcription_status"] == "queued"

    @pytest.mark.asyncio
    @patch("app.services.feed_monitor.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_completed_episodes_not_retried(self, mock_worker, mock_parse):
        """Completed episodes should NOT be retried."""
        mock_worker.return_value = None

        ep_id = await create_episode("Mediepodden 200 - Ny film", 200, None, None, None)
        await update_episode(
            ep_id,
            transcription_status="completed",
            feed_guid="guid-200",
        )

        mock_parse.return_value = [FAKE_FEED[0]]
        await set_setting("feed_url", "https://example.com/feed.xml")

        result = await check_feed()
        assert result == 0
        assert len(get_active_jobs()) == 0

    @pytest.mark.asyncio
    @patch("app.services.feed_monitor.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_new_episode_gets_audio_filename(self, mock_worker, mock_parse):
        """New episodes should get audio_filename set via make_audio_filename."""
        mock_worker.return_value = None
        mock_parse.return_value = FAKE_FEED
        await set_setting("feed_url", "https://example.com/feed.xml")

        await check_feed()

        ep200 = await get_episode_by_number(200)
        assert ep200["audio_filename"] == "2025-06-01-mediepodden-200.mp3"

        ep201 = await get_episode_by_number(201)
        assert ep201["audio_filename"] == "2025-06-15-mediepodden-201.mp3"

    @pytest.mark.asyncio
    @patch("app.services.feed_monitor.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_new_episode_without_number_gets_auto_number(self, mock_worker, mock_parse):
        """Episodes without episode_number should get auto-assigned next number."""
        mock_worker.return_value = None

        # Pre-create episode 200 so next number is 201
        await create_episode("Mediepodden 200", 200, None, None, None)

        mock_parse.return_value = [{
            "title": "EXTRA - Bonusavsnitt",
            "episode_number": None,
            "guid": "guid-extra-123",
            "audio_url": "https://example.com/extra.mp3",
            "published": "2025-07-01",
            "description": "Bonus",
        }]
        await set_setting("feed_url", "https://example.com/feed.xml")

        await check_feed()

        from app.database import get_episode_by_guid
        ep = await get_episode_by_guid("guid-extra-123")
        assert ep is not None
        assert ep["episode_number"] == 201
        assert ep["audio_filename"] == "2025-07-01-mediepodden-201.mp3"

    @pytest.mark.asyncio
    @patch("app.services.feed_monitor.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_new_episode_description_saved(self, mock_worker, mock_parse):
        """New episodes should save description from feed."""
        mock_worker.return_value = None
        mock_parse.return_value = [FAKE_FEED[0]]
        await set_setting("feed_url", "https://example.com/feed.xml")

        await check_feed()

        ep = await get_episode_by_number(200)
        assert ep["description"] == "Om en ny film"


class TestSecondsUntil:
    def test_returns_positive(self):
        """_seconds_until should always return a positive number."""
        result = _seconds_until(3)
        assert result > 0

    def test_max_24_hours(self):
        """Result should never exceed 24 hours."""
        result = _seconds_until(3)
        assert result <= 86400
