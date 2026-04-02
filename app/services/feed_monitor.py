"""Nightly RSS feed monitor.

Checks the saved RSS feed for new episodes at FEED_CHECK_HOUR (default 03:00)
and automatically starts cloud transcription for any new episodes found.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from app.config import FEED_CHECK_HOUR, TranscriptionStatus
from app.database import (
    create_episode,
    find_episode_by_feed,
    get_next_episode_number,
    get_setting,
    update_episode,
)
from app.services.feed import make_audio_filename, parse_feed
from app.services.transcription import start_transcription

logger = logging.getLogger(__name__)


async def check_feed() -> int:
    """Check RSS feed and start transcription for new episodes.

    Returns the number of new episodes queued for transcription.
    """
    feed_url = await get_setting("feed_url")
    if not feed_url:
        logger.debug("No feed_url configured, skipping feed check")
        return 0

    episodes = parse_feed(feed_url)
    queued = 0

    for ep in episodes:
        if not ep["audio_url"]:
            continue

        # Check if already in DB
        existing = await find_episode_by_feed(ep)
        if existing:
            # Retry failed episodes (e.g. download was blocked)
            if existing.get("transcription_status") == TranscriptionStatus.FAILED:
                logger.info("Retrying failed episode: %s", ep["title"])
                await start_transcription(
                    episode_id=existing["id"],
                    title=existing["title"],
                    episode_number=existing.get("episode_number"),
                    audio_url=ep["audio_url"],
                )
                queued += 1
            continue

        # New episode — create and start transcription
        episode_number = ep["episode_number"]
        if episode_number is None:
            episode_number = await get_next_episode_number()
            logger.info("Auto-assigned episode number %d to: %s", episode_number, ep["title"])
        logger.info("New episode found: %s (ep %s)", ep["title"], episode_number)
        audio_fname = make_audio_filename(
            episode_number, ep["published"] or None, ep["guid"],
        )
        episode_id = await create_episode(
            title=ep["title"],
            episode_number=episode_number,
            description=ep.get("description") or None,
            audio_filename=audio_fname,
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
            episode_number=episode_number,
            audio_url=ep["audio_url"],
        )
        queued += 1

    if queued:
        logger.info("Feed check complete: %d new episode(s) queued", queued)
    else:
        logger.debug("Feed check complete: no new episodes")

    return queued


def _next_run_at(hour: int) -> datetime:
    """Calculate the next occurrence of the given hour."""
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _seconds_until(hour: int) -> float:
    """Calculate seconds from now until the next occurrence of the given hour."""
    return (_next_run_at(hour) - datetime.now()).total_seconds()


def get_next_check() -> str:
    """Return a human-readable string for when the next feed check runs.

    Example: "i natt kl 03:00" or "ikv\u00e4ll kl 03:00"
    """
    target = _next_run_at(FEED_CHECK_HOUR)
    now = datetime.now()
    delta = target - now

    time_str = target.strftime("%H:%M")
    if delta.total_seconds() < 3600:
        minutes = int(delta.total_seconds() / 60)
        return f"om {minutes} min (kl {time_str})"

    hours = delta.total_seconds() / 3600
    if hours < 6:
        return f"om {int(hours)} tim (kl {time_str})"

    if target.date() == now.date():
        return f"idag kl {time_str}"

    return f"imorgon kl {time_str}"


async def _monitor_loop():
    """Sleep-loop that runs check_feed at FEED_CHECK_HOUR every day."""
    while True:
        wait = max(_seconds_until(FEED_CHECK_HOUR), 60)
        logger.info(
            "Feed monitor: next check in %.0f minutes (at %02d:00)",
            wait / 60,
            FEED_CHECK_HOUR,
        )
        await asyncio.sleep(wait)
        try:
            await check_feed()
            from app.health import record_feed_check
            record_feed_check()
        except Exception:
            logger.exception("Feed monitor check failed")
            from app.health import record_error
            record_error()


def start_monitor() -> asyncio.Task:
    """Start the feed monitor background task. Returns the asyncio.Task."""
    logger.info("Starting feed monitor (check hour: %02d:00)", FEED_CHECK_HOUR)
    return asyncio.create_task(_monitor_loop())
