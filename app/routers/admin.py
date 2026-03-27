"""Admin router for RSS import and cloud transcription."""

import secrets
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.status import HTTP_303_SEE_OTHER, HTTP_401_UNAUTHORIZED

from app.config import ADMIN_PASSWORD, TranscriptionStatus
from app.database import (
    create_episode,
    find_episode_by_feed,
    get_setting,
    get_stats,
    set_setting,
    update_episode,
)
from app.rate_limit import limiter
from app.templating import templates
from app.services.feed import parse_feed
from app.services.feed_monitor import get_next_check
from app.services.transcription import (
    clear_completed_jobs,
    get_active_jobs,
    start_transcription,
)

router = APIRouter(prefix="/admin", tags=["admin"])

security = HTTPBasic()


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify admin credentials via HTTP Basic Auth."""
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Admin är inte aktiverat")
    valid = secrets.compare_digest(credentials.username, "admin")
    valid = secrets.compare_digest(credentials.password, ADMIN_PASSWORD) and valid
    if not valid:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Fel lösenord",
            headers={"WWW-Authenticate": 'Basic realm="Admin"'},
        )
    return credentials


async def _enrich_with_db_status(episodes: list[dict]) -> list[dict]:
    """Add database status to each feed episode."""
    for ep in episodes:
        db_ep = await find_episode_by_feed(ep)

        if db_ep:
            ep["db_id"] = db_ep["id"]
            ep["db_status"] = db_ep["transcription_status"]
        else:
            ep["db_id"] = None
            ep["db_status"] = None

    return episodes


@router.get("", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def admin_page(request: Request, _=Depends(verify_admin)):
    feed_url = await get_setting("feed_url") or ""
    feed_episodes = []
    if feed_url:
        try:
            feed_episodes = parse_feed(feed_url)
            feed_episodes = await _enrich_with_db_status(feed_episodes)
        except Exception:
            feed_episodes = []

    jobs = get_active_jobs()

    # Stats from DB
    stats = await get_stats()
    total_episodes = stats["total_episodes"]
    completed = stats["episode_count"]
    remaining = total_episodes - completed

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "feed_url": feed_url,
            "episodes": feed_episodes,
            "jobs": jobs,
            "total_feed": total_episodes,
            "completed_count": completed,
            "remaining_count": remaining,
            "next_check": get_next_check() if feed_url else None,
        },
    )


@router.post("/feed")
@limiter.limit("5/minute")
async def save_feed(request: Request, _=Depends(verify_admin)):
    form = await request.form()
    feed_url = str(form.get("feed_url", "")).strip()
    if feed_url:
        if len(feed_url) > 2048:
            raise HTTPException(status_code=400, detail="URL för lång")
        parsed = urlparse(feed_url)
        if parsed.scheme not in ("http", "https"):
            raise HTTPException(status_code=400, detail="Bara HTTP/HTTPS tillåtet")
        hostname = (parsed.hostname or "").lower()
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            raise HTTPException(status_code=400, detail="Intern adress ej tillåten")
        _private_prefixes = ("169.254.", "10.", "192.168.", "172.16.", "172.17.",
                             "172.18.", "172.19.", "172.20.", "172.21.", "172.22.",
                             "172.23.", "172.24.", "172.25.", "172.26.", "172.27.",
                             "172.28.", "172.29.", "172.30.", "172.31.")
        if any(hostname.startswith(p) for p in _private_prefixes):
            raise HTTPException(status_code=400, detail="Intern adress ej tillåten")
        await set_setting("feed_url", feed_url)
    return RedirectResponse(url="/admin", status_code=HTTP_303_SEE_OTHER)


@router.post("/transkribera")
@limiter.limit("3/minute")
async def start_transcriptions(request: Request, _=Depends(verify_admin)):
    form = await request.form()
    selected = form.getlist("episodes")

    feed_url = await get_setting("feed_url") or ""
    if not feed_url:
        return RedirectResponse(url="/admin", status_code=HTTP_303_SEE_OTHER)

    feed_episodes = parse_feed(feed_url)
    feed_by_guid = {ep["guid"]: ep for ep in feed_episodes if ep["guid"]}

    for guid in selected:
        ep = feed_by_guid.get(guid)
        if not ep or not ep["audio_url"]:
            continue

        # Find or create episode in DB
        db_ep = await find_episode_by_feed(ep)

        if db_ep:
            # Skip already completed
            if db_ep["transcription_status"] == TranscriptionStatus.COMPLETED:
                continue
            episode_id = db_ep["id"]
            update_kwargs = {
                "audio_url": ep["audio_url"],
                "feed_guid": ep["guid"] or None,
            }
            if not db_ep["description"] and ep.get("description"):
                update_kwargs["description"] = ep["description"]
            await update_episode(episode_id, **update_kwargs)
        else:
            episode_id = await create_episode(
                title=ep["title"],
                episode_number=ep["episode_number"],
                description=ep.get("description") or None,
                audio_filename=None,
                published_date=ep["published"] or None,
            )
            await update_episode(
                episode_id,
                audio_url=ep["audio_url"],
                feed_guid=ep["guid"] or None,
            )

        await start_transcription(
            episode_id=episode_id,
            title=ep["title"],
            episode_number=ep["episode_number"],
            audio_url=ep["audio_url"],
        )

    return RedirectResponse(url="/admin", status_code=HTTP_303_SEE_OTHER)


@router.get("/status", response_class=HTMLResponse)
async def job_status(request: Request, _=Depends(verify_admin)):
    jobs = get_active_jobs()
    return templates.TemplateResponse(
        "partials/admin_status.html",
        {"request": request, "jobs": jobs},
    )


@router.post("/rensa")
async def clear_jobs(request: Request, _=Depends(verify_admin)):
    clear_completed_jobs()
    return RedirectResponse(url="/admin", status_code=HTTP_303_SEE_OTHER)
