import asyncio
import base64
import logging
import secrets

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
from starlette.requests import Request
from starlette.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware as _GZipMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import AUDIO_DIR, SITE_USERNAME, SITE_PASSWORD
from app.database import init_db, log_page_view
from app.database.analytics import cleanup_old_analytics
from app.routers import search, episodes, clips, admin, seo
from app.rate_limit import limiter
from app.services.feed_monitor import start_monitor
from app import health

logger = logging.getLogger(__name__)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Optional site-wide HTTP Basic Auth.

    Enabled when SITE_USERNAME and SITE_PASSWORD are set in the environment.
    """

    async def dispatch(self, request: Request, call_next):
        if not SITE_USERNAME:
            return await call_next(request)

        # Health check is public (for monitoring and deploy scripts)
        if request.url.path == "/health":
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
                username, password = decoded.split(":", 1)
                if (
                    secrets.compare_digest(username, SITE_USERNAME)
                    and secrets.compare_digest(password, SITE_PASSWORD)
                ):
                    return await call_next(request)
            except Exception:
                pass

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Mediepodden"'},
            content="Åtkomst nekad",
        )


async def _daily_cleanup():
    """Delete analytics data older than 90 days, once per day."""
    while True:
        await asyncio.sleep(86400)
        try:
            await cleanup_old_analytics(90)
            logger.info("Analytics cleanup completed (>90 days deleted)")
        except Exception:
            logger.exception("Analytics cleanup failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    # Run initial cleanup at startup
    try:
        await cleanup_old_analytics(90)
    except Exception:
        logger.exception("Initial analytics cleanup failed")
    monitor_task = start_monitor()
    cleanup_task = asyncio.create_task(_daily_cleanup())
    yield
    cleanup_task.cancel()
    monitor_task.cancel()


app = FastAPI(
    title="Mediepodden sök",
    description="Sök i alla avsnitt av Mediepodden",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

class AnalyticsMiddleware(BaseHTTPMiddleware):
    """Log page views for analytics."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # Only log GET requests to non-static paths
        if (
            request.method == "GET"
            and not request.url.path.startswith("/static")
            and request.url.path != "/health"
            and response.status_code < 400
        ):
            try:
                await log_page_view(
                    path=request.url.path,
                    query_string=request.url.query or None,
                    referrer=request.headers.get("referer"),
                    user_agent=request.headers.get("user-agent"),
                    ip=request.client.host if request.client else None,
                )
            except Exception:
                logger.debug("Failed to log page view for %s", request.url.path)
        return response


class CacheHeadersMiddleware(BaseHTTPMiddleware):
    """Set Cache-Control for static assets (versioned via ?v= hash)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://unpkg.com https://s.grj.se; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' blob: https://s.grj.se; "
            "media-src 'self' blob:; "
            "font-src 'self'; "
            "frame-ancestors 'none'"
        )
        if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


app.add_middleware(AnalyticsMiddleware)
app.add_middleware(CacheHeadersMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(BasicAuthMiddleware)

class _AudioSkipGZipMiddleware:
    """GZip middleware that skips compression for audio clip responses."""

    def __init__(self, app, minimum_size=500):
        self.app = app
        self.gzip = _GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/klipp/"):
            await self.app(scope, receive, send)
        else:
            await self.gzip(scope, receive, send)


app.add_middleware(_AudioSkipGZipMiddleware, minimum_size=500)

app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)

app.include_router(health.router)
app.include_router(search.router)
app.include_router(episodes.router)
app.include_router(clips.router)
app.include_router(admin.router)
app.include_router(seo.router)
