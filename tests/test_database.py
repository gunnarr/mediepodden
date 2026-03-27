"""Tests for database CRUD operations, search, stats, and analytics."""

import pytest

from app.database import (
    create_episode,
    delete_episode,
    get_episode,
    get_episode_by_slug,
    get_episode_segments,
    get_stats,
    save_segments,
    search_segments,
    update_episode,
    get_setting,
    set_setting,
    log_page_view,
    get_analytics,
)


class TestEpisodeCRUD:
    @pytest.mark.asyncio
    async def test_create_episode(self, sample_episode):
        ep = await get_episode(sample_episode)
        assert ep is not None
        assert ep["title"] == "Döden i Venedig"
        assert ep["episode_number"] == 42
        assert ep["transcription_status"] == "pending"

    @pytest.mark.asyncio
    async def test_create_episode_generates_slug(self, sample_episode):
        ep = await get_episode(sample_episode)
        assert ep["slug"] == "doden-i-venedig"

    @pytest.mark.asyncio
    async def test_duplicate_slug_gets_suffix(self):
        id1 = await create_episode("Test Title", None, None, None, None)
        id2 = await create_episode("Test Title", None, None, None, None)
        ep1 = await get_episode(id1)
        ep2 = await get_episode(id2)
        assert ep1["slug"] == "test-title"
        assert ep2["slug"] == "test-title-2"

    @pytest.mark.asyncio
    async def test_get_episode_by_slug(self, sample_episode):
        ep = await get_episode_by_slug("doden-i-venedig")
        assert ep is not None
        assert ep["id"] == sample_episode

    @pytest.mark.asyncio
    async def test_get_episode_by_slug_not_found(self):
        ep = await get_episode_by_slug("does-not-exist")
        assert ep is None

    @pytest.mark.asyncio
    async def test_get_episode_not_found(self):
        ep = await get_episode(99999)
        assert ep is None

    @pytest.mark.asyncio
    async def test_update_episode(self, sample_episode):
        await update_episode(sample_episode, title="Ny titel", transcription_status="completed")
        ep = await get_episode(sample_episode)
        assert ep["title"] == "Ny titel"
        assert ep["transcription_status"] == "completed"

    @pytest.mark.asyncio
    async def test_update_episode_ignores_unknown_fields(self, sample_episode):
        await update_episode(sample_episode, unknown_field="value")
        ep = await get_episode(sample_episode)
        assert ep is not None  # No error, just ignored

    @pytest.mark.asyncio
    async def test_update_speaker_labels(self, sample_episode):
        await update_episode(
            sample_episode,
            speaker_label_0="Kalle",
            speaker_label_1="Fredrik",
        )
        ep = await get_episode(sample_episode)
        assert ep["speaker_label_0"] == "Kalle"
        assert ep["speaker_label_1"] == "Fredrik"

    @pytest.mark.asyncio
    async def test_delete_episode(self, sample_episode):
        await delete_episode(sample_episode)
        ep = await get_episode(sample_episode)
        assert ep is None

    @pytest.mark.asyncio
    async def test_delete_episode_cascades_segments(self, completed_episode):
        segments = await get_episode_segments(completed_episode)
        assert len(segments) == 3

        await delete_episode(completed_episode)
        segments = await get_episode_segments(completed_episode)
        assert len(segments) == 0


class TestSegments:
    @pytest.mark.asyncio
    async def test_save_and_get_segments(self, sample_episode):
        segments = [
            {"start": 0.0, "end": 5.0, "text": "Hello", "speaker": "SPEAKER_0"},
            {"start": 5.0, "end": 10.0, "text": "World"},
        ]
        await save_segments(sample_episode, segments)
        result = await get_episode_segments(sample_episode)
        assert len(result) == 2
        assert result[0]["text"] == "Hello"
        assert result[0]["speaker"] == "SPEAKER_0"
        assert result[1]["speaker"] is None

    @pytest.mark.asyncio
    async def test_save_segments_replaces_existing(self, sample_episode):
        await save_segments(sample_episode, [
            {"start": 0.0, "end": 5.0, "text": "First version"},
        ])
        await save_segments(sample_episode, [
            {"start": 0.0, "end": 3.0, "text": "New version"},
        ])
        result = await get_episode_segments(sample_episode)
        assert len(result) == 1
        assert result[0]["text"] == "New version"

    @pytest.mark.asyncio
    async def test_segments_ordered_by_time(self, sample_episode):
        await save_segments(sample_episode, [
            {"start": 10.0, "end": 15.0, "text": "Later"},
            {"start": 0.0, "end": 5.0, "text": "Earlier"},
        ])
        result = await get_episode_segments(sample_episode)
        assert result[0]["text"] == "Earlier"
        assert result[1]["text"] == "Later"


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_finds_matching_text(self, completed_episode):
        results, total = await search_segments("Venedig")
        assert len(results) >= 1
        assert total >= 1
        assert "Venedig" in results[0]["text"]

    @pytest.mark.asyncio
    async def test_search_returns_episode_info(self, completed_episode):
        results, _ = await search_segments("Venedig")
        assert results[0]["episode_title"] == "Döden i Venedig"
        assert results[0]["episode_slug"] == "doden-i-venedig"

    @pytest.mark.asyncio
    async def test_search_returns_speaker_labels(self, completed_episode):
        results, _ = await search_segments("Venedig")
        r = results[0]
        assert "speaker" in r
        assert "speaker_label_0" in r
        assert "speaker_label_1" in r

    @pytest.mark.asyncio
    async def test_search_returns_highlighted_text(self, completed_episode):
        results, _ = await search_segments("Venedig")
        assert "<mark>" in results[0]["highlighted_text"]

    @pytest.mark.asyncio
    async def test_search_no_results(self, completed_episode):
        results, total = await search_segments("xyznonexistent")
        assert len(results) == 0
        assert total == 0

    @pytest.mark.asyncio
    async def test_search_respects_limit(self, completed_episode):
        results, _ = await search_segments("Mediepodden", limit=1)
        assert len(results) <= 1

    @pytest.mark.asyncio
    async def test_search_filter_by_speaker(self, completed_episode):
        # "döden i Venedig" is spoken by SPEAKER_1 in conftest data
        results, total = await search_segments("Venedig", speaker="SPEAKER_1")
        assert total > 0
        assert all(r["speaker"] == "SPEAKER_1" for r in results)

        # SPEAKER_0 does not say "Venedig"
        results_0, total_0 = await search_segments("Venedig", speaker="SPEAKER_0")
        assert total_0 == 0

    @pytest.mark.asyncio
    async def test_search_with_offset(self, completed_episode):
        results_all, total = await search_segments("Mediepodden")
        results_offset, total2 = await search_segments("Mediepodden", offset=1)
        assert total == total2
        # With offset, we should get fewer or equal results
        assert len(results_offset) <= len(results_all)


class TestStats:
    @pytest.mark.asyncio
    async def test_stats_empty_db(self):
        stats = await get_stats()
        assert stats["episode_count"] == 0
        assert stats["segment_count"] == 0
        assert stats["total_hours"] == 0

    @pytest.mark.asyncio
    async def test_stats_with_completed_episode(self, completed_episode):
        stats = await get_stats()
        assert stats["episode_count"] == 1
        assert stats["segment_count"] == 3
        assert stats["total_seconds_raw"] == 3600.0
        assert stats["total_hours"] == 1
        assert stats["total_minutes"] == 0

    @pytest.mark.asyncio
    async def test_stats_only_counts_completed(self, sample_episode):
        """Pending episodes should not count in stats."""
        stats = await get_stats()
        assert stats["episode_count"] == 0


class TestSettings:
    @pytest.mark.asyncio
    async def test_get_missing_setting(self):
        val = await get_setting("nonexistent")
        assert val is None

    @pytest.mark.asyncio
    async def test_set_and_get_setting(self):
        await set_setting("feed_url", "https://example.com/feed.xml")
        val = await get_setting("feed_url")
        assert val == "https://example.com/feed.xml"

    @pytest.mark.asyncio
    async def test_set_setting_upsert(self):
        await set_setting("key", "value1")
        await set_setting("key", "value2")
        val = await get_setting("key")
        assert val == "value2"


class TestAnalytics:
    @pytest.mark.asyncio
    async def test_log_and_get_analytics(self):
        await log_page_view("/", "q=test", "https://google.com", "Mozilla/5.0", "127.0.0.1")
        await log_page_view("/avsnitt/test", None, None, "Mozilla/5.0", "127.0.0.1")
        await log_page_view("/", "q=death", None, "Mozilla/5.0", "10.0.0.1")

        analytics = await get_analytics(days=30)
        assert analytics["total_views"] == 3
        assert analytics["unique_visitors"] == 2
        assert len(analytics["top_pages"]) >= 1
        assert len(analytics["top_searches"]) >= 1

    @pytest.mark.asyncio
    async def test_analytics_empty(self):
        analytics = await get_analytics(days=30)
        assert analytics["total_views"] == 0
        assert analytics["unique_visitors"] == 0
        assert analytics["top_pages"] == []
        assert analytics["top_searches"] == []
        assert analytics["top_referrers"] == []

    @pytest.mark.asyncio
    async def test_analytics_referrers(self):
        await log_page_view("/", None, "https://twitter.com/post/123", None, "1.1.1.1")
        await log_page_view("/", None, "https://twitter.com/post/123", None, "1.1.1.2")

        analytics = await get_analytics(days=30)
        assert len(analytics["top_referrers"]) >= 1
        assert analytics["top_referrers"][0]["count"] == 2
