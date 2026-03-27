"""Database schema, migrations, and initialization."""

import logging

import aiosqlite
from .connection import get_db

logger = logging.getLogger(__name__)

MIGRATIONS = [
    # Migration 1: Add columns for speaker support, progress, cancellation
    """
    -- Handled by legacy ALTER TABLE code (kept for reference)
    SELECT 1;
    """,
    # Migration 2: Add search_logs table and index on transcription_status
    """
    CREATE TABLE IF NOT EXISTS search_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT NOT NULL,
        result_count INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_search_logs_created
        ON search_logs(created_at);
    CREATE INDEX IF NOT EXISTS idx_episodes_status
        ON episodes(transcription_status);
    CREATE INDEX IF NOT EXISTS idx_segments_episode
        ON segments(episode_id);
    """,
    # Migration 3: Blog posts table
    """
    CREATE TABLE IF NOT EXISTS blog_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        entity_name TEXT NOT NULL,
        entity_category TEXT NOT NULL,
        published_date TEXT NOT NULL,
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_blog_posts_published ON blog_posts(published_date DESC);
    CREATE INDEX IF NOT EXISTS idx_blog_posts_slug ON blog_posts(slug);
    """,
    # Migration 4: Composite index for context segment lookups
    """
    CREATE INDEX IF NOT EXISTS idx_segments_episode_time
        ON segments(episode_id, start_time, end_time);
    """,
]


async def _get_schema_version(db) -> int:
    """Get current schema version from the database."""
    try:
        cursor = await db.execute(
            "SELECT value FROM settings WHERE key = 'schema_version'"
        )
        row = await cursor.fetchone()
        return int(row["value"]) if row else 0
    except aiosqlite.OperationalError:
        return 0


async def _set_schema_version(db, version: int):
    """Set the schema version in settings."""
    await db.execute(
        "INSERT INTO settings (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


async def init_db():
    async with get_db() as db:
        # Set WAL mode once (persists across connections)
        await db.execute("PRAGMA journal_mode=WAL")

        # Legacy migrations for existing databases (safe to run multiple times)
        for col, tbl, default in [
            ("speaker", "segments", None),
            ("speaker_label_0", "episodes", "'Talare 1'"),
            ("speaker_label_1", "episodes", "'Talare 2'"),
            ("transcription_progress", "episodes", "0"),
        ]:
            try:
                default_clause = f" DEFAULT {default}" if default else ""
                await db.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT{default_clause}"
                )
            except aiosqlite.OperationalError:
                pass  # Column already exists

        await db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE,
                title TEXT NOT NULL,
                episode_number INTEGER,
                description TEXT,
                audio_filename TEXT,
                audio_url TEXT,
                feed_guid TEXT UNIQUE,
                published_date TEXT,
                duration_seconds REAL,
                transcription_status TEXT DEFAULT 'pending',
                transcription_progress REAL DEFAULT 0,
                speaker_label_0 TEXT DEFAULT 'Talare 1',
                speaker_label_1 TEXT DEFAULT 'Talare 2',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS page_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                query_string TEXT,
                referrer TEXT,
                user_agent TEXT,
                ip TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_page_views_created
                ON page_views(created_at);
            CREATE INDEX IF NOT EXISTS idx_page_views_path
                ON page_views(path);

            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL,
                text TEXT NOT NULL,
                speaker TEXT,
                FOREIGN KEY (episode_id) REFERENCES episodes(id) ON DELETE CASCADE
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
                text,
                content='segments',
                content_rowid='id',
                tokenize='unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
                INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
            END;

            CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
                INSERT INTO segments_fts(segments_fts, rowid, text)
                    VALUES('delete', old.id, old.text);
            END;

            CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
                INSERT INTO segments_fts(segments_fts, rowid, text)
                    VALUES('delete', old.id, old.text);
                INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
            END;
        """)
        await db.commit()

        # Run numbered migrations
        current_version = await _get_schema_version(db)
        for i, migration_sql in enumerate(MIGRATIONS, start=1):
            if i > current_version:
                logger.info("Running migration %d", i)
                await db.executescript(migration_sql)
                await _set_schema_version(db, i)
                await db.commit()
                logger.info("Migration %d complete", i)
