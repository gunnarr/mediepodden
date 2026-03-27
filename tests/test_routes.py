"""Tests for FastAPI routes (public)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.database import (
    create_episode,
    save_segments,
    update_episode,
)
from app.main import app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def episode_with_transcript():
    ep_id = await create_episode(
        title="Testdöden",
        episode_number=1,
        description="Testavsnitt",
        audio_filename=None,
        published_date="2024-01-01",
    )
    await update_episode(ep_id, transcription_status="completed", duration_seconds=600)
    await save_segments(ep_id, [
        {"start": 0.0, "end": 5.0, "text": "Hej och välkommen", "speaker": "SPEAKER_0"},
        {"start": 5.0, "end": 10.0, "text": "Tack det samma", "speaker": "SPEAKER_1"},
    ])
    return ep_id


# --- Public routes ---

class TestSearchRoutes:
    @pytest.mark.asyncio
    async def test_index_page(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Mediepodden" in resp.text

    @pytest.mark.asyncio
    async def test_search_with_query(self, client, episode_with_transcript):
        resp = await client.get("/?q=välkommen")
        assert resp.status_code == 200
        assert "ffar" in resp.text  # "träffar" may be HTML-encoded as "tr&auml;ffar"

    @pytest.mark.asyncio
    async def test_search_empty_query(self, client):
        resp = await client.get("/?q=")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_about_page(self, client):
        resp = await client.get("/om")
        assert resp.status_code == 200
        assert "KB-Whisper" in resp.text


class TestEpisodeRoutes:
    @pytest.mark.asyncio
    async def test_episode_list(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt")
        assert resp.status_code == 200


class TestClipPage:
    @pytest.mark.asyncio
    async def test_clip_page_shows_segment(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt/testdoden/t/0")
        assert resp.status_code == 200
        assert "välkommen" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_clip_page_has_player(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt/testdoden/t/0")
        assert resp.status_code == 200
        assert "clip-player" in resp.text
        assert "clip-play-btn" in resp.text

    @pytest.mark.asyncio
    async def test_clip_page_404_for_bad_time(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt/testdoden/t/9999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_clip_page_404_for_nonexistent_episode(self, client):
        resp = await client.get("/avsnitt/nonexistent/t/0")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_clip_page_has_og_tags(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt/testdoden/t/0")
        assert 'property="og:title"' in resp.text
        assert 'property="og:description"' in resp.text


class TestOGMetaTags:
    @pytest.mark.asyncio
    async def test_index_has_og_tags(self, client):
        resp = await client.get("/")
        assert 'property="og:title"' in resp.text
        assert 'property="og:description"' in resp.text


# --- HTMX live search ---

class TestLiveSearch:
    @pytest.mark.asyncio
    async def test_live_search_returns_partial(self, client, episode_with_transcript):
        resp = await client.get("/sok?q=välkommen")
        assert resp.status_code == 200
        # Should return partial HTML (no <!DOCTYPE>)
        assert "<!DOCTYPE" not in resp.text
        assert "ffar" in resp.text

    @pytest.mark.asyncio
    async def test_live_search_empty_query(self, client):
        resp = await client.get("/sok?q=")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_live_search_has_player(self, client, episode_with_transcript):
        resp = await client.get("/sok?q=välkommen")
        assert resp.status_code == 200
        assert "result-player" in resp.text
        assert "play-btn" in resp.text


# --- Audio clips ---

class TestAudioClips:
    @pytest.mark.asyncio
    async def test_clip_404_for_nonexistent_episode(self, client):
        resp = await client.get("/klipp/99999/0.0-5.0.mp3")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_clip_400_for_invalid_range(self, client, episode_with_transcript):
        resp = await client.get(f"/klipp/{episode_with_transcript}/10.0-5.0.mp3")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_clip_404_for_no_audio_file(self, client, episode_with_transcript):
        resp = await client.get(f"/klipp/{episode_with_transcript}/0.0-5.0.mp3")
        # No audio file exists for test episode
        assert resp.status_code == 404


# --- New feature tests ---

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_check(self, client):
        resp = await client.get("/health")
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert "status" in data
        assert "checks" in data
        assert "database" in data["checks"]


class TestSearchPermalinks:
    @pytest.mark.asyncio
    async def test_search_url_with_params(self, client, episode_with_transcript):
        resp = await client.get("/?q=Mediepodden&sida=1")
        assert resp.status_code == 200


class TestExportTranscription:
    @pytest.mark.asyncio
    async def test_export_srt(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt/testdoden/transkription.srt")
        assert resp.status_code == 200
        assert "-->" in resp.text  # SRT format

    @pytest.mark.asyncio
    async def test_export_txt(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt/testdoden/transkription.txt")
        assert resp.status_code == 200
        assert "välkommen" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_export_srt_404_for_nonexistent(self, client):
        resp = await client.get("/avsnitt/nonexistent/transkription.srt")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_export_txt_404_for_pending(self, client):
        await create_episode("Pending", None, None, None, None)
        resp = await client.get("/avsnitt/pending/transkription.txt")
        assert resp.status_code == 404


class TestEpisodeListAll:
    @pytest.mark.asyncio
    async def test_episode_list_shows_all_statuses(self, client, episode_with_transcript):
        # Create a pending episode
        await create_episode("Väntande", 99, None, None, None)
        resp = await client.get("/avsnitt")
        assert resp.status_code == 200
        # Both episodes should be visible
        assert "Testdöden" in resp.text or "testd" in resp.text.lower()
        assert "ntande" in resp.text  # "Väntande"
