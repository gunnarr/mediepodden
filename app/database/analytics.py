"""Analytics, stats cache, and search logging."""

import asyncio
import time

from .connection import get_db

# --- Stats cache ---
_stats_cache: dict | None = None
_stats_cache_time: float = 0
_stats_lock = asyncio.Lock()
STATS_CACHE_TTL = 60  # seconds


def invalidate_stats_cache():
    """Invalidate the stats cache (call after transcription completes)."""
    global _stats_cache, _stats_cache_time
    _stats_cache = None
    _stats_cache_time = 0


async def get_stats() -> dict:
    global _stats_cache, _stats_cache_time
    now = time.time()
    if _stats_cache and (now - _stats_cache_time) < STATS_CACHE_TTL:
        return _stats_cache

    async with _stats_lock:
        # Double-check after acquiring lock
        now = time.time()
        if _stats_cache and (now - _stats_cache_time) < STATS_CACHE_TTL:
            return _stats_cache

        async with get_db() as db:
            c = await db.execute(
                "SELECT COUNT(*) FROM episodes WHERE transcription_status = 'completed'"
            )
            episode_count = (await c.fetchone())[0]

            c = await db.execute(
                "SELECT COUNT(*) FROM episodes"
            )
            total_episodes = (await c.fetchone())[0]

            c = await db.execute(
                "SELECT COALESCE(SUM(duration_seconds), 0) FROM episodes "
                "WHERE transcription_status = 'completed'"
            )
            total_seconds = (await c.fetchone())[0]

            c = await db.execute(
                "SELECT COUNT(*) FROM segments"
            )
            segment_count = (await c.fetchone())[0]

        total_minutes = int(total_seconds // 60)
        total_hours = total_minutes // 60
        remaining_minutes = total_minutes % 60

        result = {
            "episode_count": episode_count,
            "total_episodes": total_episodes,
            "total_hours": total_hours,
            "total_minutes": remaining_minutes,
            "total_seconds_raw": total_seconds,
            "segment_count": segment_count,
        }
        _stats_cache = result
        _stats_cache_time = now
        return result


# --- Page view analytics ---

async def cleanup_old_analytics(days: int = 90):
    """Delete page_views and search_logs older than N days."""
    async with get_db() as db:
        await db.execute(
            "DELETE FROM page_views WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        await db.execute(
            "DELETE FROM search_logs WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        await db.commit()


async def log_page_view(
    path: str,
    query_string: str | None,
    referrer: str | None,
    user_agent: str | None,
    ip: str | None,
):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO page_views (path, query_string, referrer, user_agent, ip)
               VALUES (?, ?, ?, ?, ?)""",
            (path, query_string, referrer, user_agent, ip),
        )
        await db.commit()


async def get_analytics(days: int = 30) -> dict:
    async with get_db() as db:
        # Total views
        c = await db.execute(
            "SELECT COUNT(*) FROM page_views "
            "WHERE created_at >= datetime('now', ?)",
            (f"-{days} days",),
        )
        total_views = (await c.fetchone())[0]

        # Unique IPs (approximate unique visitors)
        c = await db.execute(
            "SELECT COUNT(DISTINCT ip) FROM page_views "
            "WHERE created_at >= datetime('now', ?)",
            (f"-{days} days",),
        )
        unique_visitors = (await c.fetchone())[0]

        # Views per day
        c = await db.execute(
            """SELECT date(created_at) AS day, COUNT(*) AS views
               FROM page_views
               WHERE created_at >= datetime('now', ?)
               GROUP BY day ORDER BY day""",
            (f"-{days} days",),
        )
        views_per_day = [dict(r) for r in await c.fetchall()]

        # Top pages
        c = await db.execute(
            """SELECT path, COUNT(*) AS views
               FROM page_views
               WHERE created_at >= datetime('now', ?)
               GROUP BY path ORDER BY views DESC LIMIT 20""",
            (f"-{days} days",),
        )
        top_pages = [dict(r) for r in await c.fetchall()]

        # Top search queries
        c = await db.execute(
            """SELECT query_string AS query, COUNT(*) AS count
               FROM page_views
               WHERE path = '/' AND query_string IS NOT NULL
                 AND query_string != ''
                 AND created_at >= datetime('now', ?)
               GROUP BY query_string ORDER BY count DESC LIMIT 20""",
            (f"-{days} days",),
        )
        top_searches = [dict(r) for r in await c.fetchall()]

        # Top referrers
        c = await db.execute(
            """SELECT referrer, COUNT(*) AS count
               FROM page_views
               WHERE referrer IS NOT NULL AND referrer != ''
                 AND created_at >= datetime('now', ?)
               GROUP BY referrer ORDER BY count DESC LIMIT 20""",
            (f"-{days} days",),
        )
        top_referrers = [dict(r) for r in await c.fetchall()]

        return {
            "days": days,
            "total_views": total_views,
            "unique_visitors": unique_visitors,
            "views_per_day": views_per_day,
            "top_pages": top_pages,
            "top_searches": top_searches,
            "top_referrers": top_referrers,
        }


# --- Search logging ---

async def log_search(query: str, result_count: int):
    """Log a search query for analytics."""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO search_logs (query, result_count) VALUES (?, ?)",
            (query, result_count),
        )
        await db.commit()


async def get_search_analytics(days: int = 30) -> dict:
    """Get search analytics: top queries, zero-result queries."""
    async with get_db() as db:
        # Total searches
        c = await db.execute(
            "SELECT COUNT(*) FROM search_logs "
            "WHERE created_at >= datetime('now', ?)",
            (f"-{days} days",),
        )
        total_searches = (await c.fetchone())[0]

        # Top queries
        c = await db.execute(
            """SELECT query, COUNT(*) AS count,
                      ROUND(AVG(result_count), 0) AS avg_results
               FROM search_logs
               WHERE created_at >= datetime('now', ?)
               GROUP BY LOWER(query)
               ORDER BY count DESC
               LIMIT 30""",
            (f"-{days} days",),
        )
        top_queries = [dict(r) for r in await c.fetchall()]

        # Zero-result queries
        c = await db.execute(
            """SELECT query, COUNT(*) AS count
               FROM search_logs
               WHERE result_count = 0
                 AND created_at >= datetime('now', ?)
               GROUP BY LOWER(query)
               ORDER BY count DESC
               LIMIT 30""",
            (f"-{days} days",),
        )
        zero_results = [dict(r) for r in await c.fetchall()]

        # Searches per day
        c = await db.execute(
            """SELECT date(created_at) AS day, COUNT(*) AS count
               FROM search_logs
               WHERE created_at >= datetime('now', ?)
               GROUP BY day ORDER BY day""",
            (f"-{days} days",),
        )
        per_day = [dict(r) for r in await c.fetchall()]

        return {
            "days": days,
            "total_searches": total_searches,
            "top_queries": top_queries,
            "zero_results": zero_results,
            "per_day": per_day,
        }
