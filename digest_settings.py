"""Environment-backed settings shared by the generator and the renderer.

Personal values stay in the environment. Defaults are public placeholders.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import urllib.parse


DEFAULT_SECTION_ORDER = (
    "weather",
    "global",
    "local",
    "events",
    "markets",
    "uk",
    "film",
    "bot_updates",
)

OPTIONAL_SECTION_IDS = frozenset({"bot_updates"})

DEFAULT_TITLES = {
    "weather": "WEATHER",
    "global": "GLOBAL",
    "local": "LOCAL",
    "events": "EVENTS TODAY / SOON",
    "markets": "SPACEX/SPACE STOCK",
    "uk": "UK",
    "film": "FILM/TV",
    "bot_updates": "BOT UPDATES",
}

TITLE_ENV = {
    "weather": "DIGEST_SECTION_TITLE_WEATHER",
    "global": "DIGEST_SECTION_TITLE_GLOBAL",
    "local": "DIGEST_SECTION_TITLE_LOCAL",
    "events": "DIGEST_SECTION_TITLE_EVENTS",
    "markets": "DIGEST_SECTION_TITLE_MARKETS",
    "uk": "DIGEST_SECTION_TITLE_UK",
    "film": "DIGEST_SECTION_TITLE_FILM",
    "bot_updates": "DIGEST_SECTION_TITLE_BOT_UPDATES",
}

SECTION_KINDS = {
    "weather": "weather",
    "global": "news",
    "local": "news",
    "events": "events",
    "markets": "news",
    "uk": "news",
    "film": "news",
    "bot_updates": "bot_updates",
}

PURPOSES = {
    "weather": "Daytime forecast for the configured place.",
    "global": "World news that will still matter by tonight. Prefer reported events over columns.",
    "local": "Local news that affects daily life in the configured area.",
    "events": "Verified events from the configured calendars.",
    "markets": "News about the configured market interests. Skip promotional stock tips and buy alerts.",
    "uk": "UK news with a clear event, decision, or public impact.",
    "film": "Film and television the configured taste would actually care about.",
    "bot_updates": "Notes dropped off by the household's assistant bots.",
}

DEFAULT_GLOBAL_FEED = "https://feeds.bbci.co.uk/news/world/rss.xml"
DEFAULT_UK_FEED = "https://feeds.bbci.co.uk/news/uk/rss.xml"
DEFAULT_FILM_FEED = "https://www.hollywoodreporter.com/c/movies/movie-news/feed/"
DEFAULT_MARKETS_QUERY = "SpaceX OR Rocket Lab OR ASTS stock"
DEFAULT_TASTE = (
    "mystery-box world-building, character-driven thrillers, action anime, "
    "westerns, offbeat adult animation; exclude generic superheroes."
)

MAX_NEWS_ITEMS = 3
MAX_TITLE_CHARS = 32
_READER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


@dataclass(frozen=True)
class SectionSpec:
    id: str
    title: str
    kind: str
    optional: bool
    purpose: str


def env_or(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def project_dir() -> Path:
    raw = os.environ.get("DIGEST_HOME")
    if raw and raw.strip():
        return Path(raw.strip())
    return Path(__file__).resolve().parent


def valid_title(value: str) -> bool:
    text = value.strip()
    if not text or len(text) > MAX_TITLE_CHARS:
        return False
    return not any(ord(char) < 32 for char in text)


def _parse_title_json(raw: str) -> tuple[dict[str, str], str | None]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}, "DIGEST_SECTION_TITLES is not valid JSON"
    if not isinstance(parsed, dict):
        return {}, "DIGEST_SECTION_TITLES must be a JSON object"
    found: dict[str, str] = {}
    for key, value in parsed.items():
        if key not in DEFAULT_TITLES or not isinstance(value, str) or not valid_title(value):
            continue
        found[key] = value.strip()
    return found, None


def resolve_titles() -> dict[str, str]:
    titles = dict(DEFAULT_TITLES)
    raw = os.environ.get("DIGEST_SECTION_TITLES", "").strip()
    if raw:
        overrides, _error = _parse_title_json(raw)
        titles.update(overrides)
    for section_id, env_name in TITLE_ENV.items():
        value = os.environ.get(env_name, "").strip()
        if value and valid_title(value):
            titles[section_id] = value.strip()
    seen: dict[str, str] = {}
    for section_id in DEFAULT_SECTION_ORDER:
        title = titles[section_id]
        if title in seen and seen[title] != section_id:
            titles[section_id] = DEFAULT_TITLES[section_id]
        seen[titles[section_id]] = section_id
    if len(set(titles.values())) != len(titles):
        return dict(DEFAULT_TITLES)
    return titles


def resolve_order() -> tuple[str, ...]:
    raw = os.environ.get("DIGEST_SECTION_ORDER", "").strip()
    if not raw:
        return DEFAULT_SECTION_ORDER
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    known = set(DEFAULT_SECTION_ORDER)
    if not parts or len(parts) != len(set(parts)) or any(part not in known for part in parts):
        return DEFAULT_SECTION_ORDER
    return tuple(parts)


def configuration_problems() -> list[str]:
    problems: list[str] = []
    raw_order = os.environ.get("DIGEST_SECTION_ORDER", "").strip()
    if raw_order and resolve_order() == DEFAULT_SECTION_ORDER:
        parts = [part.strip() for part in raw_order.split(",") if part.strip()]
        if tuple(parts) != DEFAULT_SECTION_ORDER:
            problems.append("DIGEST_SECTION_ORDER is invalid; the default order was used")
    raw_titles = os.environ.get("DIGEST_SECTION_TITLES", "").strip()
    if raw_titles:
        _found, error = _parse_title_json(raw_titles)
        if error:
            problems.append(error)
    for env_name in TITLE_ENV.values():
        value = os.environ.get(env_name, "").strip()
        if value and not valid_title(value):
            problems.append(f"{env_name} is not a usable title")
    raw_readers = os.environ.get("DIGEST_READER_IDS", "").strip()
    if raw_readers and parse_reader_ids(raw_readers) is None:
        problems.append("DIGEST_READER_IDS is invalid; reader-a and reader-b were used")
    return problems


def sections() -> tuple[SectionSpec, ...]:
    titles = resolve_titles()
    return tuple(
        SectionSpec(
            id=section_id,
            title=titles[section_id],
            kind=SECTION_KINDS[section_id],
            optional=section_id in OPTIONAL_SECTION_IDS,
            purpose=PURPOSES[section_id],
        )
        for section_id in resolve_order()
    )


def section_by_id(section_id: str) -> SectionSpec | None:
    for spec in sections():
        if spec.id == section_id:
            return spec
    return None


def required_titles() -> tuple[str, ...]:
    return tuple(spec.title for spec in sections() if not spec.optional)


def google_news(query: str) -> str:
    return (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote_plus(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )


def film_terms() -> tuple[str, ...]:
    raw = env_or("DIGEST_FILM_TERMS", "anime,thriller,western,animation")
    return tuple(part.strip().casefold() for part in raw.split(",") if part.strip())


def news_feed(section_id: str) -> tuple[str, tuple[str, ...]]:
    """Return (feed URL, preferred terms) for one RSS section."""
    if section_id == "global":
        return env_or("DIGEST_GLOBAL_FEED", DEFAULT_GLOBAL_FEED), ()
    if section_id == "local":
        return google_news(env_or("DIGEST_LOCAL_NEWS_QUERY", "Springfield")), ()
    if section_id == "markets":
        explicit = os.environ.get("DIGEST_MARKETS_FEED", "").strip()
        if explicit:
            return explicit, ()
        return google_news(env_or("DIGEST_MARKETS_NEWS_QUERY", DEFAULT_MARKETS_QUERY)), ()
    if section_id == "uk":
        return env_or("DIGEST_UK_FEED", DEFAULT_UK_FEED), ()
    if section_id == "film":
        return env_or("DIGEST_FILM_FEED", DEFAULT_FILM_FEED), film_terms()
    raise KeyError(section_id)


def taste() -> str:
    return env_or("DIGEST_TASTE", DEFAULT_TASTE)


def candidates_per_section() -> int:
    return env_int("DIGEST_CANDIDATES_PER_SECTION", 16, 10, 20)


def news_item_count(has_bots: bool) -> int:
    if has_bots:
        return env_int("DIGEST_ITEMS_WITH_BOTS", 1, 1, MAX_NEWS_ITEMS)
    return env_int("DIGEST_ITEMS_WITHOUT_BOTS", 2, 1, MAX_NEWS_ITEMS)


def summary_limit(has_bots: bool) -> int:
    if has_bots:
        return env_int("DIGEST_SUMMARY_CHARS_WITH_BOTS", 200, 80, 420)
    return env_int("DIGEST_SUMMARY_CHARS_WITHOUT_BOTS", 300, 80, 520)


def max_news_items() -> int:
    return MAX_NEWS_ITEMS


def max_bot_updates() -> int:
    return env_int("DIGEST_BOT_UPDATES_MAX", 4, 1, 6)


def history_path() -> Path:
    raw = os.environ.get("DIGEST_HISTORY_PATH", "").strip()
    if raw:
        return Path(raw)
    return project_dir() / "digest-history.json"


def history_days() -> int:
    return env_int("DIGEST_HISTORY_DAYS", 7, 1, 30)


def bot_updates_dir() -> Path:
    raw = os.environ.get("DIGEST_BOT_UPDATES_DIR", "").strip()
    if raw:
        return Path(raw)
    return project_dir() / "bot-updates"


def bot_max_age_hours() -> int:
    return env_int("DIGEST_BOT_UPDATES_MAX_AGE_HOURS", 30, 1, 72)


def bot_future_skew_minutes() -> int:
    return env_int("DIGEST_BOT_UPDATES_FUTURE_SKEW_MINUTES", 120, 0, 720)


def parse_reader_ids(raw: str) -> tuple[str, ...] | None:
    parts = tuple(part.strip().casefold() for part in raw.split(",") if part.strip())
    if not parts or any(part == "both" or not _READER_ID.fullmatch(part) for part in parts):
        return None
    unique: list[str] = []
    for part in parts:
        if part not in unique:
            unique.append(part)
    return tuple(unique)


def reader_ids() -> tuple[str, ...]:
    raw = os.environ.get("DIGEST_READER_IDS")
    if raw is None or not raw.strip():
        return ("reader-a", "reader-b")
    return parse_reader_ids(raw) or ("reader-a", "reader-b")


def audiences() -> frozenset[str]:
    return frozenset({"both", *reader_ids()})


def llm_base_url() -> str:
    return env_or("DIGEST_LLM_BASE_URL", "https://api.x.ai/v1").rstrip("/")


def llm_api_key() -> str:
    return os.environ.get("DIGEST_LLM_API_KEY", "").strip()


def llm_model() -> str:
    # grok-4.7 is the current xAI chat model. Override DIGEST_LLM_MODEL when it changes.
    return env_or("DIGEST_LLM_MODEL", "grok-4.7")


def llm_timeout() -> float:
    return float(env_int("DIGEST_LLM_TIMEOUT", 45, 5, 90))


def llm_reasoning_effort() -> str | None:
    raw = os.environ.get("DIGEST_LLM_REASONING_EFFORT")
    if raw is None:
        return "low"
    value = raw.strip().lower()
    if value in {"", "none", "off"}:
        return None
    if value in {"low", "medium", "high", "xhigh"}:
        return value
    return "low"
