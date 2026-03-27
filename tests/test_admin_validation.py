"""Tests for admin feed URL validation and search analytics."""

import base64

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import app.config as app_config
import app.routers.admin as admin_mod
from app.database import log_search, get_search_analytics, cleanup_old_analytics
from app.database import log_page_view, get_analytics
from app.main import app

ADMIN_PASSWORD = "testpass123"


def _auth_header(password=ADMIN_PASSWORD):
    creds = base64.b64encode(f"admin:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


@pytest_asyncio.fixture
async def admin_client():
    original_config = app_config.ADMIN_PASSWORD
    original_mod = admin_mod.ADMIN_PASSWORD
    app_config.ADMIN_PASSWORD = ADMIN_PASSWORD
    admin_mod.ADMIN_PASSWORD = ADMIN_PASSWORD
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=_auth_header(),
    ) as ac:
        yield ac
    app_config.ADMIN_PASSWORD = original_config
    admin_mod.ADMIN_PASSWORD = original_mod


class TestFeedURLValidation:
    @pytest.mark.asyncio
    async def test_rejects_too_long_url(self, admin_client):
        resp = await admin_client.post(
            "/admin/feed",
            data={"feed_url": "https://example.com/" + "a" * 2048},
            headers={**admin_client.headers, "X-Forwarded-For": "10.10.10.1"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_ftp_scheme(self, admin_client):
        resp = await admin_client.post(
            "/admin/feed",
            data={"feed_url": "ftp://example.com/feed"},
            headers={**admin_client.headers, "X-Forwarded-For": "10.10.10.2"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_javascript_scheme(self, admin_client):
        resp = await admin_client.post(
            "/admin/feed",
            data={"feed_url": "javascript:alert(1)"},
            headers={**admin_client.headers, "X-Forwarded-For": "10.10.10.3"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_localhost(self, admin_client):
        resp = await admin_client.post(
            "/admin/feed",
            data={"feed_url": "https://localhost/feed"},
            headers={**admin_client.headers, "X-Forwarded-For": "10.10.10.4"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_127_0_0_1(self, admin_client):
        resp = await admin_client.post(
            "/admin/feed",
            data={"feed_url": "https://127.0.0.1/feed"},
            headers={**admin_client.headers, "X-Forwarded-For": "10.10.10.5"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_private_ip_10(self, admin_client):
        resp = await admin_client.post(
            "/admin/feed",
            data={"feed_url": "https://10.0.0.1/feed"},
            headers={**admin_client.headers, "X-Forwarded-For": "10.10.10.6"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_private_ip_192(self, admin_client):
        resp = await admin_client.post(
            "/admin/feed",
            data={"feed_url": "https://192.168.1.1/feed"},
            headers={**admin_client.headers, "X-Forwarded-For": "10.10.10.7"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_url_redirects(self, admin_client):
        resp = await admin_client.post(
            "/admin/feed",
            data={"feed_url": ""},
            follow_redirects=False,
            headers={**admin_client.headers, "X-Forwarded-For": "10.10.10.8"},
        )
        assert resp.status_code == 303


class TestSearchAnalytics:
    @pytest.mark.asyncio
    async def test_log_and_get_search_analytics(self):
        await log_search("Elon Musk", 15)
        await log_search("Elon Musk", 12)
        await log_search("Schibsted", 3)
        result = await get_search_analytics(days=30)
        assert result["total_searches"] == 3
        assert len(result["top_queries"]) >= 2

    @pytest.mark.asyncio
    async def test_zero_result_queries(self):
        await log_search("nonexistent", 0)
        await log_search("also_missing", 0)
        await log_search("has_results", 5)
        result = await get_search_analytics(days=30)
        assert len(result["zero_results"]) == 2

    @pytest.mark.asyncio
    async def test_search_analytics_empty(self):
        result = await get_search_analytics(days=30)
        assert result["total_searches"] == 0
        assert result["top_queries"] == []
        assert result["zero_results"] == []

    @pytest.mark.asyncio
    async def test_search_analytics_per_day(self):
        await log_search("test", 1)
        result = await get_search_analytics(days=30)
        assert len(result["per_day"]) == 1


class TestCleanupAnalytics:
    @pytest.mark.asyncio
    async def test_cleanup_old_data(self):
        """Cleanup with days=0 means 'older than 0 days from now' = everything before now."""
        await log_page_view("/test", None, None, None, "1.2.3.4")
        await log_search("test", 5)
        # Verify data exists
        analytics = await get_analytics(days=365)
        assert analytics["total_views"] >= 1
        # Cleanup with large window should keep recent data
        await cleanup_old_analytics(days=365)
        analytics = await get_analytics(days=365)
        assert analytics["total_views"] >= 1
