"""SEO routes: robots.txt, sitemap.xml."""

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from app.config import SITE_DOMAIN
from app.database import list_all_episodes

router = APIRouter(tags=["seo"])


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /sok\n"
        "Disallow: /klipp/\n"
        "\n"
        f"Sitemap: https://{SITE_DOMAIN}/sitemap.xml\n"
    )


@router.get("/sitemap.xml")
async def sitemap_xml(request: Request):
    base = f"https://{SITE_DOMAIN}"
    episodes = await list_all_episodes()
    completed = [e for e in episodes if e.get("transcription_status") == "completed"]

    urls = []

    # Static pages
    static = [
        ("/", "1.0", "daily"),
        ("/avsnitt", "0.9", "weekly"),
        ("/statistik", "0.5", "daily"),
        ("/om", "0.8", "monthly"),
    ]
    for path, priority, freq in static:
        urls.append(
            f"  <url>\n"
            f"    <loc>{base}{path}</loc>\n"
            f"    <priority>{priority}</priority>\n"
            f"    <changefreq>{freq}</changefreq>\n"
            f"  </url>"
        )

    # Episode pages
    for ep in completed:
        slug = ep.get("slug", "")
        lastmod = (ep.get("published_date") or "")[:10]
        entry = f"  <url>\n    <loc>{base}/avsnitt/{slug}</loc>\n"
        if lastmod:
            entry += f"    <lastmod>{lastmod}</lastmod>\n"
        entry += f"    <priority>0.6</priority>\n  </url>"
        urls.append(entry)

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>"
    )
    return Response(content=xml, media_type="application/xml")
