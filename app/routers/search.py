import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.database import SORT_OPTIONS, get_timeline_data, log_search, search_segments
from app.config import PODCAST_URL
from app.rate_limit import limiter
from app.templating import templates, context

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

RESULTS_PER_PAGE = 50
MAX_QUERY_LEN = 500
MAX_PAGE = 100

# --- Entity cache (loaded once at startup) ---
_entities_cache: dict | None = None
ENTITIES_PATH = Path(__file__).parent.parent.parent / "data" / "entities.json"


def invalidate_entities_cache():
    """Reset the entities cache so it's reloaded on next request."""
    global _entities_cache
    _entities_cache = None


def _load_entities() -> dict | None:
    """Load entities from data/entities.json, cached in memory."""
    global _entities_cache
    if _entities_cache is not None:
        return _entities_cache
    if ENTITIES_PATH.exists():
        try:
            _entities_cache = json.loads(ENTITIES_PATH.read_text())
            logger.info("Loaded entities from %s", ENTITIES_PATH)
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load entities from %s", ENTITIES_PATH)
            _entities_cache = None
    return _entities_cache


def _parse_search_params(request: Request) -> dict:
    """Extract search parameters from query string."""
    q = request.query_params.get("q", "").strip()[:MAX_QUERY_LEN]
    page = request.query_params.get("sida", "1")
    sort = request.query_params.get("sort", "relevans")
    avsnitt = request.query_params.get("avsnitt", "")

    try:
        page = min(max(1, int(page)), MAX_PAGE)
    except (ValueError, TypeError):
        page = 1

    if sort not in SORT_OPTIONS:
        sort = "relevans"

    episode_filter = None
    if avsnitt:
        try:
            episode_filter = int(avsnitt)
        except (ValueError, TypeError):
            pass

    return {
        "q": q,
        "page": page,
        "sort": sort,
        "episode_filter": episode_filter,
    }


async def _do_search(params: dict) -> dict:
    """Perform search and return context dict."""
    q = params["q"]
    page = params["page"]
    sort = params["sort"]
    episode_filter = params.get("episode_filter")
    offset = (page - 1) * RESULTS_PER_PAGE

    # Build episode filter kwargs
    search_kwargs = {}
    if episode_filter is not None:
        search_kwargs["episode_from"] = episode_filter
        search_kwargs["episode_to"] = episode_filter

    results, total = await search_segments(
        q,
        limit=RESULTS_PER_PAGE,
        offset=offset,
        sort=sort,
        **search_kwargs,
    )

    # Timeline always shows full picture (no episode filter)
    timeline, max_episode = await get_timeline_data(q)
    max_hits = max(timeline.values()) if timeline else 0

    total_pages = max(1, (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)

    # Log search (fire and forget, non-blocking)
    try:
        await log_search(q, total)
    except Exception:
        pass

    return {
        "results": results,
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_prev": page > 1,
        "sort": sort,
        "timeline": timeline,
        "max_episode": max_episode,
        "max_hits": max_hits,
        "episode_filter": episode_filter,
    }


@router.get("/", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def index(request: Request, q: str = ""):
    params = _parse_search_params(request)
    search_ctx = {"sort": params["sort"]}
    if params["q"]:
        search_ctx = await _do_search(params)
    entities = _load_entities() if not params["q"] else None
    return templates.TemplateResponse(
        "index.html",
        await context(
            request,
            query=params["q"],
            entities=entities,
            **search_ctx,
        ),
    )


@router.get("/sok", response_class=HTMLResponse)
@limiter.limit("30/minute")
async def live_search(request: Request, q: str = ""):
    """HTMX partial endpoint for live search results."""
    params = _parse_search_params(request)
    search_ctx = {"results": [], "total": 0, "page": 1, "total_pages": 1,
                  "has_next": False, "has_prev": False, "sort": params["sort"]}
    if params["q"]:
        search_ctx = await _do_search(params)
    return templates.TemplateResponse(
        "partials/search_results.html",
        {
            "request": request,
            "query": params["q"],
            **search_ctx,
        },
    )


@router.get("/statistik", response_class=HTMLResponse)
async def statistik(request: Request):
    entities = _load_entities()
    return templates.TemplateResponse(
        "statistik.html",
        await context(request, entities=entities),
    )


@router.get("/statistik/tidslinje", response_class=HTMLResponse)
@limiter.limit("20/minute")
async def entity_timeline(request: Request, q: str = ""):
    """HTMX partial: timeline for a single entity on the statistics page."""
    q = q.strip()
    if not q:
        return HTMLResponse("")
    timeline, max_episode = await get_timeline_data(q)
    max_hits = max(timeline.values()) if timeline else 0
    total_mentions = sum(timeline.values())
    episode_count = len(timeline)
    return templates.TemplateResponse(
        "partials/entity_timeline.html",
        {
            "request": request,
            "query": q,
            "timeline": timeline,
            "max_episode": max_episode,
            "max_hits": max_hits,
            "total_mentions": total_mentions,
            "episode_count": episode_count,
        },
    )


@router.get("/om", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse(
        "about.html",
        await context(request, podcast_url=PODCAST_URL),
    )
