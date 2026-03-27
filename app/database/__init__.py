"""Database package — re-exports all public functions.

All existing ``from app.database import X`` imports continue to work.
"""

from .connection import get_db, slugify
from .schema import init_db
from .episodes import (
    create_episode,
    update_episode,
    delete_episode,
    get_episode,
    get_episode_by_slug,
    get_episode_by_guid,
    get_episode_by_number,
    find_episode_by_feed,
    get_next_episode_number,
    list_all_episodes,
    get_episode_segments,
    get_clip_context_segments,
    save_segments,
)
from .search import SORT_OPTIONS, search_segments, get_timeline_data
from .analytics import (
    invalidate_stats_cache,
    get_stats,
    log_page_view,
    get_analytics,
    log_search,
    get_search_analytics,
    cleanup_old_analytics,
)
from .settings import get_setting, set_setting

__all__ = [
    "get_db",
    "slugify",
    "init_db",
    "create_episode",
    "update_episode",
    "delete_episode",
    "get_episode",
    "get_episode_by_slug",
    "get_episode_by_guid",
    "get_episode_by_number",
    "find_episode_by_feed",
    "get_next_episode_number",
    "list_all_episodes",
    "get_episode_segments",
    "get_clip_context_segments",
    "save_segments",
    "SORT_OPTIONS",
    "search_segments",
    "get_timeline_data",
    "invalidate_stats_cache",
    "get_stats",
    "log_page_view",
    "get_analytics",
    "log_search",
    "get_search_analytics",
    "cleanup_old_analytics",
    "get_setting",
    "set_setting",
]
