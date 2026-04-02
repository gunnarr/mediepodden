#!/usr/bin/env python3
"""
Transkribering av Mediepodden-avsnitt.

Skannar IN/ efter .mp3-filer, transkriberar och sparar till databasen.
Stödjer två lägen:
  - Lokalt (standard): mlx-whisper (GPU-accelererad på Apple Silicon) + pyannote
  - Moln (--cloud): svenska-ord (Modal GPU)

Filnamnsformat: "Mediepodden 175 - Var Jon Skolmen egentligen så rolig?.mp3"

Kör:
    python scripts/transcribe.py              # lokalt (mlx-whisper)
    python scripts/transcribe.py --cloud      # moln (Modal)
"""

import argparse
import asyncio
import logging
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Lägg till projektroten i sys.path så att app.database kan importeras
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from app.database import create_episode, get_db, init_db, save_segments, update_episode, invalidate_stats_cache
from app.config import TranscriptionStatus

logger = logging.getLogger(__name__)

IN_DIR = PROJECT_ROOT / "IN"
OUT_DIR = PROJECT_ROOT / "OUT"

# Whisper-inställningar (mlx-whisper med lokal MLX-konverterad KB-modell)
MLX_MODEL_PATH = str(PROJECT_ROOT / "cache" / "kb-whisper-large-mlx")
HUGGINGFACE_TOKEN = os.getenv("HUGGINGFACE_TOKEN", "")

_diarize_pipeline = None


def _get_diarize_pipeline():
    """Lazy-load the pyannote speaker diarization pipeline."""
    global _diarize_pipeline
    if _diarize_pipeline is None:
        if not HUGGINGFACE_TOKEN:
            logger.warning("HUGGINGFACE_TOKEN not set, diarization disabled")
            return None
        try:
            from pyannote.audio import Pipeline

            logger.info("Loading pyannote speaker diarization pipeline")
            _diarize_pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=HUGGINGFACE_TOKEN,
            )
        except ImportError:
            logger.warning("pyannote.audio not installed, diarization unavailable")
            return None
        except Exception:
            logger.exception("Failed to load diarization pipeline")
            return None
    return _diarize_pipeline


def _run_diarization(audio_path: str, num_speakers: int = 2) -> list[dict] | None:
    """Run speaker diarization and return speaker turns."""
    pipeline = _get_diarize_pipeline()
    if pipeline is None:
        return None

    try:
        logger.info("Running speaker diarization on %s", audio_path)
        diarization = pipeline(audio_path, num_speakers=num_speakers)

        turns = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            turns.append({
                "start": turn.start,
                "end": turn.end,
                "speaker": speaker,
            })

        logger.info("Diarization found %d speaker turns", len(turns))
        return turns
    except Exception:
        logger.exception("Diarization failed for %s", audio_path)
        return None


def _assign_speakers(segments: list[dict], turns: list[dict]) -> list[dict]:
    """Assign speaker labels to transcription segments based on diarization turns."""
    if not turns:
        return segments

    seen_speakers = {}
    counter = 0
    for t in turns:
        if t["speaker"] not in seen_speakers:
            seen_speakers[t["speaker"]] = f"SPEAKER_{counter}"
            counter += 1

    for seg in segments:
        seg_start = seg["start"]
        seg_end = seg["end"]
        best_speaker = None
        best_overlap = 0

        for t in turns:
            overlap_start = max(seg_start, t["start"])
            overlap_end = min(seg_end, t["end"])
            overlap = max(0, overlap_end - overlap_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = seen_speakers[t["speaker"]]

        seg["speaker"] = best_speaker

    return segments


def parse_filename(filename: str) -> tuple[str | None, int | None, str]:
    """Parse date, episode number and title from filename.

    New format: "2024-03-15 - Mediepodden 175 - Var Jon Skolmen egentligen så rolig?.mp3"
    Old format: "Mediepodden 175 - Var Jon Skolmen egentligen så rolig?.mp3"
    Returns: (published_date, episode_number, title)
    """
    name = Path(filename).stem

    # New format: date prefix
    match = re.match(
        r"(\d{4}-\d{2}-\d{2})\s*[-–]\s*Mediepodden\s+(\d+)\s*[-–]\s*(.+)", name
    )
    if match:
        return match.group(1), int(match.group(2)), match.group(3).strip()

    # Old format: no date
    match = re.match(r"Mediepodden\s+(\d+)\s*[-–]\s*(.+)", name)
    if match:
        return None, int(match.group(1)), match.group(2).strip()

    # Fallback: try to find any number
    match = re.match(r".*?(\d+)\s*[-–]\s*(.+)", name)
    if match:
        return None, int(match.group(1)), match.group(2).strip()

    return None, None, name


def get_file_date(path: Path) -> str:
    """Get file creation date (birthtime on macOS, mtime as fallback)."""
    stat = path.stat()
    try:
        ts = stat.st_birthtime
    except AttributeError:
        ts = stat.st_mtime
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")


async def get_episode_status(episode_number: int) -> str | None:
    """Check if an episode exists and return its transcription status."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT transcription_status FROM episodes WHERE episode_number = ?",
            (episode_number,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def cleanup_incomplete_episode(episode_number: int):
    """Remove an incomplete episode (PROCESSING/FAILED) so it can be retried."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM episodes WHERE episode_number = ?", (episode_number,),
        )
        row = await cursor.fetchone()
        if not row:
            return
        episode_id = row[0]
        await db.execute("DELETE FROM segments WHERE episode_id = ?", (episode_id,))
        await db.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
        await db.commit()
        logger.info("Rensade avbruten episod %d (id=%d)", episode_number, episode_id)


def _transcribe_local(mp3_path: Path) -> tuple[list[dict], float, bool]:
    """Transcribe locally with mlx-whisper + pyannote.

    Returns: (segments, duration, diarization_used)
    """
    import mlx_whisper

    logger.info("Transkriberar med mlx-whisper (KB Swedish model)...")
    result = mlx_whisper.transcribe(
        str(mp3_path),
        path_or_hf_repo=MLX_MODEL_PATH,
        language="sv",
        condition_on_previous_text=True,
        verbose=False,
    )

    segments = []
    duration = 0
    for seg in result["segments"]:
        segments.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": seg["text"].strip(),
        })
        duration = max(duration, seg["end"])

    logger.info("Whisper klar: %d segment, %.0fs", len(segments), duration)

    # Talaridentifiering
    diarization_turns = _run_diarization(str(mp3_path))
    if diarization_turns:
        segments = _assign_speakers(segments, diarization_turns)

    return segments, duration, diarization_turns is not None


def _transcribe_cloud(mp3_path: Path) -> tuple[list[dict], float, bool]:
    """Transcribe via svenska-ord (Modal GPU).

    Returns: (segments, duration, diarization_used)
    """
    from svenska_ord import SvenskaOrd

    client = SvenskaOrd()
    logger.info("Transkriberar med svenska-ord (Modal GPU)...")

    result = client.transcribe(str(mp3_path), language="sv")

    segments = result.to_segment_dicts()
    logger.info("Moln-transkribering klar: %d segment, %.0fs", len(segments), result.duration_seconds)

    return segments, result.duration_seconds, False


async def transcribe_file(mp3_path: Path, use_cloud: bool = False):
    """Transcribe a single MP3 file and save to database."""
    parsed_date, episode_number, title = parse_filename(mp3_path.name)

    if episode_number:
        status = await get_episode_status(episode_number)
        if status == TranscriptionStatus.COMPLETED:
            logger.info("Avsnitt %d redan transkriberat, hoppar över: %s", episode_number, mp3_path.name)
            return
        if status is not None:
            # Incomplete (PROCESSING/FAILED) — clean up and retry
            await cleanup_incomplete_episode(episode_number)

    logger.info("=== Transkriberar: %s ===", mp3_path.name)
    published_date = parsed_date or get_file_date(mp3_path)

    # Skapa episod i databasen
    episode_id = await create_episode(
        title=title,
        episode_number=episode_number,
        description=None,
        audio_filename=None,
        published_date=published_date,
    )
    logger.info("Skapade episod #%s (id=%d): %s", episode_number, episode_id, title)

    await update_episode(episode_id, transcription_status=TranscriptionStatus.PROCESSING)

    try:
        # Transkribera (lokalt eller moln)
        if use_cloud:
            segments, duration, diarization_used = _transcribe_cloud(mp3_path)
        else:
            segments, duration, diarization_used = _transcribe_local(mp3_path)

        # Spara segment
        await save_segments(episode_id, segments)

        # Bestäm audio_filename
        nr = episode_number or episode_id
        audio_filename = f"{published_date}-mediepodden-{nr}.mp3"

        # Uppdatera episod
        await update_episode(
            episode_id,
            transcription_status=TranscriptionStatus.COMPLETED,
            transcription_progress=100,
            duration_seconds=duration,
            audio_filename=audio_filename,
        )
        invalidate_stats_cache()

        # Flytta original till OUT/
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(mp3_path), OUT_DIR / mp3_path.name)
        logger.info("Flyttade %s till OUT/", mp3_path.name)

        logger.info(
            "Klart! Avsnitt %s: %d segment, %.0fs, talaridentifiering=%s",
            episode_number, len(segments), duration,
            "ja" if diarization_used else "nej",
        )

    except Exception:
        logger.exception("Transkribering misslyckades för %s", mp3_path.name)
        await update_episode(episode_id, transcription_status=TranscriptionStatus.FAILED)
        raise


async def main():
    parser = argparse.ArgumentParser(description="Transkribera Mediepodden-avsnitt")
    parser.add_argument("--cloud", action="store_true",
                        help="Använd svenska-ord (Modal GPU) istället för lokal mlx-whisper")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.cloud:
        logger.info("Läge: svenska-ord (Modal GPU)")
    else:
        logger.info("Läge: lokalt (mlx-whisper)")

    await init_db()

    IN_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    mp3_files = sorted(IN_DIR.glob("*.mp3"))
    if not mp3_files:
        logger.info("Inga MP3-filer i IN/ — inget att göra.")
        return

    logger.info("Hittade %d MP3-fil(er) i IN/", len(mp3_files))

    for mp3_path in mp3_files:
        await transcribe_file(mp3_path, use_cloud=args.cloud)

    logger.info("=== Alla filer transkriberade! ===")


if __name__ == "__main__":
    asyncio.run(main())
