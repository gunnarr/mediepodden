"""Server-side audio clip generation using ffmpeg.

Generates short MP3 clips from full episode audio files, with caching.
Used to let users preview search result segments without exposing full episodes.
"""

import asyncio
import hashlib
import logging
from pathlib import Path

from app.config import AUDIO_DIR

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


async def _resolve_audio_path(episode: dict) -> Path | None:
    """Resolve audio path: local file first, then S3 download.

    Returns a Path to the audio file, or None if unavailable.
    If downloaded from S3, the caller must clean up the temp file.
    Returns a tuple-like behavior via the _s3_temp attribute on the path.
    """
    # Try local file first
    local = get_audio_path(episode)
    if local:
        return local

    # Try S3
    audio_filename = episode.get("audio_filename")
    if not audio_filename:
        return None

    from app.services.s3 import is_configured, audio_exists, download_audio

    if not is_configured():
        return None

    if not await audio_exists(audio_filename):
        logger.warning("Audio not found in S3: %s", audio_filename)
        return None

    try:
        tmp_path = await download_audio(audio_filename)
        return tmp_path
    except Exception:
        logger.exception("Failed to download from S3: %s", audio_filename)
        return None


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

    cache_path = _clip_cache_path(episode_id, start, end)

    if cache_path.exists():
        return cache_path

    # Resolve audio (local or S3)
    audio_path = await _resolve_audio_path(episode)
    if not audio_path:
        logger.warning("No audio available for episode %d", episode_id)
        return None

    # Track whether this is a temp file from S3
    is_local = get_audio_path(episode) is not None

    try:
        CLIP_DIR.mkdir(parents=True, exist_ok=True)

        fade_out_start = max(0, duration - FADE_DURATION)
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{clip_start:.2f}",
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
    finally:
        # Clean up S3 temp file
        if not is_local and audio_path and audio_path.exists():
            audio_path.unlink(missing_ok=True)


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
