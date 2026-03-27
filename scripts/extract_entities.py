#!/usr/bin/env python3
"""
Extrahera namngivna entiteter ur alla Mediepodden-avsnitt med Claude API.

Läser transkriberade avsnitt från databasen, skickar varje episods fulltext
till Claude Haiku, och aggregerar de populäraste entiteterna till
data/entities.json för visning på startsidan.

Kör:
    python scripts/extract_entities.py                # alla avsnitt
    python scripts/extract_entities.py --episode 50   # enstaka avsnitt (debug)
    python scripts/extract_entities.py --resume       # hoppa över redan processade
    python scripts/extract_entities.py --dry-run      # visa vad som skulle göras
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from app.database import get_db, init_db
from app.services.entities import (
    CATEGORIES,
    EXTRACTION_PROMPT,
    RAW_PATH,
    OUTPUT_PATH,
    extract_entities_from_response,
    load_processed_episodes,
    load_raw_data,
    append_raw_entry,
    merge_entities,
    build_output,
    rebuild_entities_json,
)

logger = logging.getLogger(__name__)


async def get_completed_episodes() -> list[dict]:
    """Hämta alla completed-episoder med deras segmenttext."""
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT e.id, e.episode_number, e.title
               FROM episodes e
               WHERE e.transcription_status = 'completed'
               ORDER BY e.episode_number"""
        )
        episodes = [dict(row) for row in await cursor.fetchall()]

        for ep in episodes:
            seg_cursor = await db.execute(
                "SELECT text FROM segments WHERE episode_id = ? ORDER BY start_time",
                (ep["id"],),
            )
            segments = await seg_cursor.fetchall()
            ep["full_text"] = " ".join(row["text"] for row in segments)

    return episodes


async def process_episode(client, episode: dict) -> dict[str, list[str]] | None:
    """Skicka en episods text till Claude och extrahera entiteter."""
    text = episode["full_text"]

    # Trunkera om nödvändigt (Haiku klarar ~200k tokens men vi begränsar till säkerhet)
    if len(text) > 100_000:
        text = text[:100_000]

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16384,
            messages=[
                {"role": "user", "content": EXTRACTION_PROMPT + text}
            ],
        )
        response_text = message.content[0].text
        return extract_entities_from_response(response_text)
    except json.JSONDecodeError:
        logger.error(
            "Kunde inte parsa JSON för avsnitt %s: %s",
            episode["episode_number"],
            response_text[:200] if 'response_text' in dir() else "?",
        )
        return None
    except Exception as e:
        # Avbryt vid credit-fel istället för att loopa vidare
        if "credit balance" in str(e).lower():
            logger.error("Credits slut! Avbryter. Fyll på och kör --resume.")
            raise SystemExit(1)
        logger.exception("API-fel för avsnitt %s", episode["episode_number"])
        return None


async def main():
    parser = argparse.ArgumentParser(
        description="Extrahera entiteter ur Mediepodden-avsnitt med Claude API"
    )
    parser.add_argument("--episode", type=int, help="Processera bara ett avsnitt (debug)")
    parser.add_argument("--resume", action="store_true", help="Hoppa över redan processade")
    parser.add_argument("--dry-run", action="store_true", help="Visa vad som skulle göras")
    parser.add_argument("--rebuild", action="store_true", help="Bara aggregera om, inga API-anrop")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    api_key = os.getenv("ANTHROPIC_API_KEY")

    await init_db()

    # Hämta episoder
    episodes = await get_completed_episodes()
    logger.info("Hittade %d transkriberade avsnitt", len(episodes))

    if args.episode:
        episodes = [ep for ep in episodes if ep["episode_number"] == args.episode]
        if not episodes:
            logger.error("Avsnitt %d hittades inte eller är inte transkriberat", args.episode)
            sys.exit(1)

    # Resume: filtrera bort redan processade
    processed = set()
    if args.resume:
        processed = load_processed_episodes()
        before = len(episodes)
        episodes = [ep for ep in episodes if ep["episode_number"] not in processed]
        logger.info("Resume: hoppar över %d redan processade, %d kvar", before - len(episodes), len(episodes))

    if args.rebuild:
        logger.info("Rebuild — hoppar över API-anrop, aggregerar bara om.")
        episodes = []  # skip processing

    if args.dry_run:
        logger.info("Dry run — skulle processera %d avsnitt:", len(episodes))
        for ep in episodes:
            logger.info("  Avsnitt %s: %s (%d tecken)",
                        ep["episode_number"], ep["title"], len(ep["full_text"]))
        total_chars = sum(len(ep["full_text"]) for ep in episodes)
        logger.info("Totalt ~%dk tecken (~%.1fM tokens)", total_chars // 1000, total_chars / 4 / 1_000_000)
        return

    if not episodes:
        logger.info("Inga nya avsnitt att processera.")
    else:
        if not api_key:
            logger.error("ANTHROPIC_API_KEY saknas i .env")
            sys.exit(1)
        # Importera anthropic
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)

        # Processera episoder
        for i, ep in enumerate(episodes, 1):
            ep_num = ep["episode_number"]
            logger.info("[%d/%d] Avsnitt %s: %s (%d tecken)",
                        i, len(episodes), ep_num, ep["title"], len(ep["full_text"]))

            entities = await process_episode(client, ep)
            if entities is None:
                logger.warning("Misslyckades med avsnitt %s, hoppar över", ep_num)
                continue

            total = sum(len(v) for v in entities.values())
            logger.info("  Extraherade %d entiteter", total)

            raw_entry = {
                "episode_number": ep_num,
                "title": ep["title"],
                "entities": entities,
            }
            append_raw_entry(raw_entry)

            # Rate limiting — var försiktig med API:t
            if i < len(episodes):
                time.sleep(2)

    # Aggregera alla raw-data (inklusive tidigare körningar)
    logger.info("Aggregerar entiteter...")
    rebuild_entities_json()

    # Sammanfattning
    if OUTPUT_PATH.exists():
        output = json.loads(OUTPUT_PATH.read_text())
        for cat, label in CATEGORIES.items():
            entities = output["categories"][cat]["entities"]
            if entities:
                top3 = ", ".join(f"{e['name']} ({e['count']})" for e in entities[:3])
                logger.info("  %s: %d st — %s ...", label, len(entities), top3)


if __name__ == "__main__":
    asyncio.run(main())
