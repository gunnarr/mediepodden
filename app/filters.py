"""Shared Jinja2 template filters and globals."""

import hashlib
import math
import re
from pathlib import Path

from markupsafe import Markup

_TITLE_PREFIX_RE = re.compile(r"^Mediepodden\s+\d+\s*[-–—]\s*")

# CSS cache-buster: hash computed once at import time
_css_path = Path(__file__).parent / "static" / "style.css"
CSS_HASH = hashlib.md5(_css_path.read_bytes()).hexdigest()[:8] if _css_path.exists() else ""


def format_timestamp(seconds: float) -> str:
    """Format seconds as h:mm:ss or m:ss."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def log_filter(value: float) -> float:
    """Natural logarithm, safe for values <= 0."""
    if value <= 0:
        return 0.0
    return math.log(value)


def short_title(title: str) -> str:
    """Strip 'Mediepodden 175 - ' prefix from episode title."""
    return _TITLE_PREFIX_RE.sub("", title)


_SAFE_BLOG_TAGS = re.compile(
    r"<(?!/?(?:p|h[1-6]|ul|ol|li|a|em|strong|blockquote|br|hr|code|pre|table|thead|tbody|tr|th|td)\b)[^>]+>",
    re.IGNORECASE,
)


def sanitize_html(html: str) -> Markup:
    """Strip dangerous HTML tags, keeping only safe formatting tags."""
    cleaned = _SAFE_BLOG_TAGS.sub("", html)
    return Markup(cleaned)


def register_filters(templates):
    """Register all custom filters and globals on a Jinja2Templates instance."""
    templates.env.filters["timestamp"] = format_timestamp
    templates.env.filters["log"] = log_filter
    templates.env.filters["short_title"] = short_title
    templates.env.filters["sanitize_html"] = sanitize_html
    templates.env.globals["css_hash"] = CSS_HASH
