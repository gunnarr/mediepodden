"""Shared test fixtures for Mediepodden test suite."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

# Point database at a temporary file before importing app modules
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()

os.environ.setdefault("DATABASE_PATH", _tmp_db.name)
os.environ.setdefault("AUDIO_DIR", tempfile.mkdtemp())
os.environ.setdefault("SITE_USERNAME", "")
os.environ.setdefault("SITE_PASSWORD", "")

# Patch config before anything imports it
with patch.dict(os.environ, {
    "DATABASE_PATH": _tmp_db.name,
    "AUDIO_DIR": os.environ["AUDIO_DIR"],
    "SITE_USERNAME": "",
    "SITE_PASSWORD": "",
}):
    import app.config
    app.config.DATABASE_PATH = Path(_tmp_db.name)
    app.config.AUDIO_DIR = Path(os.environ["AUDIO_DIR"])
    app.config.SITE_USERNAME = ""
    app.config.SITE_PASSWORD = ""

from app.database import init_db, create_episode, invalidate_stats_cache, save_segments, update_episode


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initialize a fresh database for each test."""
    # Re-init tables
    await init_db()
    invalidate_stats_cache()
    yield
    # Clean up between tests
    invalidate_stats_cache()
    import aiosqlite
    db = await aiosqlite.connect(str(app.config.DATABASE_PATH))
    try:
        await db.executescript("""
            DELETE FROM segments;
            DELETE FROM episodes;
            DELETE FROM settings;
            DELETE FROM page_views;
            DELETE FROM search_logs;
            DELETE FROM blog_posts;  -- table kept for migration compat
        """)
        await db.commit()
    finally:
        await db.close()


@pytest_asyncio.fixture
async def sample_episode():
    """Create a sample episode and return its ID."""
    ep_id = await create_episode(
        title="Döden i Venedig",
        episode_number=42,
        description="Ett avsnitt om Thomas Manns novell",
        audio_filename=None,
        published_date="2024-06-15",
    )
    return ep_id


@pytest_asyncio.fixture
async def completed_episode(sample_episode):
    """Create a completed episode with segments."""
    ep_id = sample_episode
    await update_episode(
        ep_id,
        transcription_status="completed",
        duration_seconds=3600.0,
    )
    await save_segments(ep_id, [
        {"start": 0.0, "end": 5.0, "text": "Välkommen till Mediepodden", "speaker": "SPEAKER_0"},
        {"start": 5.0, "end": 12.0, "text": "Idag ska vi prata om döden i Venedig", "speaker": "SPEAKER_1"},
        {"start": 12.0, "end": 20.0, "text": "Thomas Mann skrev novellen 1912", "speaker": "SPEAKER_0"},
    ])
    return ep_id


@pytest.fixture
def test_client():
    """Create a FastAPI TestClient."""
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)
