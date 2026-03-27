"""Audio clip endpoints for search result playback."""

import hashlib
import math
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.config import AUDIO_DIR, TranscriptionStatus
from app.database import get_episode
from app.rate_limit import limiter
from app.services.audio import get_or_create_clip, generate_waveform_data

router = APIRouter(prefix="/klipp", tags=["clips"])


def _validate_times(start: float, end: float):
    """Validate clip time parameters."""
    if math.isnan(start) or math.isnan(end) or math.isinf(start) or math.isinf(end):
        raise HTTPException(status_code=400, detail="Ogiltigt tidsintervall")
    if start < 0 or end < 0 or start > 36000 or end > 36000:
        raise HTTPException(status_code=400, detail="Ogiltigt tidsintervall")
    if end <= start or end - start > 60:
        raise HTTPException(status_code=400, detail="Ogiltigt tidsintervall")


@router.get("/{episode_id}/{start}-{end}.mp3")
@limiter.limit("10/minute")
async def audio_clip(request: Request, episode_id: int, start: float, end: float):
    """Serve a short MP3 clip of an episode segment."""
    _validate_times(start, end)

    episode = await get_episode(episode_id)
    if not episode or episode["transcription_status"] != TranscriptionStatus.COMPLETED:
        raise HTTPException(status_code=404, detail="Avsnitt hittades inte")

    clip_path = await get_or_create_clip(dict(episode), episode_id, start, end)
    if not clip_path:
        raise HTTPException(status_code=404, detail="Ingen ljudfil tillgänglig")

    return FileResponse(
        clip_path,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/{episode_id}/{start}-{end}/waveform")
@limiter.limit("10/minute")
async def waveform_data(request: Request, episode_id: int, start: float, end: float):
    """Return waveform peak data as JSON for wavesurfer.js rendering."""
    _validate_times(start, end)

    episode = await get_episode(episode_id)
    if not episode or episode["transcription_status"] != TranscriptionStatus.COMPLETED:
        raise HTTPException(status_code=404, detail="Avsnitt hittades inte")

    peaks = await generate_waveform_data(dict(episode), episode_id, start, end)
    if peaks is None:
        raise HTTPException(status_code=500, detail="Kunde inte generera vågform")

    return JSONResponse(
        {"peaks": peaks},
        headers={"Cache-Control": "public, max-age=86400"},
    )


OG_DIR = AUDIO_DIR / "og"


def _og_cache_path(episode_id: int, start: float, end: float) -> Path:
    key = f"og:{episode_id}:{start:.1f}:{end:.1f}"
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return OG_DIR / f"{episode_id}_{h}.png"


def _render_og_image(
    peaks: list[float],
    title: str,
    timestamp: str,
    episode_label: str,
) -> bytes:
    """Render an OG image with waveform and play button."""
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1200, 630
    BG = (12, 12, 16)
    BAR_COLOR = (78, 234, 170)
    BAR_DIM = (30, 80, 60)
    PLAY_COLOR = (255, 255, 255)
    TEXT_COLOR = (224, 224, 230)
    MUTED_COLOR = (138, 138, 150)

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Fonts — try system fonts, fall back to default
    try:
        font_brand_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        font_meta = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
        font_url = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except OSError:
        try:
            font_brand_lg = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 48)
            font_title = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 28)
            font_meta = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 22)
            font_url = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 18)
        except OSError:
            font_brand_lg = ImageFont.load_default()
            font_title = font_brand_lg
            font_meta = font_brand_lg
            font_url = font_brand_lg

    # "Mediepodden sök" — prominent brand
    draw.text((80, 50), "Mediepodden sök", fill=BAR_COLOR, font=font_brand_lg)

    # Episode title (truncate if too long)
    if len(title) > 60:
        title = title[:57] + "..."
    draw.text((80, 115), title, fill=TEXT_COLOR, font=font_title)

    # Episode label + timestamp
    meta_text = f"{episode_label}  ·  {timestamp}"
    draw.text((80, 155), meta_text, fill=MUTED_COLOR, font=font_meta)

    # Waveform bars
    wave_y = 220
    wave_h = 220
    wave_x_start = 120
    wave_x_end = W - 120
    wave_w = wave_x_end - wave_x_start
    num_bars = min(len(peaks), 80)
    bar_total_w = wave_w / num_bars
    bar_w = max(2, int(bar_total_w * 0.7))
    bar_gap = bar_total_w - bar_w

    for i in range(num_bars):
        idx = int(i * len(peaks) / num_bars)
        peak = peaks[idx] if idx < len(peaks) else 0
        bar_h = max(4, int(peak * wave_h))
        x = wave_x_start + i * bar_total_w
        y_top = wave_y + (wave_h - bar_h) // 2
        # Color: brighter for taller bars
        t = peak
        color = (
            int(BAR_DIM[0] + t * (BAR_COLOR[0] - BAR_DIM[0])),
            int(BAR_DIM[1] + t * (BAR_COLOR[1] - BAR_DIM[1])),
            int(BAR_DIM[2] + t * (BAR_COLOR[2] - BAR_DIM[2])),
        )
        draw.rounded_rectangle(
            [x, y_top, x + bar_w, y_top + bar_h],
            radius=2,
            fill=color,
        )

    # Play button circle
    cx, cy = W // 2, wave_y + wave_h // 2
    r = 44
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=None, outline=PLAY_COLOR, width=3)
    # Play triangle
    tri_size = 20
    tri_offset = 5
    draw.polygon(
        [
            (cx - tri_size // 2 + tri_offset, cy - tri_size),
            (cx - tri_size // 2 + tri_offset, cy + tri_size),
            (cx + tri_size + tri_offset, cy),
        ],
        fill=PLAY_COLOR,
    )

    # URL
    draw.text((80, H - 60), "mediepodden.gunnar.se", fill=MUTED_COLOR, font=font_url)

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@router.get("/{episode_id}/{start}-{end}/og.png")
@limiter.limit("10/minute")
async def og_image(request: Request, episode_id: int, start: float, end: float):
    """Generate an Open Graph image with waveform visualization."""
    _validate_times(start, end)

    episode = await get_episode(episode_id)
    if not episode or episode["transcription_status"] != TranscriptionStatus.COMPLETED:
        raise HTTPException(status_code=404, detail="Avsnitt hittades inte")

    cache_path = _og_cache_path(episode_id, start, end)
    if cache_path.exists():
        return FileResponse(
            cache_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    peaks = await generate_waveform_data(dict(episode), episode_id, start, end)
    if peaks is None:
        raise HTTPException(status_code=404, detail="Ingen ljudfil tillgänglig")

    # Format metadata
    ep_num = episode.get("episode_number")
    episode_label = f"Avsnitt {ep_num}" if ep_num else ""
    total_sec = int(start)
    h, m, s = total_sec // 3600, (total_sec % 3600) // 60, total_sec % 60
    timestamp = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    png_data = _render_og_image(
        peaks=peaks,
        title=episode["title"],
        timestamp=timestamp,
        episode_label=episode_label,
    )

    OG_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(png_data)

    return Response(
        content=png_data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )
