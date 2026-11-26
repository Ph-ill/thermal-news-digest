#!/usr/bin/env python3
"""Build the daily news digest, including nearby concerts when they are configured.

News sections collect a handful of candidates, then one chat-completions call
picks and summarizes them. If that call cannot be used, a deterministic picker
still drops obituaries, ads, title-echo summaries, and recent repeats.
"""

from __future__ import annotations

import argparse
import contextvars
import datetime as dt
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import digest_picker
import digest_settings as settings

# Place defaults are the public city center of Bastrop, Texas. Not a street address.
def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


LOCATION_LABEL = _env("DIGEST_LOCATION_LABEL", "Bastrop, TX")
# Leaves room inside the 120s trigger for render and the CUPS poll.
GENERATOR_BUDGET_SECONDS = 75.0
FETCH_TIMEOUT_CAP = 8.0
FETCH_READ_CAP = 1_048_576
TITLE_CAP = 240
MAX_SHOWS = 3
MAX_ARTISTS = 8
TICKETMASTER_EVENTS = "https://app.ticketmaster.com/discovery/v2/events.json"
_GEOHASH_CHARS = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float, lon: float, precision: int = 7) -> str:
    """Encode a point the way Ticketmaster's geoPoint parameter expects."""
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    bits = (16, 8, 4, 2, 1)
    chars: list[str] = []
    bit = 0
    value = 0
    even = True
    while len(chars) < precision:
        if even:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                value |= bits[bit]
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                value |= bits[bit]
                lat_range[0] = mid
            else:
                lat_range[1] = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            chars.append(_GEOHASH_CHARS[value])
            bit = 0
            value = 0
    return "".join(chars)


def _weather_point() -> str:
    explicit = os.environ.get("DIGEST_WEATHER_POINT", "").strip()
    if explicit:
        return explicit
    return (
        "https://api.weather.gov/points/"
        f"{_env('DIGEST_WEATHER_LAT', settings.DEFAULT_LAT)},{_env('DIGEST_WEATHER_LON', settings.DEFAULT_LON)}"
    )


WEATHER_POINT = _weather_point()


class Budget:
    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + max(0.0, seconds)

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


_budget_var: contextvars.ContextVar[Budget | None] = contextvars.ContextVar("digest_budget", default=None)


def local_zone() -> ZoneInfo:
    name = os.environ.get("DIGEST_TZ") or "America/Chicago"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("America/Chicago")


def digest_date(moment: dt.datetime | None = None) -> str:
    """Calendar date of the edition in DIGEST_TZ, not the host's local zone."""
    current = moment or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(local_zone()).date().isoformat()


def fetch(url: str) -> bytes:
    budget = _budget_var.get()
    timeout = FETCH_TIMEOUT_CAP
    deadline = None
    if budget is not None:
        timeout = min(FETCH_TIMEOUT_CAP, max(0.0, budget.remaining()))
        if timeout < 0.05:
            raise TimeoutError("budget")
        deadline = budget.deadline
    request = urllib.request.Request(url, headers={"User-Agent": "daily-digest/2.0"})
    try:
        with digest_picker.open_bounded(request, timeout, deadline) as response:
            return digest_picker.read_bounded(response, FETCH_READ_CAP, deadline)
    except TimeoutError:
        raise TimeoutError("budget")
    except Exception as exc:
        # Header and connect failures surface after the watchdog closes the socket.
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("budget") from exc
        raise


def clean(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = digest_picker.xml_text(html.unescape(value))
    return re.sub(r"\s+", " ", value).strip()


def _failure_name(exc: BaseException) -> str:
    return type(exc).__name__


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
        title = title[:TITLE_CAP]
        if not title or not link:
            continue
        url_key = digest_picker.normalize_url(link)
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        items.append(
            {
                "title": title,
                "url": digest_picker.strip_userinfo(link),
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


def load_artists(path: Path) -> list[str]:
    raw = digest_picker._read_regular_nofollow(path, digest_picker.ARTISTS_MAX_BYTES + 1)
    if len(raw) > digest_picker.ARTISTS_MAX_BYTES:
        raise OSError("artists file is too large")
    artists: list[str] = []
    for line in raw.decode("utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if name and name not in artists:
            artists.append(name)
    return artists[:MAX_ARTISTS]


def ticketmaster_events_url(artist: str, start: str, end: str) -> str:
    """Discovery API search. The key stays in this URL and must not be stored."""
    query = urllib.parse.urlencode(
        {
            "apikey": settings.ticketmaster_key(),
            "keyword": artist,
            "classificationName": "music",
            "latlong": f"{settings.shows_lat():.4f},{settings.shows_lon():.4f}",
            "geoPoint": geohash(settings.shows_lat(), settings.shows_lon()),
            "radius": str(settings.shows_radius_miles()),
            "unit": "miles",
            "sort": "date,asc",
            "size": "5",
            "startDateTime": start,
            "endDateTime": end,
        }
    )
    return TICKETMASTER_EVENTS + "?" + query


def _shows_window(digest_day: str) -> tuple[str, str]:
    day = dt.date.fromisoformat(digest_day)
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(days=settings.shows_horizon_days())
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def build_shows_section(
    digest_day: str,
    fetcher: Callable[[str], bytes],
    history: list[dict],
) -> tuple[dict | None, list[dict], list[dict]]:
    """Upcoming concerts. Returns no section when the key, file, or matches are missing."""
    spec = settings.section_by_id("shows")
    if spec is None or not settings.ticketmaster_key():
        return None, [], []
    path = settings.artists_path()
    if path is None or not path.is_file():
        return None, [], []
    try:
        artists = load_artists(path)
    except Exception:
        return None, [{"section": spec.title, "url": "artists", "error": "artists file ignored: unreadable"}], []
    if not artists:
        return None, [], []
    try:
        start, end = _shows_window(digest_day)
    except ValueError:
        return None, [], []
    errors: list[dict] = []
    found: list[dict] = []
    for artist in artists:
        # The request URL contains the API key. Errors cite the bare endpoint instead.
        url = ticketmaster_events_url(artist, start, end)
        try:
            raw = fetcher(url)
        except Exception as exc:
            errors.append({"section": spec.title, "url": TICKETMASTER_EVENTS, "error": _failure_name(exc)})
            continue
        try:
            found.extend(
                digest_picker.shows_from_ticketmaster(
                    raw,
                    artist,
                    digest_day,
                    history,
                    settings.shows_horizon_days(),
                )
            )
        except Exception:
            errors.append({"section": spec.title, "url": TICKETMASTER_EVENTS, "error": "ticketmaster_parse"})
    if not found:
        return None, errors, []
    found.sort(key=lambda item: (item["event_start"], item["title"]))
    items: list[dict] = []
    records: list[dict] = []
    seen_ids: set[str] = set()
    for item in found:
        if item["show_id"] in seen_ids:
            continue
        seen_ids.add(item["show_id"])
        items.append(
            {
                "title": item["title"],
                "summary": item["summary"],
                "url": item["url"],
                "published_at": item["event_start"],
            }
        )
        records.append(
            {
                "digest_date": digest_day,
                "section": "shows",
                "url": digest_picker.normalize_url(item["url"]),
                "title": digest_picker.normalize_title(item["title"]),
                "show_id": item["show_id"],
                "event_start": item["event_start"],
                "phase": item["phase"],
            }
        )
        if len(items) >= MAX_SHOWS:
            break
    return {"section": spec.title, "source_feed": TICKETMASTER_EVENTS, "items": items}, errors, records


def _period_on_date(period: dict, today: dt.date, tz: ZoneInfo) -> bool:
    start_raw = period.get("startTime")
    if not isinstance(start_raw, str):
        return False
    try:
        start = dt.datetime.fromisoformat(start_raw)
    except ValueError:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    return start.astimezone(tz).date() == today


def _temperature_value(period: dict) -> float:
    temperature = period.get("temperature")
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        return float("-inf")
    return float(temperature)


def select_daytime_period(
    periods: list[dict], digest_date: str, tz: ZoneInfo
) -> tuple[dict, dict | None]:
    """Hottest daytime period on the digest date, plus the next nighttime period after it.

    A split day (This Morning, then This Afternoon) keeps the higher temperature.
    The low can come from a later night period that is not the immediate neighbor.
    """
    if not isinstance(periods, list):
        raise LookupError("forecast periods missing")
    today = dt.date.fromisoformat(digest_date)
    candidates: list[tuple[float, int, dict]] = []
    for index, period in enumerate(periods):
        if not isinstance(period, dict) or not period.get("isDaytime"):
            continue
        if not _period_on_date(period, today, tz):
            continue
        candidates.append((_temperature_value(period), index, period))
    if not candidates:
        raise LookupError(f"no daytime forecast period on {digest_date}")
    _temperature, chosen_index, day = max(candidates, key=lambda row: (row[0], row[1]))
    night = None
    for following in periods[chosen_index + 1 :]:
        if isinstance(following, dict) and not following.get("isDaytime"):
            night = following
            break
    return day, night


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
    forecast_url = digest_picker.strip_userinfo(forecast_url)
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
    forecast_url = digest_picker.strip_userinfo(str(point["properties"]["forecast"]))
    forecast = json.loads(fetcher(forecast_url))
    periods = forecast["properties"]["periods"]
    try:
        day, night = select_daytime_period(periods, digest_date, local_zone())
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
        "url": digest_picker.strip_userinfo(feed_url),
        "published_at": None,
        "rationale": f"source unavailable: {_failure_name(exc)}",
    }


def _guarded(fetcher: Callable[[str], bytes]) -> Callable[[str], bytes]:
    def wrapped(url: str) -> bytes:
        budget = _budget_var.get()
        if budget is not None and budget.remaining() < 0.05:
            raise TimeoutError("budget")
        return fetcher(url)

    return wrapped


def _emergency_payload(digest_date: str, moment: dt.datetime) -> dict:
    """A printable edition used only when building the real one raises."""
    sections = []
    for spec in settings.sections():
        if spec.optional:
            continue
        if spec.kind == "weather":
            item = {
                "headline": "Forecast unavailable",
                "summary": "The forecast could not be built for this edition.",
                "url": "https://api.weather.gov/",
                "published_at": None,
            }
        else:
            item = {
                "title": "No new update today",
                "summary": "This section could not be built for this edition.",
                "url": "https://example.invalid/feed",
                "published_at": None,
            }
        sections.append({"section": spec.title, "source_feed": "unavailable", "items": [item]})
    payload = {
        "schema_version": "1.0",
        "digest_date": digest_date,
        "location": LOCATION_LABEL,
        "picker": {"mode": "fallback", "model": None, "reason": "generator_error"},
        "sections": sections,
        "source_errors": [{"section": "GENERATOR", "url": "local", "error": "generator_error"}],
        "generated_at": moment.astimezone(dt.timezone.utc).isoformat(),
        "credentials_in_label": False,
    }
    payload["digest_id"] = compute_digest_id(payload)
    return payload


def make_payload(
    digest_date: str,
    fetcher: Callable[[str], bytes] = fetch,
    *,
    opener: Callable | None = None,
    now: dt.datetime | None = None,
    history_file: Path | None = None,
    bot_directory: Path | None = None,
    budget_seconds: float = GENERATOR_BUDGET_SECONDS,
    persist_history: bool = True,
    history_out: list | None = None,
) -> dict:
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    token = _budget_var.set(Budget(budget_seconds))
    try:
        return _make_payload(
            digest_date,
            fetcher,
            opener=opener,
            moment=moment,
            history_file=history_file,
            bot_directory=bot_directory,
            persist_history=persist_history,
            history_out=history_out,
        )
    finally:
        _budget_var.reset(token)


def _make_payload(
    digest_date: str,
    fetcher: Callable[[str], bytes],
    *,
    opener: Callable | None,
    moment: dt.datetime,
    history_file: Path | None,
    bot_directory: Path | None,
    persist_history: bool,
    history_out: list | None,
) -> dict:
    bounded = _guarded(fetcher)
    budget = _budget_var.get()
    deadline = None if budget is None else budget.deadline
    errors: list[dict] = [
        {"section": "CONFIG", "url": "env", "error": problem}
        for problem in settings.configuration_problems()
    ]
    specs = settings.sections()
    by_id: dict[str, dict] = {}

    weather_spec = settings.section_by_id("weather")
    if weather_spec is not None:
        try:
            weather_item = weather(bounded, digest_date)
        except Exception as exc:  # source failure is represented, never invented
            errors.append(
                {"section": weather_spec.title, "url": WEATHER_POINT, "error": _failure_name(exc)}
            )
            weather_item = {
                "headline": "No new update today (weather source unavailable)",
                "summary": "The configured NOAA forecast could not be reached for this edition.",
                "url": WEATHER_POINT,
                "published_at": None,
                "rationale": f"weather unavailable: {_failure_name(exc)}",
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
        try:
            feeds = settings.feeds_for(spec.id)
        except Exception as exc:
            errors.append({"section": spec.title, "url": "feeds", "error": _failure_name(exc)})
            continue
        collected: list[dict] = []
        public_urls: list[str] = []
        failures = 0
        for feed in feeds:
            public = digest_picker.strip_userinfo(feed.url)
            public_urls.append(public)
            try:
                raw_items = rss_items(bounded(feed.url))
            except Exception as exc:
                failures += 1
                errors.append({"section": spec.title, "url": public, "error": _failure_name(exc)})
                continue
            for raw in raw_items:
                item = dict(raw)
                item["topic"] = feed.topic
                blob = f"{item.get('title') or ''} {item.get('summary') or ''}"
                if feed.required and not any(term in blob.casefold() for term in feed.required):
                    continue
                if spec.id == "local" and not digest_picker.keep_local(item, require_place=feed.require_place):
                    continue
                if spec.id == "companies" and not digest_picker.keep_company(item):
                    continue
                collected.append(item)
        if feeds and failures == len(feeds):
            first = public_urls[0]
            by_id[spec.id] = {
                "section": spec.title,
                "source_feed": first,
                "items": [_unavailable_news_item(first, TimeoutError("unavailable"))],
            }
            continue
        if spec.id == "companies":
            collected = digest_picker.select_company_fresh(
                collected,
                moment,
                settings.fresh_hours(),
                settings.company_stale_hours(),
            )
        else:
            collected = digest_picker.select_fresh(collected, moment, hours=settings.fresh_hours())
        request = {
            "section_id": spec.id,
            "title": spec.title,
            "purpose": spec.purpose,
            "feed_url": public_urls[0] if public_urls else "",
            "prefer_terms": (),
            "candidates": collected,
        }
        topics = {str(item.get("topic") or "") for item in collected if item.get("topic")}
        # One real story per company, even when shows or bot notes have shortened the other sections.
        if spec.id == "companies" and len(topics) > 1:
            request["item_count"] = min(2, len(topics))
        news_requests.append(request)

    bot_spec = settings.section_by_id("bot_updates")
    bot_dir = bot_directory or settings.bot_updates_dir()
    if bot_spec is not None:
        try:
            bot_items, bot_error = digest_picker.load_bot_updates(bot_dir, digest_date, moment)
        except Exception:
            bot_items, bot_error = None, "bot updates ignored: unreadable"
        if bot_error:
            errors.append(
                {
                    "section": bot_spec.title,
                    "url": "bot-updates",
                    "error": bot_error,
                }
            )
        if bot_items:
            by_id["bot_updates"] = {
                "section": bot_spec.title,
                "source_feed": "bot-updates",
                "items": bot_items,
            }

    history_path = history_file or settings.history_path()
    try:
        history, history_error = digest_picker.load_history(history_path)
    except Exception:
        history, history_error = [], "history ignored: unreadable"
    if history_error:
        errors.append({"section": "HISTORY", "url": "digest-history", "error": history_error})
    profile, profile_error = digest_picker.load_profile(settings.profile_path())
    if profile_error:
        errors.append({"section": "PROFILE", "url": "interests", "error": profile_error})
    shows_section, show_errors, show_records = build_shows_section(digest_date, bounded, history)
    errors.extend(show_errors)
    if shows_section is not None:
        by_id["shows"] = shows_section
    try:
        picked, picker_meta, picker_errors, history_records = digest_picker.pick_news(
            news_requests,
            digest_date=digest_date,
            history=history,
            has_bots="bot_updates" in by_id,
            crowded="shows" in by_id,
            taste=settings.taste(),
            opener=opener,
            deadline=deadline,
            profile=profile,
        )
    except Exception:
        picked, picker_meta, picker_errors, history_records = {}, {"mode": "fallback", "model": None, "reason": "picker_error"}, [], []
        for request in news_requests:
            picked[request["section_id"]] = [
                {
                    "title": "No new update today",
                    "summary": "The picker could not finish, so this section has no story today.",
                    "url": digest_picker.strip_userinfo(request["feed_url"]),
                    "published_at": None,
                    "rationale": "picker error",
                }
            ]
        errors.append({"section": "PICKER", "url": "llm", "error": "llm fallback: picker_error"})
    errors.extend(picker_errors)
    history_records.extend(show_records)
    feeds = {request["section_id"]: request["feed_url"] for request in news_requests}
    for section_id, items in picked.items():
        spec = settings.section_by_id(section_id)
        if spec is None:
            continue
        by_id[section_id] = {
            "section": spec.title,
            "source_feed": digest_picker.strip_userinfo(feeds.get(section_id, "")),
            "items": items,
        }

    if persist_history:
        try:
            saved = digest_picker.save_history(history_path, digest_date, history_records)
            if not saved:
                errors.append({"section": "HISTORY", "url": "digest-history", "error": "history ignored: unreadable"})
        except Exception as exc:
            errors.append({"section": "HISTORY", "url": "digest-history", "error": _failure_name(exc)})
    elif history_out is not None:
        history_out.extend(history_records)

    ordered = [by_id[spec.id] for spec in specs if spec.id in by_id]
    payload = {
        "schema_version": "1.0",
        "digest_date": digest_date,
        "location": LOCATION_LABEL,
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
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    day = args.date or digest_date()
    moment = dt.datetime.now(dt.timezone.utc)
    records: list = []
    try:
        payload = make_payload(day, now=moment, persist_history=False, history_out=records)
    except Exception:
        payload = _emergency_payload(day, moment)
        records = []
    atomic_json(args.out, payload)
    try:
        digest_picker.save_history(settings.history_path(), day, records)
    except Exception:
        pass
    print(json.dumps({"digest_id": payload["digest_id"], "picker": payload.get("picker", {}).get("mode"), "sections": len(payload["sections"]), "source_errors": payload["source_errors"], "output": str(args.out.resolve())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
