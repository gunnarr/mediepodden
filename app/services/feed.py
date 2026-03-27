"""RSS feed parsing utilities shared between admin and feed monitor."""

import re
from datetime import datetime, timezone, timedelta
from html import unescape
from zoneinfo import ZoneInfo

import feedparser

_STOCKHOLM = ZoneInfo("Europe/Stockholm")

EPISODE_NUMBER_RE = re.compile(r"Mediepodden\s+(\d+)")
_TITLE_NUMBER_RE = re.compile(r"(?:Avsnitt|Episod)\s+(\d+)", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_PREMIUM_FEED_RE = re.compile(r"\s*Personlig premiumfeed för \S+")


def strip_html(text: str) -> str:
    """Remove HTML tags, decode entities, strip premium feed footer."""
    cleaned = unescape(_HTML_TAG_RE.sub("", text)).strip()
    return _PREMIUM_FEED_RE.sub("", cleaned).strip()


def parse_episode_number(title: str) -> int | None:
    """Extract episode number from title like 'Mediepodden 175', 'Avsnitt 72:', 'Episod 25:'."""
    m = EPISODE_NUMBER_RE.search(title)
    if m:
        return int(m.group(1))
    m = _TITLE_NUMBER_RE.search(title)
    return int(m.group(1)) if m else None


def make_audio_filename(
    episode_number: int | None,
    published: str | None,
    guid: str | None = None,
) -> str:
    """Generate a deterministic audio filename for an episode.

    Uses episode_number when available, otherwise a short hash of the guid
    to avoid collisions between unnumbered episodes.
    """
    if episode_number is not None:
        tag = str(episode_number)
    elif guid:
        import hashlib
        tag = hashlib.md5(guid.encode()).hexdigest()[:8]
    else:
        tag = "0"

    if published:
        return f"{published}-mediepodden-{tag}.mp3"
    return f"mediepodden-{tag}.mp3"


def parse_feed(feed_url: str) -> list[dict]:
    """Parse RSS feed and return episode list."""
    feed = feedparser.parse(feed_url)
    episodes = []
    for entry in feed.entries:
        title = entry.get("title", "")
        episode_number = parse_episode_number(title)
        # Fallback: itunes:episode tag (used by Patreon feeds etc.)
        if episode_number is None:
            itunes_ep = entry.get("itunes_episode", "")
            if itunes_ep and itunes_ep.isdigit():
                episode_number = int(itunes_ep)
        guid = entry.get("id", "")

        # Find audio URL from enclosures
        audio_url = ""
        for enc in entry.get("enclosures", []):
            if enc.get("type", "").startswith("audio/"):
                audio_url = enc.get("href", "")
                break

        # Fallback: check links
        if not audio_url:
            for link in entry.get("links", []):
                if link.get("type", "").startswith("audio/"):
                    audio_url = link.get("href", "")
                    break

        parsed_time = entry.get("published_parsed")
        if parsed_time:
            utc_dt = datetime(*parsed_time[:6], tzinfo=timezone.utc)
            published = utc_dt.astimezone(_STOCKHOLM).strftime("%Y-%m-%d")
        else:
            published = ""
        description = strip_html(entry.get("summary", ""))

        episodes.append({
            "title": title,
            "episode_number": episode_number,
            "guid": guid,
            "audio_url": audio_url,
            "published": published,
            "description": description,
        })

    # Sort by episode number (highest first), entries without number last
    episodes.sort(
        key=lambda e: (e["episode_number"] is not None, e["episode_number"] or 0),
        reverse=True,
    )
    return episodes
