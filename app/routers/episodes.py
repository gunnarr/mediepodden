from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.config import PODCAST_URL, TranscriptionStatus
from app.database import get_episode_by_slug, get_episode_segments, get_clip_context_segments, list_all_episodes
from app.filters import format_timestamp
from app.templating import templates, context

router = APIRouter(prefix="/avsnitt", tags=["episodes"])


@router.get("", response_class=HTMLResponse)
async def episode_list(request: Request):
    episodes = await list_all_episodes()
    return templates.TemplateResponse(
        "episodes.html",
        await context(
            request,
            episodes=episodes,
            podcast_url=PODCAST_URL,
        ),
    )


@router.get("/{slug}/t/{start_time:int}", response_class=HTMLResponse)
async def clip_page(request: Request, slug: str, start_time: int):
    episode = await get_episode_by_slug(slug)
    if not episode:
        raise HTTPException(status_code=404, detail="Avsnitt hittades inte")

    if episode["transcription_status"] != TranscriptionStatus.COMPLETED:
        raise HTTPException(status_code=404, detail="Transkription saknas")

    segments = await get_episode_segments(episode["id"])

    # Find the segment whose start_time (rounded to int) matches
    main_segment = None
    for seg in segments:
        if int(seg["start_time"]) == start_time:
            main_segment = seg
            break

    if main_segment is None:
        raise HTTPException(status_code=404, detail="Segment hittades inte")

    # Get all segments within the audio clip window (±10s)
    clip_padding = 10
    clip_start = max(main_segment["start_time"] - clip_padding, 0)
    clip_end = main_segment["end_time"] + clip_padding
    context_segments = await get_clip_context_segments(
        episode["id"], clip_start, clip_end
    )

    clip_url = f"/klipp/{episode['id']}/{clip_start}-{clip_end}.mp3"

    return templates.TemplateResponse(
        "clip.html",
        await context(
            request,
            episode=episode,
            segment=main_segment,
            context_segments=context_segments,
            clip_url=clip_url,
            podcast_url=PODCAST_URL,
        ),
    )


def _format_srt_time(seconds: float) -> str:
    """Format seconds as HH:MM:SS,mmm for SRT."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@router.get("/{slug}/transkription.srt")
async def export_srt(slug: str):
    """Export transcription as SRT subtitle file."""
    episode = await get_episode_by_slug(slug)
    if not episode or episode["transcription_status"] != TranscriptionStatus.COMPLETED:
        raise HTTPException(status_code=404, detail="Avsnitt hittades inte")

    segments = await get_episode_segments(episode["id"])
    lines = []
    for i, seg in enumerate(segments, 1):
        start = _format_srt_time(seg["start_time"])
        end = _format_srt_time(seg["end_time"])
        speaker_prefix = ""
        if seg.get("speaker"):
            if seg["speaker"] == "SPEAKER_0":
                speaker_prefix = f"[{episode['speaker_label_0']}] "
            elif seg["speaker"] == "SPEAKER_1":
                speaker_prefix = f"[{episode['speaker_label_1']}] "
        lines.append(f"{i}\n{start} --> {end}\n{speaker_prefix}{seg['text']}\n")

    content = "\n".join(lines)
    filename = f"{slug}.srt"
    return PlainTextResponse(
        content,
        media_type="text/srt; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{slug}/transkription.txt")
async def export_txt(slug: str):
    """Export transcription as plain text."""
    episode = await get_episode_by_slug(slug)
    if not episode or episode["transcription_status"] != TranscriptionStatus.COMPLETED:
        raise HTTPException(status_code=404, detail="Avsnitt hittades inte")

    segments = await get_episode_segments(episode["id"])
    lines = []
    title = episode["title"]
    if episode.get("episode_number"):
        title = f"Avsnitt {episode['episode_number']} — {title}"
    lines.append(title)
    lines.append("=" * len(title))
    lines.append("")

    for seg in segments:
        ts = format_timestamp(seg["start_time"])
        speaker = ""
        if seg.get("speaker"):
            if seg["speaker"] == "SPEAKER_0":
                speaker = f" [{episode['speaker_label_0']}]"
            elif seg["speaker"] == "SPEAKER_1":
                speaker = f" [{episode['speaker_label_1']}]"
        lines.append(f"[{ts}]{speaker} {seg['text']}")

    content = "\n".join(lines) + "\n"
    filename = f"{slug}.txt"
    return PlainTextResponse(
        content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
