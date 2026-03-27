"""Full-text search and timeline queries."""

from markupsafe import escape

from .connection import get_db

SORT_OPTIONS = {
    "relevans": "rank",
    "aldst": "e.published_date ASC, s.start_time ASC",
    "nyast": "e.published_date DESC, s.start_time DESC",
}


async def search_segments(
    query: str,
    limit: int = 50,
    offset: int = 0,
    speaker: str | None = None,
    episode_from: int | None = None,
    episode_to: int | None = None,
    sort: str = "relevans",
) -> tuple[list[dict], int]:
    """Search segments with optional filters.

    Returns (results, total_count) where total_count is the full count
    before LIMIT/OFFSET.
    """
    order_clause = SORT_OPTIONS.get(sort, "rank")

    async with get_db() as db:
        where_extra = []
        params: list = [query]

        if speaker:
            where_extra.append("s.speaker = ?")
            params.append(speaker)
        if episode_from is not None:
            where_extra.append("e.episode_number >= ?")
            params.append(episode_from)
        if episode_to is not None:
            where_extra.append("e.episode_number <= ?")
            params.append(episode_to)

        extra_clause = ""
        if where_extra:
            extra_clause = "AND " + " AND ".join(where_extra)

        # Get total count
        count_cursor = await db.execute(
            f"""
            SELECT COUNT(*)
            FROM segments_fts
            JOIN segments s ON s.id = segments_fts.rowid
            JOIN episodes e ON e.id = s.episode_id
            WHERE segments_fts MATCH ?
            {extra_clause}
            """,
            params,
        )
        total = (await count_cursor.fetchone())[0]

        # Get results with offset/limit
        cursor = await db.execute(
            f"""
            SELECT
                s.id,
                s.episode_id,
                s.start_time,
                s.end_time,
                s.text,
                s.speaker,
                e.slug AS episode_slug,
                e.title AS episode_title,
                e.episode_number,
                e.published_date,
                e.speaker_label_0,
                e.speaker_label_1,
                highlight(segments_fts, 0, '<mark>', '</mark>') AS highlighted_text,
                rank
            FROM segments_fts
            JOIN segments s ON s.id = segments_fts.rowid
            JOIN episodes e ON e.id = s.episode_id
            WHERE segments_fts MATCH ?
            {extra_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
        results = [dict(row) for row in rows]

        # Batch-fetch context segments for all results (avoids N+1 queries)
        clip_padding = 10
        if results:
            # Build time windows per result
            windows = []
            for r in results:
                windows.append({
                    "episode_id": r["episode_id"],
                    "clip_start": max(r["start_time"] - clip_padding, 0),
                    "clip_end": r["end_time"] + clip_padding,
                })

            # Fetch all context segments in one query per unique episode
            episode_ids = list({r["episode_id"] for r in results})
            placeholders = ",".join("?" * len(episode_ids))
            ctx_cursor = await db.execute(
                f"""
                SELECT id, episode_id, text, start_time, end_time
                FROM segments
                WHERE episode_id IN ({placeholders})
                ORDER BY episode_id, start_time
                """,
                episode_ids,
            )
            all_ctx = await ctx_cursor.fetchall()

            # Index by episode_id for fast lookup
            ctx_by_episode: dict[int, list] = {}
            for row in all_ctx:
                ctx_by_episode.setdefault(row["episode_id"], []).append(row)

            # Build context_text for each result
            for r, w in zip(results, windows):
                ep_segments = ctx_by_episode.get(r["episode_id"], [])
                parts = []
                for seg in ep_segments:
                    if seg["end_time"] <= w["clip_start"] or seg["start_time"] >= w["clip_end"]:
                        continue
                    if seg["id"] == r["id"]:
                        parts.append(r["highlighted_text"])
                    else:
                        parts.append(str(escape(seg["text"])))
                r["context_text"] = " ".join(parts)

        return results, total


async def get_timeline_data(query: str) -> tuple[dict[int, int], int]:
    """Get per-episode hit counts for a search query.

    Returns ({episode_number: hit_count}, max_episode_number).
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT e.episode_number, COUNT(*) AS hits
            FROM segments_fts
            JOIN segments s ON s.id = segments_fts.rowid
            JOIN episodes e ON e.id = s.episode_id
            WHERE segments_fts MATCH ?
              AND e.episode_number IS NOT NULL
            GROUP BY e.episode_number
            ORDER BY e.episode_number
            """,
            (query,),
        )
        rows = await cursor.fetchall()
        timeline = {row["episode_number"]: row["hits"] for row in rows}

        cursor = await db.execute(
            "SELECT MAX(episode_number) FROM episodes WHERE episode_number IS NOT NULL"
        )
        row = await cursor.fetchone()
        max_ep = row[0] or 0

        return timeline, max_ep
