"""Tests for SEO routes: robots.txt and sitemap.xml."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.database import create_episode, update_episode
from app.main import app


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestRobotsTxt:
    @pytest.mark.asyncio
    async def test_robots_returns_200(self, client):
        resp = await client.get("/robots.txt")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_robots_content_type(self, client):
        resp = await client.get("/robots.txt")
        assert "text/plain" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_robots_allows_root(self, client):
        resp = await client.get("/robots.txt")
        assert "Allow: /" in resp.text

    @pytest.mark.asyncio
    async def test_robots_disallows_admin(self, client):
        resp = await client.get("/robots.txt")
        assert "Disallow: /admin" in resp.text

    @pytest.mark.asyncio
    async def test_robots_disallows_sok(self, client):
        resp = await client.get("/robots.txt")
        assert "Disallow: /sok" in resp.text

    @pytest.mark.asyncio
    async def test_robots_has_sitemap(self, client):
        resp = await client.get("/robots.txt")
        assert "Sitemap: https://mediepodden.gunnar.se/sitemap.xml" in resp.text


class TestSitemapXml:
    @pytest.mark.asyncio
    async def test_sitemap_returns_200(self, client):
        resp = await client.get("/sitemap.xml")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_sitemap_is_xml(self, client):
        resp = await client.get("/sitemap.xml")
        assert "application/xml" in resp.headers["content-type"]
        assert '<?xml version="1.0"' in resp.text

    @pytest.mark.asyncio
    async def test_sitemap_has_static_pages(self, client):
        resp = await client.get("/sitemap.xml")
        for path in ["/", "/avsnitt", "/statistik", "/om"]:
            assert f"<loc>https://mediepodden.gunnar.se{path}</loc>" in resp.text

    @pytest.mark.asyncio
    async def test_sitemap_includes_completed_episodes(self, client):
        ep_id = await create_episode("Sitemap Test", 77, None, None, "2024-03-01")
        await update_episode(ep_id, transcription_status="completed")
        resp = await client.get("/sitemap.xml")
        assert "/avsnitt/sitemap-test</loc>" in resp.text

    @pytest.mark.asyncio
    async def test_sitemap_excludes_pending_episodes(self, client):
        await create_episode("Pending Episode", 78, None, None, "2024-03-01")
        resp = await client.get("/sitemap.xml")
        assert "pending-episode" not in resp.text

