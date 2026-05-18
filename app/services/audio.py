"""Server-side audio clip generation using ffmpeg.

Generates short MP3 clips from full episode audio files, with caching.
Used to let users preview search result segments without exposing full episodes.
"""

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from app.config import AUDIO_DIR
from app.services import storage

logger = logging.getLogger(__name__)

CLIP_DIR = AUDIO_DIR / "clips"
MAX_CLIP_DURATION = 60  # Maximum clip length in seconds
CLIP_PADDING = 2  # Seconds of padding before/after segment (in ffmpeg)
FADE_DURATION = 1  # Seconds of fade in/out
CLIP_BITRATE = "128k"
CLIP_SAMPLE_RATE = 44100
WAVEFORM_SAMPLE_RATE = 8000
FFMPEG_CLIP_TIMEOUT = 30  # seconds
FFMPEG_WAVEFORM_TIMEOUT = 15  # seconds
# Extra audio on each side of the clip's time range when fetching a Range from
# MinIO. Compensates for VBR encoders that don't strictly hit CBR bitrate. 5s
# is conservative — for ~130-min episodes this is <0.1% of the file.
RANGE_SAFETY_SECONDS = 5


def get_audio_path(episode: dict) -> Path | None:
    """Get the local audio file path for an episode."""
    if episode.get("audio_filename"):
        path = AUDIO_DIR / episode["audio_filename"]
        if path.exists():
            return path
    return None


def _clip_cache_path(episode_id: int, start: float, end: float) -> Path:
    """Generate a deterministic cache path for a clip."""
    key = f"{episode_id}:{start:.1f}:{end:.1f}"
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return CLIP_DIR / f"{episode_id}_{h}.mp3"


@asynccontextmanager
async def _audio_file(episode: dict):
    """Yield a readable Path to the episode audio (full file).

    Tries the local disk first (fast path). Falls back to downloading the
    entire object from MinIO into a temp file, which is removed when the
    context exits. Yields None if neither source produces a file.

    Prefer `_prepare_audio_for_clip()` when you only need a short window —
    it downloads just the relevant byte range from MinIO and is ~10x faster
    on cold cache.
    """
    local = get_audio_path(episode)
    if local:
        yield local
        return

    key = episode.get("audio_filename")
    if key and storage.is_configured():
        try:
            tmp = await storage.download_audio(key)
        except Exception:
            logger.exception("Failed to download %s from MinIO", key)
            yield None
            return
        try:
            yield tmp
        finally:
            tmp.unlink(missing_ok=True)
        return

    yield None


@asynccontextmanager
async def _prepare_audio_for_clip(
    episode: dict,
    clip_start_s: float,
    clip_end_s: float,
):
    """Yield (path, offset_seconds) where ffmpeg can read audio for the clip.

    Three resolution paths, in order:
      1. Local disk hit              → (local_path, 0.0)            full file
      2. MinIO Range (fast)          → (tmp_path,  range_start_s)   partial file
      3. MinIO full download (slow)  → (tmp_path,  0.0)             full file

    `offset_seconds` is how far into the original audio the returned file
    starts. ffmpeg should seek with `-ss (clip_start_s - offset_seconds)`.

    Path 2 is taken only when `audio_filename` and `duration_seconds` are
    both present on the episode (needed to convert time → byte range via
    CBR approximation). On any Range failure, falls through to path 3.

    Temp files are deleted when the context exits.
    """
    local = get_audio_path(episode)
    if local:
        yield local, 0.0
        return

    key = episode.get("audio_filename")
    if not key or not storage.is_configured():
        yield None, 0.0
        return

    duration = episode.get("duration_seconds")
    if duration and duration > 0:
        try:
            size = await storage.audio_size(key)
            if size:
                bitrate_bps = size * 8 / duration
                range_start_s = max(0.0, clip_start_s - RANGE_SAFETY_SECONDS)
                range_end_s = min(float(duration), clip_end_s + RANGE_SAFETY_SECONDS)
                byte_start = int(range_start_s * bitrate_bps / 8)
                byte_end = int(range_end_s * bitrate_bps / 8) - 1
                if byte_end > byte_start:
                    tmp = await storage.download_audio_range(key, byte_start, byte_end)
                    try:
                        yield tmp, range_start_s
                        return
                    finally:
                        tmp.unlink(missing_ok=True)
        except Exception:
            logger.exception(
                "Range download for %s failed, falling back to full", key
            )

    # Fallback: full download
    try:
        tmp = await storage.download_audio(key)
    except Exception:
        logger.exception("Failed to download %s from MinIO (full)", key)
        yield None, 0.0
        return
    try:
        yield tmp, 0.0
    finally:
        tmp.unlink(missing_ok=True)


async def get_or_create_clip(
    episode: dict,
    episode_id: int,
    start: float,
    end: float,
) -> Path | None:
    """Get a cached clip or create one with ffmpeg.

    Args:
        episode: Episode dict with audio_filename
        episode_id: Episode ID (for cache naming)
        start: Start time in seconds
        end: End time in seconds

    Returns:
        Path to the clip MP3 file, or None on failure.
    """
    # Clamp duration
    duration = min(end - start + 2 * CLIP_PADDING, MAX_CLIP_DURATION)
    clip_start = max(0, start - CLIP_PADDING)
    clip_end = clip_start + duration

    cache_path = _clip_cache_path(episode_id, start, end)

    if cache_path.exists():
        return cache_path

    async with _prepare_audio_for_clip(episode, clip_start, clip_end) as (audio_path, offset_s):
        if not audio_path:
            logger.warning("No audio available for episode %d", episode_id)
            return None

        # Seek into the (possibly partial) file: the file starts at offset_s
        # into the original audio, so we subtract that.
        ffmpeg_seek = max(0.0, clip_start - offset_s)

        try:
            CLIP_DIR.mkdir(parents=True, exist_ok=True)

            fade_out_start = max(0, duration - FADE_DURATION)
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{ffmpeg_seek:.2f}",
                "-t", f"{duration:.2f}",
                "-i", str(audio_path),
                "-af", f"afade=in:d={FADE_DURATION},afade=out:st={fade_out_start:.2f}:d={FADE_DURATION}",
                "-acodec", "libmp3lame",
                "-ab", CLIP_BITRATE,
                "-ar", str(CLIP_SAMPLE_RATE),
                "-ac", "1",  # Mono to save bandwidth
                str(cache_path),
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_CLIP_TIMEOUT)

            if proc.returncode != 0:
                logger.error("ffmpeg failed: %s", stderr.decode()[-500:])
                cache_path.unlink(missing_ok=True)
                return None

            return cache_path

        except asyncio.TimeoutError:
            logger.error("ffmpeg timed out generating clip")
            cache_path.unlink(missing_ok=True)
            return None
        except FileNotFoundError:
            logger.error("ffmpeg not found. Install ffmpeg to enable audio clips.")
            return None


async def generate_waveform_data(
    episode: dict,
    episode_id: int,
    start: float,
    end: float,
    num_peaks: int = 100,
) -> list[float] | None:
    """Generate waveform peak data for a clip using ffmpeg.

    Returns a list of normalized peak values (0.0-1.0) for rendering,
    or None on failure.
    """
    clip_path = await get_or_create_clip(episode, episode_id, start, end)
    if not clip_path:
        return None

    # Use ffmpeg to extract raw PCM and compute peaks
    cmd = [
        "ffmpeg", "-y",
        "-i", str(clip_path),
        "-f", "s16le",  # raw 16-bit PCM
        "-ac", "1",
        "-ar", str(WAVEFORM_SAMPLE_RATE),
        "-",  # Output to stdout
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_WAVEFORM_TIMEOUT)

        if proc.returncode != 0 or not stdout:
            return None

        # Convert bytes to 16-bit samples
        import struct
        samples = struct.unpack(f"<{len(stdout)//2}h", stdout)
        if not samples:
            return None

        # Divide into buckets and take absolute peak of each
        bucket_size = max(1, len(samples) // num_peaks)
        peaks = []
        for i in range(0, len(samples), bucket_size):
            bucket = samples[i:i + bucket_size]
            peak = max(abs(s) for s in bucket) if bucket else 0
            peaks.append(peak)

        # Normalize to 0.0-1.0
        max_peak = max(peaks) if peaks else 1
        if max_peak == 0:
            max_peak = 1
        return [round(p / max_peak, 3) for p in peaks[:num_peaks]]

    except (asyncio.TimeoutError, FileNotFoundError):
        return None
