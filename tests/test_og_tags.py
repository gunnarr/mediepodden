"""Tests for OG meta tags and sharing metadata across all pages."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.database import create_episode, save_segments, update_episode
from app.main import app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def episode_with_transcript():
    ep_id = await create_episode(
        title="OG-test",
        episode_number=99,
        description="Testavsnitt",
        audio_filename=None,
        published_date="2024-01-01",
    )
    await update_episode(ep_id, transcription_status="completed", duration_seconds=600)
    await save_segments(ep_id, [
        {"start": 0.0, "end": 5.0, "text": "Testar OG-taggar", "speaker": "SPEAKER_0"},
    ])
    return ep_id


class TestOGAbsoluteURLs:
    """All OG images must use absolute URLs for social media crawlers."""

    @pytest.mark.asyncio
    async def test_index_og_image_absolute(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert 'content="https://mediepodden.gunnar.se/static/og-image.png"' in resp.text

    @pytest.mark.asyncio
    async def test_about_og_image_absolute(self, client):
        resp = await client.get("/om")
        assert resp.status_code == 200
        assert 'content="https://mediepodden.gunnar.se/static/og-image.png"' in resp.text

    @pytest.mark.asyncio
    async def test_episodes_og_image_absolute(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt")
        assert resp.status_code == 200
        assert 'content="https://mediepodden.gunnar.se/static/og-image.png"' in resp.text

    @pytest.mark.asyncio
    async def test_statistik_og_image_absolute(self, client):
        resp = await client.get("/statistik")
        assert resp.status_code == 200
        assert 'content="https://mediepodden.gunnar.se/static/og-image.png"' in resp.text

    @pytest.mark.asyncio
    async def test_clip_og_image_absolute(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt/og-test/t/0")
        assert resp.status_code == 200
        assert 'content="https://mediepodden.gunnar.se/' in resp.text
        # No relative og:image
        assert 'content="/static/og-image.png"' not in resp.text
        assert 'content="/klipp/' not in resp.text


class TestOGRequiredTags:
    """All pages must have essential OG + Twitter Card tags."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/", "/om", "/statistik"])
    async def test_pages_have_og_tags(self, client, path):
        resp = await client.get(path)
        assert resp.status_code == 200
        assert 'property="og:title"' in resp.text
        assert 'property="og:description"' in resp.text
        assert 'property="og:image"' in resp.text
        assert 'property="og:site_name"' in resp.text
        assert 'property="og:url"' in resp.text
        assert 'property="og:locale"' in resp.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/", "/om", "/statistik"])
    async def test_pages_have_twitter_card(self, client, path):
        resp = await client.get(path)
        assert resp.status_code == 200
        assert 'name="twitter:card" content="summary_large_image"' in resp.text
        assert 'name="twitter:title"' in resp.text
        assert 'name="twitter:description"' in resp.text
        assert 'name="twitter:image"' in resp.text

    @pytest.mark.asyncio
    async def test_avsnitt_has_og_tags(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt")
        assert resp.status_code == 200
        assert 'property="og:title"' in resp.text
        assert 'name="twitter:card"' in resp.text

    @pytest.mark.asyncio
    async def test_clip_has_full_og(self, client, episode_with_transcript):
        resp = await client.get("/avsnitt/og-test/t/0")
        assert resp.status_code == 200
        assert 'property="og:title"' in resp.text
        assert 'property="og:type" content="article"' in resp.text
        assert 'name="twitter:card" content="summary_large_image"' in resp.text

    @pytest.mark.asyncio
    async def test_search_query_in_og_title(self, client, episode_with_transcript):
        resp = await client.get("/?q=OG")
        assert resp.status_code == 200
        assert "OG" in resp.text


class TestOGImageDimensions:
    """OG images should declare width/height for faster rendering."""

    @pytest.mark.asyncio
    async def test_index_has_image_dimensions(self, client):
        resp = await client.get("/")
        assert 'property="og:image:width" content="1200"' in resp.text
        assert 'property="og:image:height" content="630"' in resp.text
