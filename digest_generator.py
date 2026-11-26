#!/usr/bin/env python3
"""Build the daily news digest plus verified local events.

News sections collect a handful of candidates, then one chat-completions call
picks and summarizes them. If that call cannot be used, a deterministic picker
still drops obituaries, ads, title-echo summaries, and recent repeats.
"""

from __future__ import annotations

import argparse
import datetime as dt
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import digest_picker
import digest_settings as settings

# All location- and person-specific settings come from the environment
# (see .env.example). Defaults are neutral placeholders.
def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


LOCATION_LABEL = _env("DIGEST_LOCATION_LABEL", "near Springfield")
# Primary calendar: a WordPress "Modern Events Calendar" (MEC) /events/ page.
NEIGHBORHOOD_EVENTS_URL = _env("DIGEST_NEIGHBORHOOD_EVENTS_URL", "https://neighborhood.example.org/events/")
NEIGHBORHOOD_NAME = _env("DIGEST_NEIGHBORHOOD_NAME", "Neighborhood")
NEIGHBORHOOD_MARKERS = tuple(
    marker.strip().casefold()
    for marker in _env("DIGEST_NEIGHBORHOOD_MARKERS", "neighborhood").split(",")
    if marker.strip()
)
# Fallback calendar: a GrowthZone chamber-of-commerce /eventcalendar page.
CHAMBER_EVENTS_URL = _env("DIGEST_CHAMBER_EVENTS_URL", "https://business.chamber.example.com/eventcalendar")
LOCAL_CITY = _env("DIGEST_LOCAL_CITY", "Springfield")
EVENT_WINDOW_DAYS = 21
MAX_EVENTS = 3
LOCAL_TZ = ZoneInfo(_env("DIGEST_TZ", "America/Chicago"))
WEATHER_POINT = (
    "https://api.weather.gov/points/"
    f"{_env('DIGEST_WEATHER_LAT', '39.7817')},{_env('DIGEST_WEATHER_LON', '-89.6501')}"
)
def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "daily-digest/2.0"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def clean(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, OverflowError):
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0


def rss_items(raw: bytes, limit: int | None = None) -> list[dict]:
    root = ET.fromstring(raw)
    items: list[dict] = []
    seen_urls: set[str] = set()
    for node in root.findall(".//item"):
        title = clean(node.findtext("title") or "")
        link = clean(node.findtext("link") or "")
        published = clean(
            node.findtext("pubDate")
            or node.findtext("{http://purl.org/dc/elements/1.1/}date")
            or ""
        )
        summary = clean(node.findtext("description") or "")[:500]
        if not title or not link:
            continue
        url_key = digest_picker.normalize_url(link)
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        items.append(
            {
                "title": title,
                "url": link,
                "published_at": published or None,
                "summary": summary,
            }
        )
    items.sort(
        key=lambda item: (parse_timestamp(item.get("published_at")), item["title"]),
        reverse=True,
    )
    cap = settings.candidates_per_section() if limit is None else limit
    return items[:cap]


def _parse_local_datetime(value: str) -> dt.datetime:
    return dt.datetime.strptime(value.strip(), "%m/%d/%Y %I:%M:%S %p").replace(
        tzinfo=LOCAL_TZ
    )


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or "source"


def _event_window(digest_date: str) -> tuple[dt.date, dt.date]:
    start = dt.date.fromisoformat(digest_date)
    return start, start + dt.timedelta(days=EVENT_WINDOW_DAYS)


def _event_item(event: dict) -> dict:
    start: dt.datetime = event["start"]
    end: dt.datetime | None = event.get("end")
    date_label = "TODAY" if event["is_today"] else start.strftime("%a %b %-d").upper()
    if event.get("time_listed", True):
        time_label = start.strftime("%-I:%M %p")
        if end:
            time_label += f"-{end.strftime('%-I:%M %p')}"
    else:
        time_label = "TIME NOT LISTED"
    location = clean(event["location"])
    scope = event["scope"]
    return {
        "title": f"{date_label} · {time_label} · {clean(event['name'])}",
        "summary": f"{location} · {scope}",
        "url": event["url"],
        "published_at": start.isoformat(),
        "event_start": start.isoformat(),
        "event_end": end.isoformat() if end else None,
        "location": location,
        "source_id": event["source_id"],
        "source_scope": scope,
        "rationale": event["rationale"],
    }


def neighborhood_events(raw: bytes, digest_date: str) -> list[dict]:
    """Parse public JSON-LD plus its adjacent MEC article time metadata."""
    text = raw.decode("utf-8", errors="replace")
    script_pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(?P<json>.*?)</script>',
        re.IGNORECASE | re.DOTALL,
    )
    window_start, window_end = _event_window(digest_date)
    events: list[dict] = []
    for match in script_pattern.finditer(text):
        article_start = text.find("<article", match.end())
        next_script = script_pattern.search(text, match.end())
        if article_start < 0 or (next_script and next_script.start() < article_start):
            continue
        article_end = text.find("</article>", article_start)
        if article_end < 0:
            continue
        article = text[article_start:article_end]
        script_text = html.unescape(match.group("json")).strip()
        try:
            value = json.loads(script_text)
        except json.JSONDecodeError:
            start_values = re.findall(r'"startDate"\s*:\s*"([^"]+)"', script_text)
            name_values = re.findall(r'"name"\s*:\s*"([^"]+)"', script_text)
            url_values = re.findall(r'"url"\s*:\s*"(https?://[^"]+)"', script_text)
            location_match = re.search(
                r'"location"\s*:\s*\{(.*?)\}\s*,\s*"organizer"',
                script_text,
                re.DOTALL,
            )
            organizer_match = re.search(
                r'"organizer"\s*:\s*\{(.*?)\}\s*,\s*"offers"',
                script_text,
                re.DOTALL,
            )
            location_text = location_match.group(1) if location_match else ""
            organizer_text = organizer_match.group(1) if organizer_match else ""
            location_names = re.findall(r'"name"\s*:\s*"([^"]*)"', location_text)
            addresses = re.findall(r'"address"\s*:\s*"([^"]*)"', location_text)
            organizer_names = re.findall(r'"name"\s*:\s*"([^"]*)"', organizer_text)
            if not start_values or not name_values or not url_values:
                continue
            value = {
                "startDate": start_values[0],
                "name": name_values[-1],
                "url": url_values[-1],
                "location": {
                    "name": location_names[-1] if location_names else "",
                    "address": addresses[-1] if addresses else "",
                },
                "organizer": {
                    "name": organizer_names[-1] if organizer_names else ""
                },
            }
        try:
            event_date = dt.date.fromisoformat(value["startDate"][:10])
        except (KeyError, TypeError, ValueError):
            continue
        if not window_start <= event_date <= window_end:
            continue
        event_id_match = re.search(r'data-event-id=["\'](\d+)["\']', article)
        start_match = re.search(
            r'class=["\']mec-start-time["\'][^>]*>(.*?)</span>', article, re.I | re.S
        )
        end_match = re.search(
            r'class=["\']mec-end-time["\'][^>]*>(.*?)</span>', article, re.I | re.S
        )
        if not event_id_match or not start_match:
            continue
        try:
            start_time = dt.datetime.strptime(clean(start_match.group(1)), "%I:%M %p").time()
            start = dt.datetime.combine(event_date, start_time, LOCAL_TZ)
            end = None
            if end_match:
                end_time = dt.datetime.strptime(clean(end_match.group(1)), "%I:%M %p").time()
                end = dt.datetime.combine(event_date, end_time, LOCAL_TZ)
        except ValueError:
            continue
        location_value = value.get("location") or {}
        venue = clean(str(location_value.get("name") or ""))
        address = clean(str(location_value.get("address") or ""))
        location = ", ".join(part for part in (venue, address) if part)
        if not location:
            continue
        name = clean(str(value.get("name") or ""))
        url = clean(str(value.get("url") or ""))
        organizer = clean(str((value.get("organizer") or {}).get("name") or ""))
        primary_text = " ".join((name, venue, address, organizer)).casefold()
        is_neighborhood = any(marker in primary_text for marker in NEIGHBORHOOD_MARKERS)
        if not name or not url:
            continue
        events.append(
            {
                "name": name,
                "start": start,
                "end": end,
                "location": location,
                "url": url,
                "source_id": f"{_host(NEIGHBORHOOD_EVENTS_URL)}:mec:{event_id_match.group(1)}",
                "scope": NEIGHBORHOOD_NAME if is_neighborhood else f"{LOCAL_CITY} area",
                "priority": 0 if is_neighborhood else 2,
                "is_today": event_date == window_start,
                "rationale": (
                    f"Official {NEIGHBORHOOD_NAME} calendar event"
                    if is_neighborhood
                    else f"{LOCAL_CITY}-area fallback from the official {NEIGHBORHOOD_NAME} calendar"
                ),
            }
        )
    return events


def chamber_event_candidates(raw: bytes, digest_date: str) -> list[dict]:
    text = raw.decode("utf-8", errors="replace")
    pattern = re.compile(
        r'<a href=["\'](?P<url>' + re.escape(CHAMBER_EVENTS_URL.rstrip("/")) + r'/Details/[^"\']+)["\']'
        r'[^>]*class=["\'][^"\']*gz-event-card-title[^"\']*["\'][^>]*>(?P<name>.*?)</a>'
        r'.*?<meta itemprop=["\']startDate["\'] content=["\'](?P<start>[^"\']+)["\']>'
        r'.*?<meta itemprop=["\']endDate["\'] content=["\'](?P<end>[^"\']+)["\']>',
        re.IGNORECASE | re.DOTALL,
    )
    window_start, window_end = _event_window(digest_date)
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for match in pattern.finditer(text):
        try:
            start = _parse_local_datetime(match.group("start"))
            end = _parse_local_datetime(match.group("end"))
        except ValueError:
            continue
        if not window_start <= start.date() <= window_end:
            continue
        name = clean(match.group("name"))
        url = html.unescape(match.group("url"))
        key = (url.split("?", 1)[0], start.isoformat())
        if key in seen:
            continue
        seen.add(key)
        result.append({"name": name, "url": url, "start": start, "end": end})
    return sorted(result, key=lambda event: (event["start"], event["name"]))


def chamber_event_location(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    match = re.search(
        r'<div[^>]+class=["\'][^"\']*gz-event-address[^"\']*["\'][^>]*>(.*?)</div>',
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    value = re.sub(r"<br\s*/?>", ", ", match.group(1), flags=re.IGNORECASE)
    value = clean(value)
    value = re.sub(r"^Address\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+,", ",", value)
    parts: list[str] = []
    for part in (piece.strip() for piece in value.split(",")):
        words = part.split()
        midpoint = len(words) // 2
        if len(words) >= 4 and len(words) % 2 == 0:
            if [word.casefold() for word in words[:midpoint]] == [
                word.casefold() for word in words[midpoint:]
            ]:
                part = " ".join(words[:midpoint])
        if part:
            parts.append(part)
    return ", ".join(parts)


def _events_title() -> str:
    spec = settings.section_by_id("events")
    if spec is not None:
        return spec.title
    return settings.DEFAULT_TITLES["events"]


def build_events_section(
    digest_date: str, fetcher: Callable[[str], bytes]
) -> tuple[dict, list[dict]]:
    errors: list[dict] = []
    events_title = _events_title()
    neighborhood: list[dict] = []
    chamber: list[dict] = []
    try:
        neighborhood = neighborhood_events(fetcher(NEIGHBORHOOD_EVENTS_URL), digest_date)
    except Exception as exc:
        errors.append(
            {
                "section": events_title,
                "url": NEIGHBORHOOD_EVENTS_URL,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    try:
        candidates = chamber_event_candidates(fetcher(CHAMBER_EVENTS_URL), digest_date)
        for candidate in candidates[:12]:
            if len(chamber) >= MAX_EVENTS:
                break
            try:
                location = chamber_event_location(fetcher(candidate["url"]))
            except Exception as exc:
                errors.append(
                    {
                        "section": events_title,
                        "url": candidate["url"],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if LOCAL_CITY.casefold() not in location.casefold():
                continue
            identifier = re.search(r"-(\d+)(?:\?|$)", candidate["url"])
            candidate.update(
                {
                    "location": location,
                    "source_id": (
                        f"{_host(CHAMBER_EVENTS_URL)}:event:{identifier.group(1)}"
                        if identifier
                        else candidate["url"]
                    ),
                    "scope": f"{LOCAL_CITY} area",
                    "priority": 1,
                    "is_today": candidate["start"].date()
                    == dt.date.fromisoformat(digest_date),
                    "rationale": f"{LOCAL_CITY}-area fallback from the chamber of commerce calendar",
                }
            )
            chamber.append(candidate)
    except Exception as exc:
        errors.append(
            {
                "section": events_title,
                "url": CHAMBER_EVENTS_URL,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )

    primary = sorted(
        (event for event in neighborhood if event["priority"] == 0),
        key=lambda event: (event["start"], event["name"]),
    )
    official_fallback = sorted(
        (event for event in neighborhood if event["priority"] == 2),
        key=lambda event: (event["start"], event["name"]),
    )
    selected: list[dict] = []
    for pool in (primary, chamber, official_fallback):
        for event in pool:
            identity = (event["url"].split("?", 1)[0], event["start"].isoformat())
            if any(
                (prior["url"].split("?", 1)[0], prior["start"].isoformat()) == identity
                for prior in selected
            ):
                continue
            selected.append(event)
            if len(selected) == MAX_EVENTS:
                break
        if len(selected) == MAX_EVENTS:
            break

    window_start, window_end = _event_window(digest_date)
    if selected:
        items = [_event_item(event) for event in selected]
    else:
        items = [
            {
                "title": f"No verified local events {window_start:%b %-d}-{window_end:%b %-d}",
                "summary": f"The configured {NEIGHBORHOOD_NAME} and {LOCAL_CITY} calendars returned no verifiable events in the today/soon window.",
                "url": NEIGHBORHOOD_EVENTS_URL,
                "published_at": None,
                "source_id": "event-window-empty",
                "rationale": "No source event was available; no event was invented.",
            }
        ]
    return (
        {
            "section": events_title,
            "source_feed": NEIGHBORHOOD_EVENTS_URL,
            "source_feeds": [NEIGHBORHOOD_EVENTS_URL, CHAMBER_EVENTS_URL],
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "window_days": EVENT_WINDOW_DAYS,
            "priority_order": [NEIGHBORHOOD_NAME, f"{LOCAL_CITY} area"],
            "items": items,
        },
        errors,
    )


def select_daytime_period(
    periods: list[dict], digest_date: str, tz: ZoneInfo
) -> tuple[dict, dict | None]:
    """First isDaytime period whose start falls on the digest date, plus the following night."""
    if not isinstance(periods, list):
        raise LookupError("forecast periods missing")
    today = dt.date.fromisoformat(digest_date)
    chosen_index: int | None = None
    for index, period in enumerate(periods):
        if not isinstance(period, dict) or not period.get("isDaytime"):
            continue
        start_raw = period.get("startTime")
        if not isinstance(start_raw, str):
            continue
        try:
            start = dt.datetime.fromisoformat(start_raw)
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        if start.astimezone(tz).date() == today:
            chosen_index = index
            break
    if chosen_index is None:
        raise LookupError(f"no daytime forecast period on {digest_date}")
    night = None
    if chosen_index + 1 < len(periods):
        following = periods[chosen_index + 1]
        if isinstance(following, dict) and not following.get("isDaytime"):
            night = following
    return periods[chosen_index], night


def _temperature_text(period: dict) -> str | None:
    if period.get("temperature") is None:
        return None
    unit = period.get("temperatureUnit") or "F"
    return f"{period['temperature']}°{unit}"


def _trim_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    window = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:")
    if not window:
        return text[:limit].rstrip()
    if window[-1] not in ".!?":
        window += "."
    return window


def format_weather_item(day: dict, night: dict | None, forecast_url: str) -> dict:
    high = _temperature_text(day)
    if high is None:
        raise KeyError("daytime temperature")
    name = clean(str(day.get("name") or "Today")) or "Today"
    pieces = [f"{name}: high {high}"]
    if night is not None:
        low = _temperature_text(night)
        if low is not None:
            pieces.append(f"low {low}")
    precipitation = day.get("probabilityOfPrecipitation")
    if isinstance(precipitation, dict) and isinstance(precipitation.get("value"), (int, float)):
        pieces.append(f"{int(round(float(precipitation['value'])))}% chance of precipitation")
    short = clean(str(day.get("shortForecast") or ""))
    headline = ", ".join(pieces)
    if short:
        headline = f"{headline}. {short}"
    summary = _trim_words(clean(str(day.get("detailedForecast") or "")), 480)
    if not summary:
        summary = short or headline
    return {
        "headline": headline,
        "summary": summary,
        "url": forecast_url,
        # The period start is stable across repeated renders. now() would change the digest id.
        "published_at": day.get("startTime"),
        "rationale": "NOAA daytime forecast for the digest date; the overnight period is not used",
    }


def weather(fetcher: Callable[[str], bytes], digest_date: str) -> dict:
    point = json.loads(fetcher(WEATHER_POINT))
    forecast_url = point["properties"]["forecast"]
    forecast = json.loads(fetcher(forecast_url))
    periods = forecast["properties"]["periods"]
    try:
        day, night = select_daytime_period(periods, digest_date, LOCAL_TZ)
    except LookupError:
        return {
            "headline": f"No daytime forecast for {digest_date}",
            "summary": (
                "NOAA returned forecast periods, but none is a daytime period on this date. "
                "The overnight period was not used."
            ),
            "url": forecast_url,
            "published_at": None,
            "rationale": "No daytime period on the digest date",
        }
    return format_weather_item(day, night, forecast_url)


def compute_digest_id(payload: dict) -> str:
    stable = {key: value for key, value in payload.items() if key not in {"generated_at", "digest_id"}}
    canonical = json.dumps(
        stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _unavailable_news_item(feed_url: str, exc: Exception) -> dict:
    return {
        "title": "No new update today (source unavailable)",
        "summary": "The configured source could not be reached for this edition.",
        "url": feed_url,
        "published_at": None,
        "rationale": f"source unavailable: {type(exc).__name__}",
    }


def make_payload(
    digest_date: str,
    fetcher: Callable[[str], bytes] = fetch,
    *,
    opener: Callable | None = None,
    now: dt.datetime | None = None,
    history_file: Path | None = None,
    bot_directory: Path | None = None,
) -> dict:
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    errors: list[dict] = [
        {"section": "CONFIG", "url": "env", "error": problem}
        for problem in settings.configuration_problems()
    ]
    specs = settings.sections()
    by_id: dict[str, dict] = {}

    weather_spec = settings.section_by_id("weather")
    if weather_spec is not None:
        try:
            weather_item = weather(fetcher, digest_date)
        except Exception as exc:  # source failure is represented, never invented
            errors.append(
                {"section": weather_spec.title, "url": WEATHER_POINT, "error": f"{type(exc).__name__}: {exc}"}
            )
            weather_item = {
                "headline": "No new update today (weather source unavailable)",
                "summary": "The configured NOAA forecast could not be reached for this edition.",
                "url": WEATHER_POINT,
                "published_at": None,
                "rationale": f"weather unavailable: {type(exc).__name__}",
            }
        by_id["weather"] = {
            "section": weather_spec.title,
            "source_feed": WEATHER_POINT,
            "items": [weather_item],
        }

    news_requests: list[dict] = []
    for spec in specs:
        if spec.kind != "news":
            continue
        feed_url, prefer_terms = settings.news_feed(spec.id)
        try:
            candidates = rss_items(fetcher(feed_url))
        except Exception as exc:
            errors.append({"section": spec.title, "url": feed_url, "error": f"{type(exc).__name__}: {exc}"})
            by_id[spec.id] = {
                "section": spec.title,
                "source_feed": feed_url,
                "items": [_unavailable_news_item(feed_url, exc)],
            }
            continue
        news_requests.append(
            {
                "section_id": spec.id,
                "title": spec.title,
                "purpose": spec.purpose,
                "feed_url": feed_url,
                "prefer_terms": prefer_terms,
                "candidates": candidates,
            }
        )

    if settings.section_by_id("events") is not None:
        events_section, event_errors = build_events_section(digest_date, fetcher)
        by_id["events"] = events_section
        errors.extend(event_errors)

    bot_spec = settings.section_by_id("bot_updates")
    if bot_spec is not None:
        bot_items, bot_error = digest_picker.load_bot_updates(
            bot_directory or settings.bot_updates_dir(),
            digest_date,
            moment,
        )
        if bot_error:
            errors.append(
                {
                    "section": bot_spec.title,
                    "url": str((bot_directory or settings.bot_updates_dir()) / f"{digest_date}.json"),
                    "error": bot_error,
                }
            )
        if bot_items:
            by_id["bot_updates"] = {
                "section": bot_spec.title,
                "source_feed": str(bot_directory or settings.bot_updates_dir()),
                "items": bot_items,
            }

    history_path = history_file or settings.history_path()
    history, history_error = digest_picker.load_history(history_path)
    if history_error:
        errors.append({"section": "HISTORY", "url": str(history_path), "error": history_error})
    picked, picker_meta, picker_errors, history_records = digest_picker.pick_news(
        news_requests,
        digest_date=digest_date,
        history=history,
        has_bots="bot_updates" in by_id,
        taste=settings.taste(),
        opener=opener,
    )
    errors.extend(picker_errors)
    feeds = {request["section_id"]: request["feed_url"] for request in news_requests}
    for section_id, items in picked.items():
        spec = settings.section_by_id(section_id)
        if spec is None:
            continue
        by_id[section_id] = {
            "section": spec.title,
            "source_feed": feeds.get(section_id, ""),
            "items": items,
        }

    try:
        digest_picker.save_history(history_path, digest_date, history_records)
    except Exception as exc:
        errors.append({"section": "HISTORY", "url": str(history_path), "error": f"{type(exc).__name__}: {exc}"})

    ordered = [by_id[spec.id] for spec in specs if spec.id in by_id]
    payload = {
        "schema_version": "1.0",
        "digest_date": digest_date,
        "location": LOCATION_LABEL,
        "taste_profile": settings.taste(),
        "picker": picker_meta,
        "sections": ordered,
        "source_errors": errors,
        "generated_at": moment.astimezone(dt.timezone.utc).isoformat(),
        "credentials_in_label": False,
    }
    payload["digest_id"] = compute_digest_id(payload)
    return payload


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with open(fd, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=dt.date.today().isoformat())
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    payload = make_payload(args.date)
    atomic_json(args.out, payload)
    print(json.dumps({"digest_id": payload["digest_id"], "picker": payload.get("picker", {}).get("mode"), "sections": len(payload["sections"]), "source_errors": payload["source_errors"], "output": str(args.out.resolve())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
