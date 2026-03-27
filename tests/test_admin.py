"""Tests for admin router (RSS import & transcription)."""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import app.config as app_config
import app.routers.admin as admin_mod
from app.database import (
    create_episode,
    get_episode,
    get_setting,
    set_setting,
    update_episode,
)
from app.main import app
from app.services.transcription import _active_jobs, get_active_jobs

ADMIN_PASSWORD = "testpass123"


def _auth_header(password=ADMIN_PASSWORD):
    creds = base64.b64encode(f"admin:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


@pytest_asyncio.fixture
async def admin_client():
    """Async client with admin password configured."""
    original = app_config.ADMIN_PASSWORD
    app_config.ADMIN_PASSWORD = ADMIN_PASSWORD
    admin_mod.ADMIN_PASSWORD = ADMIN_PASSWORD

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app_config.ADMIN_PASSWORD = original
    admin_mod.ADMIN_PASSWORD = original


@pytest_asyncio.fixture
async def client_no_auth():
    """Async client without admin password configured."""
    original = app_config.ADMIN_PASSWORD
    app_config.ADMIN_PASSWORD = ""
    admin_mod.ADMIN_PASSWORD = ""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app_config.ADMIN_PASSWORD = original
    admin_mod.ADMIN_PASSWORD = original


@pytest.fixture(autouse=True)
def cleanup_jobs():
    """Clear job queue between tests."""
    _active_jobs.clear()
    yield
    _active_jobs.clear()


# --- Auth tests ---

class TestAdminAuth:
    @pytest.mark.asyncio
    async def test_admin_requires_auth(self, admin_client):
        resp = await admin_client.get("/admin")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_with_correct_password(self, admin_client):
        resp = await admin_client.get("/admin", headers=_auth_header())
        assert resp.status_code == 200
        assert "Admin" in resp.text

    @pytest.mark.asyncio
    async def test_admin_with_wrong_password(self, admin_client):
        resp = await admin_client.get("/admin", headers=_auth_header("wrong"))
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_rejects_wrong_username(self, admin_client):
        creds = base64.b64encode(f"hacker:{ADMIN_PASSWORD}".encode()).decode()
        resp = await admin_client.get("/admin", headers={"Authorization": f"Basic {creds}"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_admin_disabled_when_no_password(self, client_no_auth):
        resp = await client_no_auth.get("/admin", headers=_auth_header())
        assert resp.status_code == 403


# --- Feed management ---

class TestFeedManagement:
    @pytest.mark.asyncio
    async def test_save_feed_url(self, admin_client):
        resp = await admin_client.post(
            "/admin/feed",
            data={"feed_url": "https://example.com/feed.xml"},
            headers=_auth_header(),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        saved = await get_setting("feed_url")
        assert saved == "https://example.com/feed.xml"

    @pytest.mark.asyncio
    async def test_admin_page_shows_feed_url(self, admin_client):
        await set_setting("feed_url", "https://example.com/rss")
        resp = await admin_client.get("/admin", headers=_auth_header())
        assert resp.status_code == 200
        assert "https://example.com/rss" in resp.text


# --- Feed parsing ---

class TestFeedParsing:
    @pytest.mark.asyncio
    @patch("app.routers.admin.parse_feed")
    async def test_admin_shows_feed_episodes(self, mock_parse, admin_client):
        mock_parse.return_value = [
            {
                "title": "Mediepodden 175 - Filmtest",
                "episode_number": 175,
                "guid": "guid-175",
                "audio_url": "https://example.com/175.mp3",
                "published": "2024-01-15",
                "db_id": None,
                "db_status": None,
            },
        ]
        await set_setting("feed_url", "https://example.com/feed")
        resp = await admin_client.get("/admin", headers=_auth_header())
        assert resp.status_code == 200
        assert "Mediepodden 175" in resp.text
        assert "175" in resp.text


# --- Episode number parsing ---

class TestEpisodeNumberParsing:
    def test_parse_standard_format(self):
        from app.services.feed import parse_episode_number
        assert parse_episode_number("Mediepodden 175 - Filmtest") == 175

    def test_parse_no_number(self):
        from app.services.feed import parse_episode_number
        assert parse_episode_number("Bonus: Extra avsnitt") is None

    def test_parse_different_spacing(self):
        from app.services.feed import parse_episode_number
        assert parse_episode_number("Mediepodden 42– Titel") == 42


# --- Job status ---

class TestJobStatus:
    @pytest.mark.asyncio
    async def test_status_endpoint_returns_html(self, admin_client):
        resp = await admin_client.get("/admin/status", headers=_auth_header())
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_status_shows_active_jobs(self, admin_client):
        from app.services.transcription import TranscriptionJob, JobStage
        job = TranscriptionJob(
            episode_id=1,
            title="Test Episode",
            episode_number=1,
            audio_url="https://example.com/1.mp3",
            stage=JobStage.DOWNLOADING,
        )
        job.progress = 5
        _active_jobs[job.id] = job

        resp = await admin_client.get("/admin/status", headers=_auth_header())
        assert resp.status_code == 200
        assert "Test Episode" in resp.text
        assert "5%" in resp.text


# --- Clear jobs ---

class TestClearJobs:
    @pytest.mark.asyncio
    async def test_clear_completed_jobs(self, admin_client):
        from app.services.transcription import TranscriptionJob, JobStage
        job = TranscriptionJob(
            episode_id=1,
            title="Done Job",
            episode_number=1,
            audio_url="https://example.com/1.mp3",
            stage=JobStage.COMPLETED,
        )
        job.progress = 100
        _active_jobs[job.id] = job
        assert len(get_active_jobs()) == 1

        resp = await admin_client.post(
            "/admin/rensa",
            headers=_auth_header(),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert len(get_active_jobs()) == 0


# --- Transcription start ---

class TestStartTranscription:
    @pytest.mark.asyncio
    @patch("app.routers.admin.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_start_transcription_creates_episode(
        self, mock_worker, mock_parse, admin_client
    ):
        mock_worker.return_value = None
        mock_parse.return_value = [
            {
                "title": "Mediepodden 200 - Ny film",
                "episode_number": 200,
                "guid": "guid-200",
                "audio_url": "https://example.com/200.mp3",
                "published": "2024-06-01",
            },
        ]
        await set_setting("feed_url", "https://example.com/feed")

        resp = await admin_client.post(
            "/admin/transkribera",
            data={"episodes": "guid-200"},
            headers=_auth_header(),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # Verify episode was created
        from app.database import get_episode_by_number
        ep = await get_episode_by_number(200)
        assert ep is not None
        assert ep["transcription_status"] == "queued"

    @pytest.mark.asyncio
    @patch("app.routers.admin.parse_feed")
    @patch("app.services.transcription.ensure_worker_running")
    async def test_skip_already_completed(
        self, mock_worker, mock_parse, admin_client
    ):
        mock_worker.return_value = None

        # Create completed episode
        ep_id = await create_episode("Mediepodden 100 - Klar", 100, None, None, None)
        await update_episode(ep_id, transcription_status="completed", feed_guid="guid-100")

        mock_parse.return_value = [
            {
                "title": "Mediepodden 100 - Klar",
                "episode_number": 100,
                "guid": "guid-100",
                "audio_url": "https://example.com/100.mp3",
                "published": "2024-01-01",
            },
        ]
        await set_setting("feed_url", "https://example.com/feed")

        resp = await admin_client.post(
            "/admin/transkribera",
            data={"episodes": "guid-100"},
            headers=_auth_header(),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # No job should have been created
        assert len(get_active_jobs()) == 0
