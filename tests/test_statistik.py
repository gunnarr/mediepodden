"""Tests for the statistik routes and timeline database function."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.database import create_episode, save_segments, update_episode
from app.database.search import get_timeline_data
from app.main import app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def episodes_with_data():
    """Create two episodes with searchable segments."""
    ep1 = await create_episode("Avsnitt ett", 1, None, None, "2024-01-01")
    await update_episode(ep1, transcription_status="completed", duration_seconds=600)
    await save_segments(ep1, [
        {"start": 0.0, "end": 5.0, "text": "Elon Musk köpte Twitter", "speaker": "SPEAKER_0"},
        {"start": 5.0, "end": 10.0, "text": "Elon Musk igen", "speaker": "SPEAKER_1"},
    ])

    ep2 = await create_episode("Avsnitt två", 2, None, None, "2024-02-01")
    await update_episode(ep2, transcription_status="completed", duration_seconds=600)
    await save_segments(ep2, [
        {"start": 0.0, "end": 5.0, "text": "Elon Musk och Mark Zuckerberg", "speaker": "SPEAKER_0"},
    ])
    return ep1, ep2


class TestStatistikRoute:
    @pytest.mark.asyncio
    async def test_statistik_returns_200(self, client):
        resp = await client.get("/statistik")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_statistik_has_title(self, client):
        resp = await client.get("/statistik")
        assert "Statistik" in resp.text

    @pytest.mark.asyncio
    async def test_statistik_has_og_tags(self, client):
        resp = await client.get("/statistik")
        assert 'property="og:title"' in resp.text


class TestEntityTimeline:
    @pytest.mark.asyncio
    async def test_timeline_returns_200(self, client, episodes_with_data):
        resp = await client.get("/statistik/tidslinje?q=Elon")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_timeline_empty_query(self, client):
        resp = await client.get("/statistik/tidslinje?q=")
        assert resp.status_code == 200
        assert resp.text == ""

    @pytest.mark.asyncio
    async def test_timeline_is_partial(self, client, episodes_with_data):
        resp = await client.get("/statistik/tidslinje?q=Elon")
        assert "<!DOCTYPE" not in resp.text


class TestGetTimelineData:
    @pytest.mark.asyncio
    async def test_timeline_returns_hits_per_episode(self, episodes_with_data):
        timeline, max_ep = await get_timeline_data("Elon")
        assert 1 in timeline
        assert 2 in timeline
        assert timeline[1] == 2  # Two "Elon" hits in episode 1
        assert timeline[2] == 1  # One hit in episode 2

    @pytest.mark.asyncio
    async def test_timeline_max_episode(self, episodes_with_data):
        timeline, max_ep = await get_timeline_data("Elon")
        assert max_ep == 2

    @pytest.mark.asyncio
    async def test_timeline_empty_for_no_matches(self, episodes_with_data):
        timeline, max_ep = await get_timeline_data("nonexistent_term_xyz")
        assert timeline == {}

    @pytest.mark.asyncio
    async def test_timeline_max_ep_still_set_for_no_matches(self, episodes_with_data):
        timeline, max_ep = await get_timeline_data("nonexistent_term_xyz")
        # max_ep comes from all episodes, not just matches
        assert max_ep == 2
