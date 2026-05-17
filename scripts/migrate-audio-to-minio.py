#!/usr/bin/env python3
"""Upload existing full-length MP3s from local disk to MinIO.

Idempotent: skips episodes already present in the bucket (same size).
Does NOT delete the local files — verify migration first, then remove
manually once confident.

    python scripts/migrate-audio-to-minio.py --dry-run    # preview
    python scripts/migrate-audio-to-minio.py              # do it
    python scripts/migrate-audio-to-minio.py --reupload   # force re-upload
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from app.config import AUDIO_DIR
from app.database import get_db, init_db
from app.services import storage

logger = logging.getLogger(__name__)


async def list_candidates() -> list[dict]:
    """Return episodes that have an audio_filename, ordered by ep number."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, episode_number, audio_filename "
            "FROM episodes WHERE audio_filename IS NOT NULL "
            "ORDER BY episode_number"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def migrate(dry_run: bool, reupload: bool) -> tuple[int, int, int, int]:
    """Upload candidates. Returns (uploaded, skipped, missing, failed)."""
    if not storage.is_configured():
        logger.error("MinIO is not configured. Set MINIO_ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY in .env.")
        return (0, 0, 0, 0)

    episodes = await list_candidates()
    logger.info("Found %d episodes with audio_filename in DB", len(episodes))

    uploaded = skipped = missing = failed = 0

    for i, ep in enumerate(episodes, 1):
        key = ep["audio_filename"]
        local_path = AUDIO_DIR / key
        prefix = f"[{i:>3}/{len(episodes)}] ep {ep['episode_number']}"

        if not local_path.exists():
            logger.warning("%s  MISSING locally: %s", prefix, key)
            missing += 1
            continue

        local_size = local_path.stat().st_size

        if not reupload:
            remote_size = await storage.audio_size(key)
            if remote_size is not None:
                if remote_size == local_size:
                    logger.info("%s  skip (already present, %d bytes)", prefix, remote_size)
                    skipped += 1
                    continue
                logger.warning(
                    "%s  remote size differs (local=%d remote=%d) — re-uploading",
                    prefix, local_size, remote_size,
                )

        if dry_run:
            logger.info("%s  would upload %s (%.1f MB)", prefix, key, local_size / 1e6)
            uploaded += 1
            continue

        try:
            await storage.upload_audio(local_path, key)
            uploaded += 1
            logger.info("%s  uploaded %s (%.1f MB)", prefix, key, local_size / 1e6)
        except Exception:
            logger.exception("%s  upload failed for %s", prefix, key)
            failed += 1

    return uploaded, skipped, missing, failed


async def main():
    parser = argparse.ArgumentParser(description="Migrate audio files to MinIO")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen")
    parser.add_argument("--reupload", action="store_true", help="Upload even if remote exists")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    await init_db()
    uploaded, skipped, missing, failed = await migrate(args.dry_run, args.reupload)

    logger.info("---")
    logger.info("Summary: %d uploaded, %d skipped, %d missing locally, %d failed",
                uploaded, skipped, missing, failed)
    if missing:
        logger.warning("%d episodes had no local audio file — they need to be re-downloaded first.", missing)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
