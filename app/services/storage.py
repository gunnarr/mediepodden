"""MinIO / S3-compatible audio storage helpers.

Provides upload/download/exists for episode audio files stored in a
self-hosted MinIO bucket (or any S3-compatible service). Boto3 calls are
wrapped with asyncio.to_thread() so they don't block the event loop.
The client is lazy-initialized only when MINIO_ENDPOINT is configured.
"""

import asyncio
import logging
import tempfile
import threading
from pathlib import Path

from app.config import (
    MINIO_ACCESS_KEY,
    MINIO_BUCKET,
    MINIO_ENDPOINT,
    MINIO_REGION,
    MINIO_SECRET_KEY,
)

logger = logging.getLogger(__name__)

_client = None
_lock = threading.Lock()


def _get_client():
    """Lazy-initialize the boto3 S3 client pointed at MINIO_ENDPOINT."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                import boto3
                _client = boto3.client(
                    "s3",
                    endpoint_url=MINIO_ENDPOINT,
                    aws_access_key_id=MINIO_ACCESS_KEY,
                    aws_secret_access_key=MINIO_SECRET_KEY,
                    region_name=MINIO_REGION,
                )
    return _client


def is_configured() -> bool:
    """True when MinIO is fully configured and usable."""
    return bool(MINIO_ENDPOINT and MINIO_BUCKET and MINIO_ACCESS_KEY and MINIO_SECRET_KEY)


def reset_client_for_tests() -> None:
    """Clear the cached client so tests can rebind config."""
    global _client
    with _lock:
        _client = None


async def upload_audio(local_path: Path, key: str) -> None:
    """Upload a local audio file to the configured bucket."""
    if not is_configured():
        raise RuntimeError("MinIO storage is not configured")
    client = _get_client()
    logger.info("Uploading %s to %s/%s", local_path.name, MINIO_BUCKET, key)
    await asyncio.to_thread(client.upload_file, str(local_path), MINIO_BUCKET, key)


async def download_audio(key: str) -> Path:
    """Download an audio object to a temp file. Caller must unlink when done."""
    if not is_configured():
        raise RuntimeError("MinIO storage is not configured")
    client = _get_client()
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    logger.info("Downloading %s/%s", MINIO_BUCKET, key)
    try:
        await asyncio.to_thread(client.download_file, MINIO_BUCKET, key, str(tmp_path))
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


async def audio_exists(key: str) -> bool:
    """Return True if an object exists in the bucket."""
    if not is_configured():
        return False
    client = _get_client()
    try:
        await asyncio.to_thread(client.head_object, Bucket=MINIO_BUCKET, Key=key)
        return True
    except Exception:
        return False


async def audio_size(key: str) -> int | None:
    """Return object size in bytes, or None if it doesn't exist."""
    if not is_configured():
        return None
    client = _get_client()
    try:
        head = await asyncio.to_thread(client.head_object, Bucket=MINIO_BUCKET, Key=key)
        return int(head["ContentLength"])
    except Exception:
        return None
