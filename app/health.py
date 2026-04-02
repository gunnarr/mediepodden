"""Health check endpoint with dependency checks, freshness, and error rate."""

import shutil
import subprocess
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import DATABASE_PATH
from app.database import get_db

router = APIRouter()

# --- Startup time ---
_startup_time = time.monotonic()


def get_uptime() -> int:
    return int(time.monotonic() - _startup_time)


# --- Version (git commit hash, resolved once at import) ---
try:
    _version = (
        subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        .decode()
        .strip()
    )
except Exception:
    _version = "unknown"


# --- Error rate tracking ---
_error_timestamps: deque[float] = deque()
ERROR_WINDOW = 300  # 5 minutes
ERROR_THRESHOLD = 10


def record_error():
    _error_timestamps.append(time.time())


def get_error_rate() -> int:
    cutoff = time.time() - ERROR_WINDOW
    while _error_timestamps and _error_timestamps[0] < cutoff:
        _error_timestamps.popleft()
    return len(_error_timestamps)


# --- Feed check freshness tracking ---
_last_feed_check: float | None = None
FRESHNESS_THRESHOLD_MINUTES = 36 * 60  # 36 hours (daily feed check)


def record_feed_check():
    global _last_feed_check
    _last_feed_check = time.time()


# --- Health endpoint ---


@router.get("/health")
async def health_check():
    """Health check for Uptime Kuma. No auth, no logging."""
    checks: dict = {}

    # 1. Database check
    try:
        async with get_db() as db:
            cursor = await db.execute("SELECT COUNT(*) FROM episodes")
            count = (await cursor.fetchone())[0]
            checks["database"] = {"status": "ok", "episodes": count}
    except Exception as e:
        checks["database"] = {"status": "error", "detail": str(e)}

    # 2. Disk space check
    try:
        usage = shutil.disk_usage(DATABASE_PATH.parent)
        free_gb = round(usage.free / (1024**3), 1)
        checks["disk"] = {
            "status": "ok" if free_gb > 1 else "warning",
            "free_gb": free_gb,
        }
    except Exception:
        checks["disk"] = {"status": "unknown"}

    # 3. Freshness check (last successful feed check)
    if _last_feed_check is not None:
        age_minutes = int((time.time() - _last_feed_check) / 60)
        threshold = FRESHNESS_THRESHOLD_MINUTES
        freshness_ok = age_minutes <= threshold
        last_dt = datetime.fromtimestamp(_last_feed_check, tz=timezone.utc)
        checks["freshness"] = {
            "status": "ok" if freshness_ok else "stale",
            "latest_check": last_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "age_minutes": age_minutes,
            "threshold_minutes": threshold,
        }
        if not freshness_ok:
            hours = age_minutes // 60
            checks["freshness"]["message"] = (
                f"Senaste feed-check är {hours} timmar gammalt"
            )
    else:
        # No feed check has run since startup — report age since startup
        uptime_min = get_uptime() // 60
        checks["freshness"] = {
            "status": "ok" if uptime_min < FRESHNESS_THRESHOLD_MINUTES else "stale",
            "latest_check": None,
            "age_minutes": None,
            "threshold_minutes": FRESHNESS_THRESHOLD_MINUTES,
            "message": "Ingen feed-check sedan uppstart",
        }

    # 4. Error rate check
    errors = get_error_rate()
    error_status = "ok" if errors < ERROR_THRESHOLD else "elevated"
    checks["error_rate"] = {
        "status": error_status,
        "errors_last_5min": errors,
        "threshold": ERROR_THRESHOLD,
    }
    if error_status != "ok":
        checks["error_rate"]["message"] = (
            f"{errors} fel senaste 5 minuterna"
        )

    all_ok = all(c.get("status") == "ok" for c in checks.values())
    status_code = 200 if all_ok else 503
    return JSONResponse(
        {
            "status": "ok" if all_ok else "error",
            "uptime": get_uptime(),
            "version": _version,
            "checks": checks,
        },
        status_code=status_code,
    )
