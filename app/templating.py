"""Shared Jinja2 templates instance and context helper."""

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.database import get_stats
from app.filters import register_filters

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
register_filters(templates)


async def context(request: Request, **kwargs) -> dict:
    """Build a template context with stats and any extra kwargs."""
    stats = await get_stats()
    return {"request": request, "stats": stats, **kwargs}
