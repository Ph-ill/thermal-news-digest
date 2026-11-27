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
import queue
import random
import re
import tempfile
import threading
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import digest_picker
import digest_settings as settings
import receipt_art

# Place defaults are the public city center of Bastrop, Texas. Not a street address.
def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


LOCATION_LABEL = _env("DIGEST_LOCATION_LABEL", "Bastrop, TX")
# Leaves room inside the 120s trigger for render and the CUPS poll.
GENERATOR_BUDGET_SECONDS = 75.0
FETCH_TIMEOUT_CAP = 8.0
# Weather, news, and show lookups share this wall so the model still has about 30s.
FETCH_STAGE_SECONDS = 35.0
FETCH_WORKERS = 8
GOOGLE_CONCURRENCY = 2
TICKETMASTER_CONCURRENCY = 3
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
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
        self.seconds = max(0.0, float(seconds))
        self.deadline = time.monotonic() + self.seconds

    def remaining(self) -> float:
        return self.deadline - time.monotonic()


_budget_var: contextvars.ContextVar[Budget | None] = contextvars.ContextVar("digest_budget", default=None)
_stage_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("digest_stage_deadline", default=None)


def _clock() -> float:
    return time.monotonic()


def _wait_wave(inflight: dict, timeout: float, done: queue.Queue) -> list:
    """Collect finished jobs until the wall passes. Never joins a worker."""
    if timeout <= 0 or not inflight:
        return []
    want = set(inflight)
    finished: list[tuple[str, object]] = []
    deadline = time.monotonic() + timeout
    while want and time.monotonic() < deadline:
        try:
            key, value = done.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            break
        if key in want:
            want.discard(key)
            finished.append((key, value))
    return finished


def _host_bucket(url: str) -> str:
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    if host == "news.google.com" or host.endswith(".news.google.com"):
        return "google"
    if host == "ticketmaster.com" or host.endswith(".ticketmaster.com"):
        return "ticketmaster"
    return ""


_HOST_CAP = {"google": GOOGLE_CONCURRENCY, "ticketmaster": TICKETMASTER_CONCURRENCY}


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


def _fetch_wall() -> float:
    """How long this edition may spend in the fetch stage."""
    budget = _budget_var.get()
    if budget is None:
        return FETCH_STAGE_SECONDS
    return max(0.0, min(FETCH_STAGE_SECONDS, budget.remaining()))


def _retry_allowed() -> bool:
    """One retry fits only when the stage and the generator budget still have a moment."""
    now = _clock()
    budget = _budget_var.get()
    if budget is not None and budget.remaining() < 0.4:
        return False
    stage = _stage_deadline.get()
    if stage is not None and stage - now < 0.4:
        return False
    return True


def _retry_pause() -> None:
    delay = random.uniform(0.05, 0.25)
    now = _clock()
    caps = []
    budget = _budget_var.get()
    if budget is not None:
        caps.append(budget.remaining())
    stage = _stage_deadline.get()
    if stage is not None:
        caps.append(stage - now)
    if caps:
        delay = min(delay, max(0.0, min(caps) - 0.05))
    if delay > 0:
        time.sleep(delay)


def _fetch_once(url: str) -> bytes:
    budget = _budget_var.get()
    timeout = FETCH_TIMEOUT_CAP
    deadline = None
    limits = []
    if budget is not None:
        limits.append(budget.deadline)
    stage = _stage_deadline.get()
    if stage is not None:
        limits.append(min(stage, _clock() + FETCH_TIMEOUT_CAP))
    if limits:
        deadline = min(limits)
        timeout = min(FETCH_TIMEOUT_CAP, max(0.0, deadline - _clock()))
        if timeout < 0.05:
            raise TimeoutError("budget")
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


def fetch(url: str) -> bytes:
    try:
        return _fetch_once(url)
    except urllib.error.HTTPError as exc:
        if exc.code not in _RETRY_STATUS or not _retry_allowed():
            raise
        try:
            exc.close()
        except Exception:
            pass
        _retry_pause()
        return _fetch_once(url)


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
    bodies: dict | None = None,
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
        if bodies is not None:
            raw = bodies.get(url, None)
            if raw is None:
                errors.append({"section": spec.title, "url": TICKETMASTER_EVENTS, "error": "not fetched"})
                continue
            if isinstance(raw, _StageTimeout):
                errors.append({"section": spec.title, "url": TICKETMASTER_EVENTS, "error": "timed out"})
                continue
            if isinstance(raw, BaseException):
                errors.append({"section": spec.title, "url": TICKETMASTER_EVENTS, "error": _failure_name(raw)})
                continue
        else:
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


def _configured_feed(section_id: str, feed_url: str):
    """The settings feed this row claims, or None when the URL is not one of ours."""
    try:
        feeds = settings.feeds_for(section_id)
    except Exception:
        return None
    wanted = digest_picker.strip_userinfo(feed_url).rstrip("/")
    for feed in feeds:
        if digest_picker.strip_userinfo(feed.url).rstrip("/") == wanted:
            return feed
    return None


def _requests_from_candidates(document: dict, specs: list, now: dt.datetime) -> list[dict]:
    """News requests from a fresh export.

    The file is untrusted. Section titles and the rubric come from settings.
    Unknown sections are ignored. Each row is gated like a story just fetched.
    """
    by_title: dict[str, dict] = {}
    for section in document.get("sections") or []:
        if isinstance(section, dict) and isinstance(section.get("section"), str):
            by_title.setdefault(section["section"], section)
    requests: list[dict] = []
    for spec in specs:
        if spec.kind != "news":
            continue
        exported = by_title.get(spec.title) or {}
        collected: list[dict] = []
        first_feed = ""
        try:
            configured = settings.feeds_for(spec.id)
            if configured:
                first_feed = configured[0].url
        except Exception:
            configured = ()
        for row in exported.get("candidates") or []:
            if not isinstance(row, dict):
                continue
            feed_url = row.get("feed_url")
            if not isinstance(feed_url, str):
                continue
            feed = _configured_feed(spec.id, feed_url)
            if feed is None:
                continue
            title = row.get("full_title") if isinstance(row.get("full_title"), str) and row.get("full_title").strip() else row.get("title")
            summary = row.get("full_summary") if isinstance(row.get("full_summary"), str) else row.get("summary")
            item = {
                "title": title or "",
                "url": row.get("url") or "",
                "summary": summary or "",
                "published_at": row.get("published_at"),
                "feed_url": digest_picker.strip_userinfo(feed.url),
            }
            if isinstance(row.get("source_label"), str):
                item["source_label"] = row["source_label"]
            gated = digest_picker.gate_feed_item(item, section_id=spec.id, feed=feed)
            if gated is not None:
                collected.append(gated)
        collected = digest_picker.apply_section_freshness(collected, section_id=spec.id, now=now)
        request = {
            "section_id": spec.id,
            "title": spec.title,
            "purpose": spec.purpose,
            "feed_url": first_feed,
            "prefer_terms": (),
            "candidates": collected,
        }
        topics = {str(item.get("topic") or "") for item in collected if item.get("topic")}
        if spec.id == "companies" and len(topics) > 1:
            request["item_count"] = min(2, len(topics))
        requests.append(request)
    return requests


def supplied_shows_section(
    digest_day: str,
    entries: list[dict],
    history: list[dict],
) -> tuple[dict | None, list[dict], list[dict]]:
    """NEARBY SHOWS from the picks file when Ticketmaster is not configured."""
    spec = settings.section_by_id("shows")
    if spec is None or not entries:
        return None, [], []
    found = digest_picker.shows_from_supplied(entries, digest_day, history, settings.shows_horizon_days())
    if not found:
        return None, [], []
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
    return {"section": spec.title, "source_feed": "picks", "items": items}, [], records


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
    icon_kind = receipt_art.classify_weather(
        short,
        str(day.get("icon") or ""),
        nighttime=not bool(day.get("isDaytime", True)),
    )
    item = {
        "headline": headline,
        "summary": summary,
        "url": forecast_url,
        # The period start is stable across repeated renders. now() would change the digest id.
        "published_at": day.get("startTime"),
        "rationale": "NOAA daytime forecast for the digest date; the overnight period is not used",
    }
    if icon_kind:
        item["weather_icon"] = icon_kind
    return item


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


class _StageTimeout(TimeoutError):
    """A fetch was started and was still running when the stage wall passed."""


def _parallel_map(
    jobs: list[tuple[str, str, Callable[[], object]]],
    wall: float,
) -> tuple[dict[str, object], list[str], list[str]]:
    """Run jobs on daemon threads. The caller never joins them.

    A job that never starts is not fetched. A job still running at the wall is
    timed out. ``wall`` is measured on ``_clock``.
    """
    if wall <= 0 or not jobs:
        return {}, [key for key, _url, _fn in jobs], []
    end = _clock() + wall
    results: dict[str, object] = {}
    not_fetched: list[str] = []
    timed_out: list[str] = []
    pending = list(jobs)
    inflight: dict[str, str] = {}
    sink: queue.Queue = queue.Queue()

    def start(key: str, fn: Callable[[], object]) -> None:
        context = contextvars.copy_context()

        def run() -> None:
            token = _stage_deadline.set(end)
            try:
                try:
                    value = fn()
                except Exception as exc:
                    value = exc
            finally:
                _stage_deadline.reset(token)
            sink.put((key, value))

        threading.Thread(target=context.run, args=(run,), name="digest-fetch", daemon=True).start()

    while pending or inflight:
        while pending and len(inflight) < FETCH_WORKERS and _clock() < end:
            counts: dict[str, int] = {}
            for host in inflight.values():
                if host:
                    counts[host] = counts.get(host, 0) + 1
            chosen = None
            for index, (key, url, _fn) in enumerate(pending):
                host = _host_bucket(url)
                cap = _HOST_CAP.get(host)
                if cap is not None and counts.get(host, 0) >= cap:
                    continue
                chosen = index
                break
            if chosen is None:
                break
            key, url, fn = pending.pop(chosen)
            inflight[key] = _host_bucket(url)
            start(key, fn)
        if not inflight:
            not_fetched.extend(key for key, _url, _fn in pending)
            break
        finished = _wait_wave(inflight, end - _clock(), sink)
        if not finished:
            timed_out.extend(inflight)
            not_fetched.extend(key for key, _url, _fn in pending)
            break
        for key, value in finished:
            inflight.pop(key, None)
            results[key] = value
    return results, not_fetched, timed_out


def _not_fetched_item(feed_url: str) -> dict:
    return {
        "title": "Feed not fetched",
        "summary": "This feed was not requested before the fetch stage ended.",
        "url": digest_picker.strip_userinfo(feed_url) or "https://example.invalid/not-fetched",
        "published_at": None,
        "rationale": "not fetched",
    }


def _timed_out_item(feed_url: str) -> dict:
    return {
        "title": "Feed timed out",
        "summary": "This feed was requested and did not finish before the fetch stage ended.",
        "url": digest_picker.strip_userinfo(feed_url) or "https://example.invalid/timed-out",
        "published_at": None,
        "rationale": "timed out",
    }


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
    fetcher: Callable[[str], bytes] | None = None,
    *,
    opener: Callable | None = None,
    now: dt.datetime | None = None,
    history_file: Path | None = None,
    bot_directory: Path | None = None,
    budget_seconds: float = GENERATOR_BUDGET_SECONDS,
    persist_history: bool = True,
    history_out: list | None = None,
    picks_directory: Path | None = None,
    candidates_directory: Path | None = None,
    export_only: bool = False,
) -> dict:
    if fetcher is None:
        fetcher = fetch
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
            picks_directory=picks_directory,
            candidates_directory=candidates_directory,
            export_only=export_only,
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
    picks_directory: Path | None = None,
    candidates_directory: Path | None = None,
    export_only: bool = False,
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

    jobs: list[tuple[str, Callable[[], object]]] = []
    feed_plan: list[tuple] = []
    saved_candidates = None
    if not export_only:
        candidates_root = candidates_directory or settings.candidates_dir()
        saved_candidates, candidates_error = digest_picker.load_candidates(candidates_root, digest_date, moment)
        if candidates_error:
            errors.append({"section": "PICKER", "url": "candidates", "error": candidates_error})
    weather_spec = settings.section_by_id("weather")
    if weather_spec is not None and not export_only:
        jobs.append(("weather", WEATHER_POINT, lambda: weather(bounded, digest_date)))

    for spec in specs:
        if spec.kind != "news" or saved_candidates is not None:
            continue
        try:
            feeds = settings.feeds_for(spec.id)
        except Exception as exc:
            errors.append({"section": spec.title, "url": "feeds", "error": _failure_name(exc)})
            continue
        for index, feed in enumerate(feeds):
            key = f"feed:{spec.id}:{index}"
            feed_plan.append((key, spec, feed))
            jobs.append((key, feed.url, lambda url=feed.url: bounded(url)))

    show_plan: list[tuple[str, str, str]] = []
    shows_spec = settings.section_by_id("shows")
    artist_names: list[str] = []
    show_window: tuple[str, str] | None = None
    artists_failed = False
    if shows_spec is not None and settings.ticketmaster_key() and not export_only:
        artist_path = settings.artists_path()
        if artist_path is not None and artist_path.is_file():
            try:
                artist_names = load_artists(artist_path)
                show_window = _shows_window(digest_date)
            except Exception:
                artist_names = []
                artists_failed = True
                errors.append({"section": shows_spec.title, "url": "artists", "error": "artists file ignored: unreadable"})
    if show_window is not None:
        window_start, window_end = show_window
        for artist in artist_names:
            url = ticketmaster_events_url(artist, window_start, window_end)
            show_plan.append((f"artist:{artist}", artist, url))
            jobs.append((f"artist:{artist}", url, lambda target=url: bounded(target)))

    fetched, not_fetched, timed_out = _parallel_map(jobs, _fetch_wall())
    missed = set(not_fetched)
    overdue = set(timed_out)

    if weather_spec is not None and not export_only:
        if "weather" in missed:
            errors.append({"section": weather_spec.title, "url": WEATHER_POINT, "error": "not fetched"})
            weather_item = {
                "headline": "Forecast not fetched",
                "summary": "The forecast was not requested before the fetch stage ended.",
                "url": WEATHER_POINT,
                "published_at": None,
                "rationale": "not fetched",
            }
        elif "weather" in overdue:
            errors.append({"section": weather_spec.title, "url": WEATHER_POINT, "error": "timed out"})
            weather_item = {
                "headline": "Forecast timed out",
                "summary": "The forecast was requested and did not finish before the fetch stage ended.",
                "url": WEATHER_POINT,
                "published_at": None,
                "rationale": "timed out",
            }
        else:
            weather_result = fetched.get("weather")
            if isinstance(weather_result, BaseException) or weather_result is None:
                exc = weather_result if isinstance(weather_result, BaseException) else TimeoutError("unavailable")
                errors.append({"section": weather_spec.title, "url": WEATHER_POINT, "error": _failure_name(exc)})
                weather_item = {
                    "headline": "No new update today (weather source unavailable)",
                    "summary": "The configured NOAA forecast could not be reached for this edition.",
                    "url": WEATHER_POINT,
                    "published_at": None,
                    "rationale": f"weather unavailable: {_failure_name(exc)}",
                }
            else:
                weather_item = weather_result
        by_id["weather"] = {
            "section": weather_spec.title,
            "source_feed": WEATHER_POINT,
            "items": [weather_item],
        }

    news_requests: list[dict] = []
    if saved_candidates is not None:
        news_requests = _requests_from_candidates(saved_candidates, specs, moment)
    planned_by_section: dict[str, list] = {}
    for key, spec, feed in feed_plan:
        planned_by_section.setdefault(spec.id, []).append((key, spec, feed))
    for spec in specs:
        if saved_candidates is not None or spec.kind != "news" or spec.id not in planned_by_section:
            continue
        rows = planned_by_section[spec.id]
        collected: list[dict] = []
        public_urls: list[str] = []
        failures = 0
        attempted = 0
        skipped = 0
        overdue_count = 0
        for key, _spec, feed in rows:
            public = digest_picker.strip_userinfo(feed.url)
            public_urls.append(public)
            if key in missed:
                skipped += 1
                errors.append({"section": spec.title, "url": public, "error": "not fetched"})
                continue
            if key in overdue:
                overdue_count += 1
                errors.append({"section": spec.title, "url": public, "error": "timed out"})
                continue
            attempted += 1
            result = fetched.get(key)
            if isinstance(result, BaseException) or result is None:
                failures += 1
                exc = result if isinstance(result, BaseException) else TimeoutError("unavailable")
                errors.append({"section": spec.title, "url": public, "error": _failure_name(exc)})
                continue
            try:
                raw_items = rss_items(result)
            except Exception as exc:
                failures += 1
                errors.append({"section": spec.title, "url": public, "error": _failure_name(exc)})
                continue
            if not raw_items:
                errors.append({"section": spec.title, "url": public, "error": "fetched, no story"})
            for raw in raw_items:
                item = dict(raw)
                item["feed_url"] = public
                gated = digest_picker.gate_feed_item(item, section_id=spec.id, feed=feed)
                if gated is None:
                    continue
                collected.append(gated)
        if rows and skipped + overdue_count == len(rows) and not collected:
            first = public_urls[0]
            item = _timed_out_item(first) if overdue_count else _not_fetched_item(first)
            by_id[spec.id] = {
                "section": spec.title,
                "source_feed": first,
                "items": [item],
            }
            continue
        if rows and attempted and failures == attempted and not collected:
            first = public_urls[0]
            by_id[spec.id] = {
                "section": spec.title,
                "source_feed": first,
                "items": [_unavailable_news_item(first, TimeoutError("unavailable"))],
            }
            continue
        collected = digest_picker.apply_section_freshness(collected, section_id=spec.id, now=moment)
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
    if export_only:
        return digest_picker.candidate_document(
            news_requests,
            history,
            digest_date,
            profile=profile,
            taste=settings.taste(),
            has_bots="bot_updates" in by_id,
            crowded=False,
        )
    picks_root = picks_directory or settings.picks_dir()
    loaded_picks, picks_error = digest_picker.load_picks(picks_root, digest_date)
    if picks_error:
        errors.append({"section": "PICKER", "url": "picks", "error": picks_error})
    external_picks = None
    file_shows: list[dict] = []
    if loaded_picks is not None:
        allowed = {request["title"] for request in news_requests}
        if any(pick["section"] not in allowed for pick in loaded_picks["picks"]):
            errors.append({"section": "PICKER", "url": "picks", "error": "picks ignored: invalid"})
        else:
            external_picks = loaded_picks["picks"]
            file_shows = loaded_picks["shows"]
    if artists_failed:
        shows_section, show_errors, show_records = None, [], []
    elif show_plan:
        bodies = {}
        for key, _artist, url in show_plan:
            if key in missed:
                bodies[url] = None
            elif key in overdue:
                bodies[url] = _StageTimeout("timed out")
            else:
                bodies[url] = fetched.get(key)
        shows_section, show_errors, show_records = build_shows_section(
            digest_date, bounded, history, bodies=bodies
        )
    elif settings.ticketmaster_key():
        shows_section, show_errors, show_records = build_shows_section(digest_date, bounded, history)
    else:
        shows_section, show_errors, show_records = supplied_shows_section(digest_date, file_shows, history)
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
            external_picks=external_picks,
        )
    except Exception:
        picked, picker_meta, picker_errors, history_records = {}, {"mode": "fallback", "model": None, "reason": "picker_error", "source": "fallback"}, [], []
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


def _item_heading(item: dict) -> str:
    return str(item.get("title") or item.get("headline") or "")


def _item_is_offline(title: str) -> bool:
    return title == digest_picker.LOCAL_OFFLINE_TITLE


def _item_is_fallback(item: dict, title: str) -> bool:
    if title in {
        digest_picker.LOCAL_OFFLINE_TITLE,
        digest_picker.LOCAL_EMPTY_TITLE,
        digest_picker.PLACEHOLDER_TITLE,
    }:
        return True
    return str(item.get("rationale") or "").startswith("Fallback pick")


def check_report(payload: dict) -> tuple[dict, int]:
    """JSON summary of a payload built the way the 05:00 run would build it.

    Exit 1 when any section uses the offline line or the picks file was ignored.
    """
    sections: list[dict] = []
    offline_used = False
    fallback_used = False
    bot_updates = 0
    for section in payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        items = [item for item in (section.get("items") or []) if isinstance(item, dict)]
        titles = [_item_heading(item) for item in items]
        section_offline = any(_item_is_offline(title) for title in titles)
        section_fallback = any(_item_is_fallback(item, title) for item, title in zip(items, titles))
        offline_used = offline_used or section_offline
        fallback_used = fallback_used or section_fallback
        if section.get("source_feed") == "bot-updates":
            bot_updates += len(items)
        sections.append(
            {
                "section": section.get("section"),
                "count": len(items),
                "titles": titles,
                "fallback": section_fallback,
                "offline": section_offline,
            }
        )
    picks_ignored = any(
        isinstance(row, dict) and str(row.get("error") or "").startswith("picks ignored")
        for row in (payload.get("source_errors") or [])
    )
    picker = payload.get("picker") if isinstance(payload.get("picker"), dict) else {}
    summary = {
        "digest_date": payload.get("digest_date"),
        "picker": {
            "mode": picker.get("mode"),
            "reason": picker.get("reason"),
            "source": picker.get("source"),
        },
        "sections": sections,
        "fallback_used": fallback_used,
        "offline_used": offline_used,
        "bot_updates": bot_updates,
        "picks_ignored": picks_ignored,
    }
    code = 1 if offline_used or picks_ignored else 0
    return summary, code


def _run_check(day: str, moment: dt.datetime) -> int:
    """Build today's payload and print a summary. Write nothing."""
    try:
        payload = make_payload(day, now=moment, persist_history=False)
    except Exception:
        payload = _emergency_payload(day, moment)
    summary, code = check_report(payload)
    print(json.dumps(summary, sort_keys=True))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--export-candidates", nargs="?", const="", default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    day = args.date or digest_date()
    moment = dt.datetime.now(dt.timezone.utc)
    if args.check and (args.export_candidates is not None or args.out is not None):
        parser.error("--check writes nothing and cannot be combined with --out or --export-candidates")
    if args.check:
        return _run_check(day, moment)
    if args.export_candidates is not None:
        destination = settings.candidates_path(day) if args.export_candidates == "" else Path(args.export_candidates)
        document = make_payload(day, now=moment, persist_history=False, export_only=True)
        atomic_json(destination, document)
        count = sum(len(section.get("candidates") or []) for section in document.get("sections") or [])
        print(
            json.dumps(
                {
                    "digest_date": document.get("digest_date"),
                    "sections": len(document.get("sections") or []),
                    "candidates": count,
                    "output": str(destination.resolve()),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.out is None:
        parser.error("--out is required unless --export-candidates is set")
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
    print(json.dumps({"digest_id": payload["digest_id"], "picker": payload.get("picker", {}).get("mode"), "source": payload.get("picker", {}).get("source"), "sections": len(payload["sections"]), "source_errors": payload["source_errors"], "output": str(args.out.resolve())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
