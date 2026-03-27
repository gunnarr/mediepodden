"""Entity extraction service.

Extracts named entities from transcribed episodes using Claude Haiku,
stores raw results in entities_raw.jsonl and rebuilds the aggregated
entities.json used by the search/statistics pages.
"""

import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.config import ANTHROPIC_API_KEY

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
RAW_PATH = DATA_DIR / "entities_raw.jsonl"
OUTPUT_PATH = DATA_DIR / "entities.json"

CATEGORIES = {
    "personer": "Personer",
    "företag": "Företag & mediebolag",
    "plattformar": "Plattformar & tjänster",
    "medier": "Medier & redaktioner",
    "tv": "TV & film",
    "händelser": "Händelser & fenomen",
    "övrigt": "Övrigt",
}

MIN_COUNT = 2  # minimum episode appearances to include

EXTRACTION_PROMPT = """\
Du är en entitetsextraktor. Analysera följande transkription från ett avsnitt av \
den svenska podden Mediepodden. Podden handlar om den komplexa digitala förändring \
som hela samhället står inför — förändringen i traditionella, digitala och sociala \
medier. Den drivs av Emanuel Karlsten och Olle Lidbom.

Extrahera alla namngivna entiteter som nämns.

Returnera ENBART giltig JSON (ingen markdown, inga kommentarer) med följande struktur:
{
  "personer": ["Elon Musk", "Anna Kinberg Batra"],
  "företag": ["Schibsted", "Bonnier", "Google", "Meta"],
  "plattformar": ["Twitter", "TikTok", "Spotify", "YouTube"],
  "medier": ["Dagens Nyheter", "SVT", "TV4", "Aftonbladet", "P3"],
  "tv": ["Agenda", "Uppdrag granskning", "Succession"],
  "händelser": ["Metoo", "Capitol-stormningen", "AI-boomen"],
  "övrigt": ["GDPR", "presstödet", "public service-utredningen"]
}

Regler:
- Exkludera värdarna Emanuel Karlsten och Olle Lidbom, samt podden Mediepodden
- Använd fullständiga namn (t.ex. "Mark Zuckerberg", inte bara "Zuckerberg")
- "personer" — journalister, medieprofiler, politiker, tech-profiler osv.
- "företag" — mediekoncerner, techbolag, förlag, produktionsbolag
- "plattformar" — sociala medier, streamingtjänster, digitala plattformar
- "medier" — tidningar, TV-kanaler, radiokanaler, poddar, nyhetssajter
- "tv" — specifika TV-program, filmer, serier, dokumentärer
- "händelser" — nyheter, fenomen, rörelser, debatter
- "övrigt" — lagar, utredningar, organisationer, begrepp som inte passar ovan
- Varje entitet ska bara finnas i EN kategori
- Om inget hittas i en kategori, returnera en tom lista
- Svara BARA med JSON, ingen annan text

Transkription:
"""

# Entities to exclude (hosts, the podcast itself)
BLOCKLIST = {
    "mediepodden",
    "emanuel karlsten",
    "olle lidbom",
}

# Spelling variants to merge: {alternative → canonical}
SPELLING_MERGE = {
    "x": "twitter",
    "facebook": "meta",
}

# Canonical display names (forced after merge)
CANONICAL_NAMES = {
    "twitter": "Twitter/X",
    "meta": "Meta",
}


def extract_entities_from_response(response_text: str) -> dict[str, list[str]]:
    """Parse Claude response to entity dict."""
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Invalid JSON from entity extraction: %s", text[:200])
        return {cat: [] for cat in CATEGORIES}

    result = {}
    for cat in CATEGORIES:
        entities = data.get(cat, [])
        if isinstance(entities, list):
            result[cat] = [str(e).strip() for e in entities if str(e).strip()]
        else:
            result[cat] = []
    return result


def normalize_name(name: str) -> str:
    """Normalize a name for deduplication."""
    return name.strip().lower()


def merge_entities(raw_data: list[dict]) -> dict:
    """Aggregate entities from all episodes.

    Counts in how many episodes each entity is mentioned.
    Normalizes names (case-insensitive dedup, merge short to long variants).
    """
    per_category: dict[str, dict[str, dict]] = {cat: {} for cat in CATEGORIES}

    for ep_data in raw_data:
        ep_num = ep_data["episode_number"]
        entities = ep_data["entities"]

        for cat in CATEGORIES:
            for name in entities.get(cat, []):
                norm = normalize_name(name)
                if norm in BLOCKLIST:
                    continue
                norm = SPELLING_MERGE.get(norm, norm)
                if norm not in per_category[cat]:
                    per_category[cat][norm] = {
                        "canonical": name,
                        "episodes": set(),
                    }
                else:
                    existing = per_category[cat][norm]
                    if len(name) > len(existing["canonical"]):
                        existing["canonical"] = name
                per_category[cat][norm]["episodes"].add(ep_num)

    # Merge last-name-only → full name (only for personer)
    entries = per_category.get("personer", {})
    norms = sorted(entries.keys(), key=len)

    to_merge = []
    for i, short_norm in enumerate(norms):
        if " " in short_norm:
            continue
        for long_norm in norms[i + 1:]:
            if long_norm.endswith(" " + short_norm):
                to_merge.append((short_norm, long_norm))
                break

    for short_norm, long_norm in to_merge:
        if short_norm in entries and long_norm in entries:
            entries[long_norm]["episodes"] |= entries[short_norm]["episodes"]
            del entries[short_norm]

    # Force canonical names
    for cat in CATEGORIES:
        for norm, entry in per_category[cat].items():
            if norm in CANONICAL_NAMES:
                entry["canonical"] = CANONICAL_NAMES[norm]

    # Cross-category dedup: keep entity in its primary category
    all_norms: dict[str, list[tuple[str, int]]] = {}
    for cat in CATEGORIES:
        for norm, entry in per_category[cat].items():
            all_norms.setdefault(norm, []).append((cat, len(entry["episodes"])))

    for norm, cats in all_norms.items():
        if len(cats) > 1:
            best_cat = max(cats, key=lambda x: x[1])[0]
            for cat, _ in cats:
                if cat != best_cat:
                    del per_category[cat][norm]

    return per_category


def build_output(per_category: dict, episode_count: int) -> dict:
    """Build output JSON with all entities mentioned in at least MIN_COUNT episodes."""
    total_entities = 0
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episode_count": episode_count,
        "categories": {},
    }

    for cat, label in CATEGORIES.items():
        entries = per_category[cat]
        sorted_entries = sorted(
            entries.values(),
            key=lambda e: (-len(e["episodes"]), e["canonical"]),
        )
        sorted_entries = [e for e in sorted_entries if len(e["episodes"]) >= MIN_COUNT]
        total_entities += len(sorted_entries)

        output["categories"][cat] = {
            "label": label,
            "entities": [
                {
                    "name": e["canonical"],
                    "count": len(e["episodes"]),
                    "search_term": e["canonical"],
                }
                for e in sorted_entries
            ],
        }

    output["total_entities"] = total_entities
    return output


def load_raw_data() -> list[dict]:
    """Load all raw entity data from entities_raw.jsonl, deduplicating by episode."""
    all_raw = []
    if RAW_PATH.exists():
        seen_episodes = set()
        for line in RAW_PATH.read_text().splitlines():
            if line.strip():
                try:
                    data = json.loads(line)
                    ep_num = data["episode_number"]
                    if ep_num in seen_episodes:
                        all_raw = [r for r in all_raw if r["episode_number"] != ep_num]
                    seen_episodes.add(ep_num)
                    all_raw.append(data)
                except (json.JSONDecodeError, KeyError):
                    continue
    return all_raw


def load_processed_episodes() -> set[int]:
    """Load already-processed episode numbers from the raw file."""
    processed = set()
    if RAW_PATH.exists():
        for line in RAW_PATH.read_text().splitlines():
            if line.strip():
                try:
                    data = json.loads(line)
                    processed.add(data["episode_number"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return processed


def append_raw_entry(entry: dict):
    """Append a raw entity entry to entities_raw.jsonl."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(RAW_PATH, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def rebuild_entities_json():
    """Rebuild entities.json from raw data and invalidate cache."""
    all_raw = load_raw_data()
    if not all_raw:
        logger.warning("No entity data to aggregate")
        return

    per_category = merge_entities(all_raw)
    output = build_output(per_category, len(all_raw))

    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    logger.info("Rebuilt %s (%d entities)", OUTPUT_PATH, output["total_entities"])

    invalidate_entities_cache()


def invalidate_entities_cache():
    """Reset the entities cache in the search router."""
    try:
        from app.routers.search import invalidate_entities_cache as _invalidate
        _invalidate()
    except ImportError:
        pass


async def extract_and_save(episode_id: int, episode_number: int | None, title: str):
    """Extract entities for a single episode and rebuild entities.json.

    1. Fetches segment text from DB
    2. Calls Claude Haiku via anthropic.Anthropic (sync, run in asyncio.to_thread)
    3. Appends result to entities_raw.jsonl
    4. Rebuilds entities.json
    5. Invalidates _entities_cache
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set, skipping entity extraction")
        return

    from app.database import get_db

    # Fetch segment text
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT text FROM segments WHERE episode_id = ? ORDER BY start_time",
            (episode_id,),
        )
        segments = await cursor.fetchall()

    if not segments:
        logger.warning("No segments found for episode %d, skipping entities", episode_id)
        return

    full_text = " ".join(row["text"] for row in segments)
    if len(full_text) > 100_000:
        full_text = full_text[:100_000]

    # Call Claude Haiku (sync client, offloaded to thread)
    def _call_api():
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16384,
            messages=[
                {"role": "user", "content": EXTRACTION_PROMPT + full_text}
            ],
        )
        return message.content[0].text

    response_text = await asyncio.to_thread(_call_api)
    entities = extract_entities_from_response(response_text)

    total = sum(len(v) for v in entities.values())
    logger.info("Extracted %d entities for episode %s (%s)", total, episode_number, title)

    raw_entry = {
        "episode_number": episode_number,
        "title": title,
        "entities": entities,
    }
    append_raw_entry(raw_entry)
    rebuild_entities_json()
