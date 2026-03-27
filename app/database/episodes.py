"""Episode and segment CRUD operations."""

from .connection import get_db, slugify


async def _unique_slug(db, base_slug: str) -> str:
    """Ensure slug uniqueness by appending a number if needed."""
    slug = base_slug
    counter = 2
    while True:
        cursor = await db.execute(
            "SELECT 1 FROM episodes WHERE slug = ?", (slug,)
        )
        if not await cursor.fetchone():
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1


async def create_episode(
    title: str,
    episode_number: int | None,
    description: str | None,
    audio_filename: str | None,
    published_date: str | None,
) -> int:
    async with get_db() as db:
        slug = await _unique_slug(db, slugify(title))
        cursor = await db.execute(
            """INSERT INTO episodes (slug, title, episode_number, description,
               audio_filename, published_date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (slug, title, episode_number, description, audio_filename, published_date),
        )
        await db.commit()
        return cursor.lastrowid


async def update_episode(episode_id: int, **kwargs):
    async with get_db() as db:
        allowed = {
            "slug", "title", "episode_number", "description", "audio_filename",
            "audio_url", "feed_guid",
            "published_date", "duration_seconds", "transcription_status",
            "transcription_progress", "speaker_label_0", "speaker_label_1",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [episode_id]
        await db.execute(
            f"UPDATE episodes SET {set_clause} WHERE id = ?", values
        )
        await db.commit()


async def delete_episode(episode_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM segments WHERE episode_id = ?", (episode_id,))
        await db.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
        await db.commit()


async def get_episode(episode_id: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_episode_by_slug(slug: str):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM episodes WHERE slug = ?", (slug,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_episode_by_guid(guid: str):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM episodes WHERE feed_guid = ?", (guid,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_episode_by_number(episode_number: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM episodes WHERE episode_number = ?", (episode_number,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def find_episode_by_feed(feed_ep: dict) -> dict | None:
    """Find an existing episode by feed guid or episode number."""
    if feed_ep.get("guid"):
        if db_ep := await get_episode_by_guid(feed_ep["guid"]):
            return db_ep
    if feed_ep.get("episode_number") is not None:
        return await get_episode_by_number(feed_ep["episode_number"])
    return None


async def get_next_episode_number() -> int:
    """Return the next available episode number (max + 1)."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT MAX(episode_number) FROM episodes WHERE episode_number IS NOT NULL"
        )
        row = await cursor.fetchone()
        return (row[0] or 0) + 1


async def list_all_episodes() -> list[dict]:
    """Return all episodes ordered by episode number, for the public episode list."""
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT * FROM episodes
               ORDER BY episode_number DESC NULLS LAST, created_at DESC"""
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_episode_segments(episode_id: int):
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM segments WHERE episode_id = ? ORDER BY start_time",
            (episode_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_clip_context_segments(episode_id: int, clip_start: float, clip_end: float):
    """Get all segments within a time window for an episode."""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT * FROM segments
            WHERE episode_id = ?
              AND end_time > ? AND start_time < ?
            ORDER BY start_time
            """,
            (episode_id, clip_start, clip_end),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def save_segments(episode_id: int, segments: list[dict]):
    async with get_db() as db:
        # Clear existing segments for this episode
        await db.execute(
            "DELETE FROM segments WHERE episode_id = ?", (episode_id,)
        )
        await db.executemany(
            """INSERT INTO segments (episode_id, start_time, end_time, text, speaker)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (episode_id, s["start"], s["end"], s["text"], s.get("speaker"))
                for s in segments
            ],
        )
        await db.commit()
