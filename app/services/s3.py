"""S3 audio storage helpers.

Provides upload/download/exists for episode audio files stored in S3.
All boto3 calls are wrapped with asyncio.to_thread() for async compatibility.
The S3 client is lazy-initialized only when S3_AUDIO_BUCKET is configured.
"""

import asyncio
import logging
import tempfile
import threading
from pathlib import Path

from app.config import S3_AUDIO_BUCKET, S3_AUDIO_REGION

logger = logging.getLogger(__name__)

_s3_client = None
_s3_lock = threading.Lock()


def _get_client():
    """Lazy-initialize the boto3 S3 client (thread-safe)."""
    global _s3_client
    if _s3_client is None:
        with _s3_lock:
            if _s3_client is None:
                import boto3
                _s3_client = boto3.client("s3", region_name=S3_AUDIO_REGION)
    return _s3_client


def is_configured() -> bool:
    """Check if S3 audio storage is configured."""
    return bool(S3_AUDIO_BUCKET)


async def upload_audio(local_path: Path, s3_key: str) -> None:
    """Upload a local audio file to S3."""
    if not is_configured():
        return

    client = _get_client()
    logger.info("Uploading %s to s3://%s/%s", local_path.name, S3_AUDIO_BUCKET, s3_key)
    await asyncio.to_thread(
        client.upload_file, str(local_path), S3_AUDIO_BUCKET, s3_key
    )
    logger.info("Upload complete: %s", s3_key)


async def download_audio(s3_key: str) -> Path:
    """Download an audio file from S3 to a temporary file.

    The caller is responsible for deleting the temp file when done.
    """
    client = _get_client()
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    logger.info("Downloading s3://%s/%s", S3_AUDIO_BUCKET, s3_key)
    await asyncio.to_thread(
        client.download_file, S3_AUDIO_BUCKET, s3_key, str(tmp_path)
    )
    return tmp_path


async def audio_exists(s3_key: str) -> bool:
    """Check if an audio file exists in S3."""
    if not is_configured():
        return False

    client = _get_client()
    try:
        await asyncio.to_thread(
            client.head_object, Bucket=S3_AUDIO_BUCKET, Key=s3_key
        )
        return True
    except Exception:
        return False
