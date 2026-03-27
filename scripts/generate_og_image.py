"""Generate the static OG image for the start page."""

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630

# Warm sepia theme (matches site CSS)
BG = (240, 234, 231)           # #f0eae7
FG = (46, 36, 32)              # #2e2420
ACCENT = (160, 120, 48)        # #a07830
ACCENT_LIGHT = (184, 145, 46)  # #b8912e
MUTED = (124, 115, 108)        # #7c736c
CARD_BG = (248, 244, 241)      # #f8f4f1
BORDER = (213, 204, 199)       # #d5ccc7
BAR_DIM = (200, 190, 182)      # muted bar color


def _load_fonts():
    sizes = {"brand": 56, "sub": 26, "search": 20, "url": 18, "tag": 16}
    for path_pattern in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans{}.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]:
        try:
            bold = path_pattern.format("-Bold") if "{}" in path_pattern else path_pattern
            regular = path_pattern.format("") if "{}" in path_pattern else path_pattern
            return {
                "brand": ImageFont.truetype(bold, sizes["brand"]),
                "sub": ImageFont.truetype(regular, sizes["sub"]),
                "search": ImageFont.truetype(regular, sizes["search"]),
                "url": ImageFont.truetype(regular, sizes["url"]),
                "tag": ImageFont.truetype(regular, sizes["tag"]),
            }
        except OSError:
            continue
    default = ImageFont.load_default()
    return {k: default for k in sizes}


def _draw_waveform(draw, y_center, x_start, x_end, height, num_bars):
    """Draw a decorative waveform."""
    random.seed(42)
    bar_total_w = (x_end - x_start) / num_bars
    bar_w = max(3, int(bar_total_w * 0.6))

    for i in range(num_bars):
        t = i / num_bars
        envelope = math.sin(t * math.pi) * 0.7 + 0.3
        peak = random.uniform(0.15, 1.0) * envelope
        bar_h = max(4, int(peak * height))
        x = x_start + i * bar_total_w
        y_top = y_center - bar_h // 2

        # Interpolate from muted to accent based on peak
        color = (
            int(BAR_DIM[0] + peak * (ACCENT[0] - BAR_DIM[0])),
            int(BAR_DIM[1] + peak * (ACCENT[1] - BAR_DIM[1])),
            int(BAR_DIM[2] + peak * (ACCENT[2] - BAR_DIM[2])),
        )
        draw.rounded_rectangle(
            [x, y_top, x + bar_w, y_top + bar_h],
            radius=2,
            fill=color,
        )


def _draw_search_box(draw, fonts, x, y, width, height):
    """Draw a search box with placeholder text."""
    draw.rounded_rectangle(
        [x, y, x + width, y + height],
        radius=8,
        fill=CARD_BG,
        outline=BORDER,
        width=2,
    )

    # Magnifying glass icon
    glass_x = x + 20
    glass_cy = y + height // 2
    glass_r = 10
    draw.ellipse(
        [glass_x - glass_r, glass_cy - glass_r, glass_x + glass_r, glass_cy + glass_r],
        outline=MUTED,
        width=2,
    )
    draw.line(
        [glass_x + 7, glass_cy + 7, glass_x + 14, glass_cy + 14],
        fill=MUTED,
        width=2,
    )

    draw.text(
        (glass_x + 24, glass_cy - 10),
        "Sök efter innehåll, ämnen, namn...",
        fill=MUTED,
        font=fonts["search"],
    )

    # "Sök" button
    btn_w = 70
    btn_h = height - 12
    btn_x = x + width - btn_w - 6
    btn_y = y + 6
    draw.rounded_rectangle(
        [btn_x, btn_y, btn_x + btn_w, btn_y + btn_h],
        radius=6,
        fill=ACCENT,
    )
    bbox = fonts["search"].getbbox("Sök")
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        (btn_x + (btn_w - tw) // 2, btn_y + (btn_h - th) // 2 - 2),
        "Sök",
        fill=BG,
        font=fonts["search"],
    )


def _draw_tags(draw, fonts, tags, x_start, y, max_width):
    """Draw a row of topic tags."""
    tag_x = x_start
    tag_h = 30
    pad_x = 12
    gap = 8

    for tag in tags:
        bbox = fonts["tag"].getbbox(tag)
        tw = bbox[2] - bbox[0]
        tag_w = tw + pad_x * 2

        if tag_x + tag_w > x_start + max_width:
            break

        draw.rounded_rectangle(
            [tag_x, y, tag_x + tag_w, y + tag_h],
            radius=tag_h // 2,
            fill=None,
            outline=BORDER,
            width=1,
        )
        draw.text(
            (tag_x + pad_x, y + (tag_h - (bbox[3] - bbox[1])) // 2 - 1),
            tag,
            fill=FG,
            font=fonts["tag"],
        )
        tag_x += tag_w + gap


def generate():
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    fonts = _load_fonts()

    # Background waveform (subtle, right side)
    _draw_waveform(draw, H // 2 + 40, W // 2 + 40, W - 40, 300, 60)

    # Semi-transparent overlay to push waveform back
    overlay = Image.new("RGBA", (W, H), (*BG, 140))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # "Mediepodden sök"
    draw.text((80, 120), "Mediepodden sök", fill=FG, font=fonts["brand"])

    # Subtitle
    draw.text(
        (80, 195),
        "Sök i alla avsnitt av podden Mediepodden",
        fill=MUTED,
        font=fonts["sub"],
    )

    # Search box
    _draw_search_box(draw, fonts, 80, 260, 540, 50)

    # Topic tags
    tags = [
        "Elon Musk", "Mark Zuckerberg", "Donald Trump",
        "Schibsted", "Netflix", "SVT", "TikTok", "AI-boomen",
    ]
    _draw_tags(draw, fonts, tags, 80, 340, 540)

    # Foreground waveform (right side, more vibrant)
    _draw_waveform(draw, H // 2 + 20, W // 2 + 100, W - 60, 200, 50)

    # URL bottom left
    draw.text((80, H - 60), "mediepodden.gunnar.se", fill=MUTED, font=fonts["url"])

    # "335 avsnitt" bottom right
    draw.text((W - 240, H - 60), "335 avsnitt indexerade", fill=MUTED, font=fonts["url"])

    out = Path(__file__).parent.parent / "app" / "static" / "og-image.png"
    img.save(out, format="PNG", optimize=True)
    print(f"Saved {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    generate()
