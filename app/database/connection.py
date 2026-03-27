"""Database connection and utility functions."""

import re
import unicodedata
from contextlib import asynccontextmanager

import aiosqlite
from app.config import DATABASE_PATH


def slugify(text: str) -> str:
    """Generate a URL-safe slug from text, handling Swedish characters."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:120]


@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        await db.close()
