"""In-memory transcription job queue.

Processes one job at a time using svenska-ord (Modal GPU).
"""

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import httpx

from app.config import TranscriptionStatus
from app.database import (
    get_episode,
    invalidate_stats_cache,
    save_segments,
    update_episode,
)

logger = logging.getLogger(__name__)


def _parse_date(date_str: str | None) -> str | None:
    """Parse a date string to YYYY-MM-DD format.

    Handles both ISO format (2024-01-15) and RFC 2822 (Sun, 27 Sep 2020 22:01:52 +0000).
    """
    if not date_str:
        return None

    # Already ISO format
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return date_str

    # Try RFC 2822 (from RSS feeds)
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None


class JobStage(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    SAVING = "saving"
    COMPLETED = "completed"
    FAILED = "failed"


STAGE_PROGRESS = {
    JobStage.QUEUED: 0,
    JobStage.DOWNLOADING: 5,
    JobStage.TRANSCRIBING: 20,
    JobStage.SAVING: 90,
    JobStage.COMPLETED: 100,
    JobStage.FAILED: 0,
}

STAGE_LABELS = {
    JobStage.QUEUED: "Väntar...",
    JobStage.DOWNLOADING: "Laddar ner ljud...",
    JobStage.TRANSCRIBING: "Transkriberar...",
    JobStage.SAVING: "Sparar segment...",
    JobStage.COMPLETED: "Klar!",
    JobStage.FAILED: "Misslyckades",
}


# Estimated transcription time in seconds (~8 min for a 2h episode)
ESTIMATED_TRANSCRIBE_SECONDS = 480
TRANSCRIBE_PROGRESS_MIN = 20
TRANSCRIBE_PROGRESS_MAX = 85


@dataclass
class TranscriptionJob:
    episode_id: int
    title: str
    episode_number: int | None
    audio_url: str
    stage: JobStage = JobStage.QUEUED
    _progress: int = 0
    error: str | None = None
    id: str = field(default="")
    _transcribe_started: float = 0.0

    def __post_init__(self):
        if not self.id:
            self.id = f"job-{self.episode_id}"

    @property
    def progress(self) -> int:
        if self.stage == JobStage.TRANSCRIBING and self._transcribe_started > 0:
            elapsed = time.time() - self._transcribe_started
            fraction = min(elapsed / ESTIMATED_TRANSCRIBE_SECONDS, 0.99)
            return TRANSCRIBE_PROGRESS_MIN + int(
                fraction * (TRANSCRIBE_PROGRESS_MAX - TRANSCRIBE_PROGRESS_MIN)
            )
        return self._progress

    @progress.setter
    def progress(self, value: int):
        self._progress = value

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, "")

    @property
    def is_done(self) -> bool:
        return self.stage in (JobStage.COMPLETED, JobStage.FAILED)


# Module-level state
MAX_QUEUE_SIZE = 500
MAX_COMPLETED_JOBS = 50
TRANSCRIBE_TIMEOUT = 1800  # 30 minutes
_queue: asyncio.Queue[TranscriptionJob] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
_active_jobs: dict[str, TranscriptionJob] = {}
_jobs_lock = asyncio.Lock()
_worker_task: asyncio.Task | None = None


def _update_stage(job: TranscriptionJob, stage: JobStage):
    job.stage = stage
    job.progress = STAGE_PROGRESS[stage]


async def _download_audio(job: TranscriptionJob) -> tuple[Path, str]:
    """Download audio to a temp file.

    Returns: (tmp_path, audio_filename)
    """
    _update_stage(job, JobStage.DOWNLOADING)
    await update_episode(
        job.episode_id,
        transcription_status=TranscriptionStatus.PROCESSING,
        transcription_progress=job.progress,
    )

    # Determine audio_filename
    episode = await get_episode(job.episode_id)
    existing_filename = episode.get("audio_filename") if episode else None

    if existing_filename:
        audio_filename = existing_filename
    else:
        from app.services.feed import make_audio_filename
        pub_date = _parse_date(episode.get("published_date")) if episode else None
        feed_guid = episode.get("feed_guid") if episode else None
        audio_filename = make_audio_filename(
            job.episode_number, pub_date, feed_guid,
        )

    logger.info("Downloading from URL: %s", job.audio_url)
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
        async with client.stream("GET", job.audio_url) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

    return tmp_path, audio_filename


async def _transcribe(job: TranscriptionJob, audio_path: Path) -> list[dict]:
    """Run transcription via svenska-ord (Modal GPU) using a local file."""
    _update_stage(job, JobStage.TRANSCRIBING)
    job._transcribe_started = time.time()
    await update_episode(
        job.episode_id, transcription_progress=job.progress
    )

    from svenska_ord import SvenskaOrd

    client = SvenskaOrd()
    result = await asyncio.wait_for(
        asyncio.to_thread(client.transcribe, str(audio_path)),
        timeout=TRANSCRIBE_TIMEOUT,
    )
    return result.to_segment_dicts()


async def _save_results(
    job: TranscriptionJob, segments: list[dict],
    audio_filename: str, audio_path: Path,
):
    """Save segments to DB."""
    _update_stage(job, JobStage.SAVING)
    await update_episode(
        job.episode_id, transcription_progress=job.progress
    )

    await save_segments(job.episode_id, segments)

    await update_episode(
        job.episode_id, audio_filename=audio_filename
    )

    # Calculate duration from segments
    if segments:
        duration = max(s["end"] for s in segments)
        await update_episode(job.episode_id, duration_seconds=duration)

    await update_episode(
        job.episode_id,
        transcription_status=TranscriptionStatus.COMPLETED,
        transcription_progress=100,
    )
    invalidate_stats_cache()


async def _process_job(job: TranscriptionJob):
    """Process a single transcription job through all stages."""
    tmp_path = None
    try:
        tmp_path, audio_filename = await _download_audio(job)
        segments = await _transcribe(job, tmp_path)
        await _save_results(job, segments, audio_filename, tmp_path)

        # Extract entities (non-fatal)
        try:
            from app.services.entities import extract_and_save
            await extract_and_save(job.episode_id, job.episode_number, job.title)
        except Exception:
            logger.warning("Entity extraction failed for episode %d", job.episode_id, exc_info=True)

        _update_stage(job, JobStage.COMPLETED)
    except Exception as e:
        logger.exception("Transcription failed for episode %d", job.episode_id)
        from app.health import record_error
        record_error()
        _update_stage(job, JobStage.FAILED)
        job.error = str(e)
        await update_episode(
            job.episode_id,
            transcription_status=TranscriptionStatus.FAILED,
            transcription_progress=0,
        )
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


async def _worker():
    """Background worker that processes jobs sequentially."""
    while True:
        job = await _queue.get()
        try:
            await _process_job(job)
        finally:
            _queue.task_done()


def ensure_worker_running():
    """Start the background worker if not already running."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


def _auto_cleanup_completed():
    """Remove oldest completed/failed jobs if over the cap."""
    done = [jid for jid, j in _active_jobs.items() if j.is_done]
    if len(done) > MAX_COMPLETED_JOBS:
        for jid in done[:len(done) - MAX_COMPLETED_JOBS]:
            del _active_jobs[jid]


async def start_transcription(
    episode_id: int,
    title: str,
    episode_number: int | None,
    audio_url: str,
) -> TranscriptionJob:
    """Queue a new transcription job. Returns existing job if already queued."""
    async with _jobs_lock:
        job_id = f"job-{episode_id}"

        # Dedup: return existing job if already active and not done
        existing = _active_jobs.get(job_id)
        if existing and not existing.is_done:
            return existing

        _auto_cleanup_completed()

        job = TranscriptionJob(
            episode_id=episode_id,
            title=title,
            episode_number=episode_number,
            audio_url=audio_url,
        )
        _active_jobs[job.id] = job

    await update_episode(
        episode_id,
        transcription_status=TranscriptionStatus.QUEUED,
        transcription_progress=0,
    )

    ensure_worker_running()
    try:
        _queue.put_nowait(job)
    except asyncio.QueueFull:
        logger.warning("Transcription queue full, dropping job for episode %d", episode_id)
        _update_stage(job, JobStage.FAILED)
        job.error = "Kön är full, försök igen senare"
    return job


def get_active_jobs() -> list[TranscriptionJob]:
    """Return all tracked jobs (including completed/failed)."""
    return list(_active_jobs.values())


def clear_completed_jobs():
    """Remove completed and failed jobs from tracking."""
    to_remove = [jid for jid, j in _active_jobs.items() if j.is_done]
    for jid in to_remove:
        del _active_jobs[jid]
