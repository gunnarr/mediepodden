"""Shared rate limiter instance (SlowAPI)."""

from slowapi import Limiter
from starlette.requests import Request


def _get_real_ip(request: Request) -> str:
    """Extract real client IP from X-Forwarded-For (behind Nginx proxy)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


limiter = Limiter(
    key_func=_get_real_ip,
    default_limits=["120/minute"],
)
