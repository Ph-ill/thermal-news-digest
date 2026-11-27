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
    "companies",
    "shows",
    "bot_updates",
)

OPTIONAL_SECTION_IDS = frozenset({"shows", "bot_updates"})

DEFAULT_TITLES = {
    "weather": "WEATHER",
    "global": "GLOBAL",
    "local": "BASTROP",
    "companies": "SPACEX & EARTH FUNERAL",
    "shows": "NEARBY SHOWS",
    "bot_updates": "GROK BOT UPDATES",
}

TITLE_ENV = {
    "weather": "DIGEST_SECTION_TITLE_WEATHER",
    "global": "DIGEST_SECTION_TITLE_GLOBAL",
    "local": "DIGEST_SECTION_TITLE_LOCAL",
    "companies": "DIGEST_SECTION_TITLE_COMPANIES",
    "shows": "DIGEST_SECTION_TITLE_SHOWS",
    "bot_updates": "DIGEST_SECTION_TITLE_BOT_UPDATES",
}

SECTION_KINDS = {
    "weather": "weather",
    "global": "news",
    "local": "news",
    "companies": "news",
    "shows": "shows",
    "bot_updates": "bot_updates",
}

PURPOSES = {
    "weather": "Daytime forecast for Bastrop, Texas.",
    "global": (
        "World news that will still matter by tonight. Prefer reported events over columns. "
        "Follow the interests profile. You decide what is relevant. A dislike is a nudge, not a ban, "
        "except for obituaries."
    ),
    "local": (
        "Bastrop and nearby, for a childless couple in their early 30s. "
        "Want new openings, food trucks, bars, tastings, live music, markets, and outdoor things to do "
        "in Bastrop, Smithville, Elgin, Cedar Creek, and east Austin. "
        "No sports. No civic items: city council meetings and votes, government meetings, school board, "
        "crime, or obituaries. A story that only mentions city council in passing can stay."
    ),
    "companies": (
        "Company news about SpaceX and about Earth Funeral, the human-composting company. "
        "Prefer launches, milestones, facilities, and press. Skip share prices, stock prices, and "
        "market chatter such as a stock that jumps, drops, or is upgraded. "
        "A stock of engines, or something that is in stock, is not market chatter. "
        "If one company has no real story, leave it out. Do not fill the gap with unrelated items."
    ),
    "shows": "Upcoming concerts by the configured artists within about an hour of Bastrop.",
    "bot_updates": "Notes dropped off by the household's assistant bots.",
}

# Public city center of Bastrop, Texas. Not a street address.
DEFAULT_LAT = "30.1105"
DEFAULT_LON = "-97.3153"
DEFAULT_GLOBAL_FEED = "https://feeds.bbci.co.uk/news/world/rss.xml"
# Community Impact documents RSS at /rss/. The Bastrop edition page does not currently publish its own feed.
DEFAULT_COMMUNITY_IMPACT_FEED = "https://communityimpact.com/rss/"
DEFAULT_EATER_AUSTIN_FEED = "https://austin.eater.com/rss/index.xml"
DEFAULT_LOCAL_QUERY = (
    'Bastrop (restaurant OR bar OR brewery OR coffee OR opening OR "now open" OR market '
    'OR "live music" OR trail OR park OR festival)'
)
DEFAULT_NEARBY_QUERY = (
    '(Smithville OR Elgin OR "Cedar Creek") (Texas) (restaurant OR bar OR brewery OR opening '
    'OR market OR festival OR "live music" OR trail)'
)
DEFAULT_EAST_AUSTIN_QUERY = '"east Austin" (restaurant OR bar OR brewery OR coffee OR opening OR "live music" OR market)'
DEFAULT_SPACEX_QUERY = '"SpaceX" (Starship OR Falcon OR Starlink OR launch OR mission OR NASA)'
DEFAULT_EARTH_QUERY = '"Earth Funeral" (compost OR "soil transformation" OR "organic reduction" OR facility OR opens OR announces)'
DEFAULT_TASTE = (
    "cerebral sci-fi, space, horror, and new local food and drink openings; "
    "skip politics, celebrity gossip, and sports."
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


@dataclass(frozen=True)
class FeedSpec:
    url: str
    topic: str = ""
    # When set, a candidate must contain one of these phrases. Stops a wide feed from padding a section.
    required: tuple[str, ...] = ()
    # Wide local feeds (a site-wide RSS, an Austin food blog) must name the area.
    require_place: bool = False


def _split_urls(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _explicit_feeds(names: tuple[str, ...], topic: str = "", required: tuple[str, ...] = ()) -> tuple[FeedSpec, ...] | None:
    for name in names:
        raw = os.environ.get(name, "").strip()
        if raw:
            return tuple(FeedSpec(url, topic, required) for url in _split_urls(raw))
    return None


def feeds_for(section_id: str) -> tuple[FeedSpec, ...]:
    """RSS sources for one news section. An explicit env list replaces the defaults."""
    if section_id == "global":
        explicit = _explicit_feeds(("DIGEST_GLOBAL_FEED",))
        if explicit:
            return explicit
        return (FeedSpec(DEFAULT_GLOBAL_FEED),)
    if section_id == "local":
        explicit = _explicit_feeds(("DIGEST_LOCAL_FEEDS", "DIGEST_LOCAL_FEED"), topic="local")
        if explicit:
            return explicit
        query = env_or("DIGEST_LOCAL_NEWS_QUERY", "")
        if query:
            return (FeedSpec(google_news(query), "local"),)
        # The Google queries already name the towns, so a headline need not repeat them.
        # Community Impact and Eater cover a wider area, so those still need a town name.
        return (
            FeedSpec(google_news(DEFAULT_LOCAL_QUERY), "local", (), False),
            FeedSpec(google_news(DEFAULT_NEARBY_QUERY), "local", (), False),
            FeedSpec(google_news(DEFAULT_EAST_AUSTIN_QUERY), "local", (), False),
            FeedSpec(env_or("DIGEST_COMMUNITY_IMPACT_FEED", DEFAULT_COMMUNITY_IMPACT_FEED), "local", (), True),
            FeedSpec(env_or("DIGEST_EATER_AUSTIN_FEED", DEFAULT_EATER_AUSTIN_FEED), "local", (), True),
        )
    if section_id == "companies":
        explicit = _explicit_feeds(("DIGEST_COMPANIES_FEEDS", "DIGEST_COMPANIES_FEED"))
        if explicit:
            return explicit
        spacex = os.environ.get("DIGEST_SPACEX_FEED", "").strip() or google_news(
            env_or("DIGEST_SPACEX_QUERY", DEFAULT_SPACEX_QUERY)
        )
        earth = os.environ.get("DIGEST_EARTH_FUNERAL_FEED", "").strip() or google_news(
            env_or("DIGEST_EARTH_FUNERAL_QUERY", DEFAULT_EARTH_QUERY)
        )
        return (
            FeedSpec(spacex, "spacex", ("spacex", "starship", "falcon", "starlink")),
            FeedSpec(earth, "earth", ("earth funeral",)),
        )
    raise KeyError(section_id)


def fresh_hours() -> int:
    return env_int("DIGEST_FRESH_HOURS", 36, 6, 168)


def local_fresh_hours() -> int:
    """BASTROP window. Same 6–168 cap as DIGEST_FRESH_HOURS, default one week."""
    return env_int("DIGEST_LOCAL_FRESH_HOURS", 168, 6, 168)


def company_stale_hours() -> int:
    """How old a company story may be when that company has nothing newer."""
    return env_int("DIGEST_COMPANY_STALE_HOURS", 24 * 14, 36, 24 * 60)


def profile_path() -> Path | None:
    raw = os.environ.get("DIGEST_PROFILE_FILE", "").strip()
    if not raw:
        return None
    return Path(raw)


def artists_path() -> Path | None:
    raw = os.environ.get("DIGEST_ARTISTS_FILE", "").strip()
    if not raw:
        return None
    return Path(raw)


def ticketmaster_key() -> str:
    return os.environ.get("DIGEST_TICKETMASTER_KEY", "").strip()


def shows_lat() -> float:
    try:
        return float(env_or("DIGEST_SHOWS_LAT", DEFAULT_LAT))
    except ValueError:
        return float(DEFAULT_LAT)


def shows_lon() -> float:
    try:
        return float(env_or("DIGEST_SHOWS_LON", DEFAULT_LON))
    except ValueError:
        return float(DEFAULT_LON)


def shows_radius_miles() -> int:
    return env_int("DIGEST_SHOWS_RADIUS_MILES", 50, 5, 150)


def shows_horizon_days() -> int:
    return env_int("DIGEST_SHOWS_HORIZON_DAYS", 90, 7, 180)


def taste() -> str:
    return env_or("DIGEST_TASTE", DEFAULT_TASTE)


def candidates_per_section() -> int:
    return env_int("DIGEST_CANDIDATES_PER_SECTION", 16, 10, 20)


def news_item_count(has_bots: bool, *, crowded: bool = False) -> int:
    if has_bots or crowded:
        return env_int("DIGEST_ITEMS_WITH_BOTS", 1, 1, MAX_NEWS_ITEMS)
    return env_int("DIGEST_ITEMS_WITHOUT_BOTS", 2, 1, MAX_NEWS_ITEMS)


def summary_limit(has_bots: bool, *, crowded: bool = False) -> int:
    if has_bots or crowded:
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


def picks_dir() -> Path:
    raw = os.environ.get("DIGEST_PICKS_DIR", "").strip()
    if raw:
        return Path(raw)
    return project_dir() / "picks"


def candidates_dir() -> Path:
    raw = os.environ.get("DIGEST_CANDIDATES_DIR", "").strip()
    if raw:
        return Path(raw)
    return project_dir() / "output"


def candidates_path(digest_date: str) -> Path:
    return candidates_dir() / f"{digest_date}.json"


def bot_updates_dir() -> Path:
    raw = os.environ.get("DIGEST_BOT_UPDATES_DIR", "").strip()
    if raw:
        return Path(raw)
    return project_dir() / "bot-updates"


def avatars_dir() -> Path:
    raw = os.environ.get("DIGEST_AVATARS_DIR", "").strip()
    if raw:
        return Path(raw)
    return project_dir() / "avatars"


def bot_updates_style() -> str:
    """``plain`` (default) or ``bubble``. Anything else stays on the plain layout."""
    raw = os.environ.get("DIGEST_BOT_UPDATES_STYLE", "plain").strip().lower()
    if raw == "bubble":
        return "bubble"
    return "plain"


def avatar_threshold() -> int | None:
    """Optional 0–255 dither midpoint. Unset keeps the contrast-boosted default."""
    raw = os.environ.get("DIGEST_AVATAR_THRESHOLD", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if 0 <= value <= 255:
        return value
    return None


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
