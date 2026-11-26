#!/usr/bin/env python3
"""Picker, weather-period, history, and bot-update tests. No live network."""

import datetime as dt
from email.message import Message
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest import mock
from zoneinfo import ZoneInfo

for _name in list(os.environ):
    if _name.startswith("DIGEST_SECTION") or _name in {
        "DIGEST_LLM_API_KEY",
        "DIGEST_READER_IDS",
        "DIGEST_SECTION_ORDER",
        "DIGEST_BOT_UPDATES_DIR",
        "DIGEST_HISTORY_PATH",
        "DIGEST_LLM_BASE_URL",
        "DIGEST_LLM_MODEL",
    }:
        os.environ.pop(_name, None)
os.environ["DIGEST_LLM_API_KEY"] = ""
os.environ.update(
    {
        "DIGEST_LOCATION_LABEL": "near Springfield",
        "DIGEST_NEIGHBORHOOD_EVENTS_URL": "https://maplegrove.example.org/events/",
        "DIGEST_NEIGHBORHOOD_NAME": "Maple Grove",
        "DIGEST_NEIGHBORHOOD_MARKERS": "maple grove,100 main street",
        "DIGEST_CHAMBER_EVENTS_URL": "https://business.chamber.example.com/eventcalendar",
        "DIGEST_LOCAL_CITY": "Springfield",
        "DIGEST_TZ": "America/Chicago",
    }
)

import digest_generator as dg
import digest_picker as dp
import digest_settings as settings


LONG = (
    "The board voted after residents asked for weekend hours and a quieter room for students. "
    "Work starts next month if the weather holds."
)
SECRET = "sk-test-secret-value"


class FakeResponse:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0

    def read(self, limit=-1):
        if limit is None or limit < 0:
            limit = len(self._data) - self._offset
        chunk = self._data[self._offset : self._offset + limit]
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def item(title, url, summary, published="Thu, 26 Nov 2026 08:00:00 GMT"):
    return {"title": title, "url": url, "summary": summary, "published_at": published}


class PickerTests(unittest.TestCase):
    def test_fallback_drops_obituaries_spam_and_title_echoes(self):
        seen = dp.Seen([], "2026-11-26")
        candidates = [
            item(
                "Local mayor dies at 88 - Daily Ledger",
                "https://news.google.com/rss/articles/obit",
                "Local mayor dies at 88 Daily Ledger",
                "Thu, 26 Nov 2026 10:00:00 GMT",
            ),
            item(
                "ASTS stock last chance to buy before the bell - Tip Sheet",
                "https://news.google.com/rss/articles/spam",
                "ASTS stock last chance to buy before the bell Tip Sheet",
                "Thu, 26 Nov 2026 09:00:00 GMT",
            ),
            item(
                "City council adopts the annual budget - Springfield Ledger",
                "https://news.google.com/rss/articles/echo",
                "<a>City council adopts the annual budget</a> Springfield Ledger",
                "Thu, 26 Nov 2026 08:00:00 GMT",
            ),
            item(
                "Library board funds a new reading room - Springfield Ledger",
                "https://news.google.com/rss/articles/real",
                LONG,
                "Thu, 26 Nov 2026 07:00:00 GMT",
            ),
            item(
                "Rescuers reach the crew after the ship dies at sea",
                "https://example.com/rescue",
                "Rescuers reached the crew after the ship lost power at sea and towed it into harbor before dawn.",
                "Wed, 25 Nov 2026 12:00:00 GMT",
            ),
        ]
        # The HTML is cleaned by the generator; the picker sees already-cleaned text.
        candidates[2]["summary"] = "City council adopts the annual budget Springfield Ledger"
        chosen = dp.fallback_pick(candidates, 2, seen, max_chars=300, feed_url="https://example.com/feed")
        self.assertEqual(chosen[0]["title"], "City council adopts the annual budget")
        self.assertNotIn(chosen[0]["title"].casefold(), chosen[0]["summary"].casefold())
        self.assertIn("Springfield Ledger", chosen[0]["summary"])
        self.assertEqual(chosen[1]["title"], "Library board funds a new reading room")
        self.assertIn("weekend hours", chosen[1]["summary"])
        blob = json.dumps(chosen).casefold()
        self.assertNotIn("dies at 88", blob)
        self.assertNotIn("last chance", blob)
        self.assertTrue(dp.is_spam("ASTS stock last chance to buy before the bell"))
        self.assertFalse(dp.is_spam("last chance to see the eclipse from the river"))
        self.assertFalse(dp.is_obituary(candidates[4]["title"]))
        self.assertTrue(dp.is_obituary("Mayor dies in a crash"))
        self.assertTrue(dp.is_obituary("Death of a long-serving clerk shocks the town"))
        self.assertTrue(dp.is_obituary("Driver killed in a downtown collision"))
        self.assertFalse(dp.is_obituary("Senate killed the bill"))
        self.assertTrue(dp.is_obituary("The clerk was killed overnight"))
        self.assertTrue(dp.is_obituary("A guard killed by an intruder"))
        kept = dp.fallback_pick(
            [
                item(
                    "Senate killed the bill",
                    "https://example.com/bill",
                    "Lawmakers voted the package down before the recess and the sponsor said the chamber will try again next month.",
                    "Thu, 26 Nov 2026 08:00:00 GMT",
                )
            ],
            1,
            dp.Seen([], "2026-11-26"),
            max_chars=300,
            feed_url="https://example.com/feed",
        )
        self.assertEqual(kept[0]["title"], "Senate killed the bill")
        self.assertTrue(dp.is_spam("Stocks I'm buying this week"))
        self.assertTrue(dp.is_spam("Stocks to watch before the open"))

    def test_film_terms_are_preferred_but_no_longer_required(self):
        seen = dp.Seen([], "2026-11-26")
        candidates = [
            item(
                "Studio announces a sequel",
                "https://www.hollywoodreporter.com/sequel",
                "The studio announced a sequel and named the director, the city, and the month cameras roll.",
                "Thu, 26 Nov 2026 10:00:00 GMT",
            ),
            item(
                "New anime film opens in the spring",
                "https://www.hollywoodreporter.com/anime",
                "The anime film follows a courier across a flooded city and opens in the spring.",
                "Thu, 26 Nov 2026 07:00:00 GMT",
            ),
        ]
        chosen = dp.fallback_pick(
            candidates,
            2,
            seen,
            prefer_terms=("anime", "western"),
            max_chars=300,
            feed_url="https://www.hollywoodreporter.com/feed",
        )
        self.assertEqual(
            [entry["title"] for entry in chosen],
            ["New anime film opens in the spring", "Studio announces a sequel"],
        )

    def test_history_skips_recent_urls_and_titles_but_not_today(self):
        repeated = "City council adopts the annual budget"
        history = [
            {
                "digest_date": "2026-11-25",
                "section": "local",
                "url": dp.normalize_url("https://example.com/story?utm_source=rss"),
                "title": dp.normalize_title(repeated),
            },
            {
                "digest_date": "2026-11-18",
                "section": "local",
                "url": dp.normalize_url("https://example.com/old"),
                "title": dp.normalize_title("River ferry resumes weekend service"),
            },
            {
                "digest_date": "2026-11-26",
                "section": "local",
                "url": dp.normalize_url("https://example.com/today"),
                "title": dp.normalize_title("Park tax stays flat for the coming year"),
            },
        ]
        seen = dp.Seen(history, "2026-11-26", keep_days=7)
        candidates = [
            item(repeated, "https://www.example.com/story?utm_campaign=1", LONG),
            item(
                "River ferry resumes weekend service",
                "https://example.com/ferry-new",
                "The ferry will run on Saturdays again after crews finished the dock repairs and posted the timetable.",
            ),
            item(
                "Park tax stays flat for the coming year",
                "https://example.com/today",
                "The council left the park tax unchanged and published the rate card for the coming year this morning.",
            ),
        ]
        chosen = dp.fallback_pick(candidates, 2, seen, max_chars=300, feed_url="https://example.com/feed")
        titles = [entry["title"] for entry in chosen]
        self.assertNotIn(repeated, titles)
        self.assertIn("River ferry resumes weekend service", titles)
        self.assertIn("Park tax stays flat for the coming year", titles)

    def test_history_round_trip_and_cross_section_dedupe(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "digest-history.json"
            records = [
                {
                    "digest_date": "2026-11-25",
                    "section": "global",
                    "url": dp.normalize_url("https://example.com/shared"),
                    "title": dp.normalize_title("Harbor talks end with a written ceasefire"),
                }
            ]
            dp.save_history(path, "2026-11-25", records, keep_days=7)
            loaded, error = dp.load_history(path)
            self.assertIsNone(error)
            self.assertEqual(loaded, records)
            requests = [
                {
                    "section_id": "global",
                    "title": "GLOBAL",
                    "purpose": "world",
                    "feed_url": "https://example.com/global",
                    "prefer_terms": (),
                    "candidates": [
                        item(
                            "Harbor talks end with a written ceasefire",
                            "https://example.com/shared",
                            "Mediators said both sides signed the text before dawn and set Friday for the first aid ship.",
                        )
                    ],
                },
                {
                    "section_id": "uk",
                    "title": "UK",
                    "purpose": "uk",
                    "feed_url": "https://example.com/uk",
                    "prefer_terms": (),
                    "candidates": [
                        item(
                            "Harbor talks end with a written ceasefire",
                            "https://example.com/shared?utm_source=uk",
                            "Mediators said both sides signed the text before dawn and set Friday for the first aid ship.",
                        ),
                        item(
                            "Rail board publishes the weekend map",
                            "https://example.com/rail",
                            "Engineers reopened the main line after replacing the cracked span and posted a shorter weekend map.",
                        ),
                    ],
                },
            ]
            picked, meta, errors, new_records = dp.pick_news(
                requests,
                digest_date="2026-11-26",
                history=loaded,
                has_bots=False,
                taste="fixture",
                item_count=1,
                summary_chars=300,
            )
            self.assertEqual(errors, [])
            self.assertEqual(meta["mode"], "fallback")
            self.assertIn("No current item passed", picked["global"][0]["summary"])
            self.assertEqual(picked["uk"][0]["title"], "Rail board publishes the weekend map")
            self.assertEqual(len(new_records), 1)
            dp.save_history(path, "2026-11-26", new_records, keep_days=7)
            stored, _error = dp.load_history(path)
            self.assertEqual({row["digest_date"] for row in stored}, {"2026-11-25", "2026-11-26"})

    def test_llm_json_is_validated_and_failures_fall_back(self):
        requests = [
            {
                "section_id": "global",
                "title": "GLOBAL",
                "purpose": "World news that will still matter by tonight.",
                "feed_url": "https://example.com/global",
                "prefer_terms": (),
                "candidates": [
                    item(
                        "Local mayor dies at 88",
                        "https://example.com/obit",
                        "Local mayor dies at 88 after a short illness at home.",
                        "Thu, 26 Nov 2026 10:00:00 GMT",
                    ),
                    item(
                        "Harbor talks end with a written ceasefire",
                        "https://example.com/harbor",
                        "Mediators said both sides signed the text before dawn and set Friday for the first aid ship.",
                        "Thu, 26 Nov 2026 09:00:00 GMT",
                    ),
                ],
            }
        ]
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            body = json.loads(request.data.decode())
            user = json.loads(body["messages"][1]["content"])
            self.assertNotIn(SECRET, request.full_url)
            self.assertNotIn(SECRET, request.data.decode())
            self.assertEqual(request.get_header("Authorization"), f"Bearer {SECRET}")
            self.assertTrue(request.full_url.endswith("/chat/completions"))
            self.assertEqual(body["model"], "grok-4.7")
            self.assertEqual(body["reasoning_effort"], "low")
            self.assertEqual(user["taste"], "fixture taste")
            self.assertNotIn("https://example.com/obit", request.data.decode())
            picks = {
                "picks": [
                    {
                        "section": "GLOBAL",
                        "items": [
                            {
                                "url": "https://example.com/harbor",
                                "summary": (
                                    "Mediators finished a written ceasefire before dawn and named Friday for the first aid ship. "
                                    "The port has been shut for nine days."
                                ),
                            }
                        ],
                    }
                ]
            }
            payload = {"choices": [{"message": {"content": json.dumps(picks)}}]}
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch.dict(os.environ, {"DIGEST_LLM_API_KEY": SECRET}):
            picked, meta, errors, _records = dp.pick_news(
                requests,
                digest_date="2026-11-26",
                history=[],
                has_bots=True,
                taste="fixture taste",
                opener=opener,
                item_count=1,
                summary_chars=220,
            )
        self.assertEqual(errors, [])
        self.assertEqual(meta["mode"], "llm")
        self.assertEqual(meta["model"], "grok-4.7")
        self.assertIn("written ceasefire", picked["global"][0]["summary"])
        self.assertEqual(picked["global"][0]["title"], "Harbor talks end with a written ceasefire")
        self.assertEqual(calls[0][1], 45)

        def bad_opener(request, timeout):
            payload = {"choices": [{"message": {"content": "sure, here is {not json"}}]}
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch.dict(os.environ, {"DIGEST_LLM_API_KEY": SECRET}):
            picked, meta, errors, _records = dp.pick_news(
                requests,
                digest_date="2026-11-26",
                history=[],
                has_bots=False,
                opener=bad_opener,
                item_count=1,
                summary_chars=300,
            )
        self.assertEqual(meta["mode"], "fallback")
        self.assertEqual(meta["reason"], "bad_json")
        self.assertEqual(errors[0]["error"], "llm fallback: bad_json")
        self.assertNotIn(SECRET, json.dumps(errors))
        self.assertEqual(picked["global"][0]["title"], "Harbor talks end with a written ceasefire")

        def extra_key(request, timeout):
            content = json.dumps({"picks": [], "note": "extra"})
            payload = {"choices": [{"message": {"content": content}}]}
            return FakeResponse(json.dumps(payload).encode())

        with mock.patch.dict(os.environ, {"DIGEST_LLM_API_KEY": SECRET}):
            _picked, meta, errors, _records = dp.pick_news(
                requests,
                digest_date="2026-11-26",
                history=[],
                has_bots=False,
                opener=extra_key,
                item_count=1,
                summary_chars=300,
            )
        self.assertEqual(meta["reason"], "bad_shape")
        self.assertIn("bad_shape", errors[0]["error"])

        def timed_out(request, timeout):
            raise TimeoutError("slow")

        with mock.patch.dict(os.environ, {"DIGEST_LLM_API_KEY": SECRET}):
            timed, meta, errors, _records = dp.pick_news(
                requests,
                digest_date="2026-11-26",
                history=[],
                has_bots=False,
                opener=timed_out,
                item_count=1,
                summary_chars=300,
            )
        self.assertEqual(meta["reason"], "timeout")
        self.assertTrue(timed["global"])

        called = {"count": 0}

        def should_not_run(request, timeout):
            called["count"] += 1
            raise AssertionError("network")

        picked, meta, errors, _records = dp.pick_news(
            requests,
            digest_date="2026-11-26",
            history=[],
            has_bots=False,
            opener=should_not_run,
            item_count=1,
            summary_chars=300,
        )
        self.assertEqual(called["count"], 0)
        self.assertEqual(meta["reason"], "no_api_key")
        self.assertEqual(errors, [])
        self.assertTrue(picked["global"])

    def test_fenced_json_is_accepted_and_http_400_retries_without_optional_fields(self):
        fenced = """```json
{"picks":[{"section":"GLOBAL","items":[{"url":"https://example.com/harbor","summary":"Mediators finished a written ceasefire before dawn and named Friday for the first aid ship."}]}]}
```"""
        parsed = dp.validate_picks(dp.parse_llm_content(fenced), {"GLOBAL"})
        self.assertEqual(parsed[0]["items"][0]["url"], "https://example.com/harbor")
        with self.assertRaises(dp.LLMError):
            dp.parse_llm_content("Here you go: {\"picks\": []}")

        bodies = []

        def opener(request, timeout):
            body = json.loads(request.data.decode())
            bodies.append(body)
            if len(bodies) == 1:
                raise urllib.error.HTTPError(request.full_url, 400, "bad", Message(), None)
            content = json.dumps(
                {
                    "picks": [
                        {
                            "section": "GLOBAL",
                            "items": [
                                {
                                    "url": "https://example.com/harbor",
                                    "summary": "Mediators finished a written ceasefire before dawn and named Friday for the first aid ship.",
                                }
                            ],
                        }
                    ]
                }
            )
            payload = {"choices": [{"message": {"content": [{"type": "text", "text": content}]}}]}
            return FakeResponse(json.dumps(payload).encode())

        requests = [
            {
                "section_id": "global",
                "title": "GLOBAL",
                "purpose": "world",
                "feed_url": "https://example.com/global",
                "prefer_terms": (),
                "candidates": [
                    item(
                        "Harbor talks end with a written ceasefire",
                        "https://example.com/harbor",
                        "Mediators said both sides signed the text before dawn and set Friday for the first aid ship.",
                    )
                ],
            }
        ]
        with mock.patch.dict(os.environ, {"DIGEST_LLM_API_KEY": SECRET}):
            picked, meta, errors, _records = dp.pick_news(
                requests,
                digest_date="2026-11-26",
                history=[],
                has_bots=True,
                opener=opener,
                item_count=1,
                summary_chars=220,
            )
        self.assertEqual(errors, [])
        self.assertEqual(meta["mode"], "llm")
        self.assertIn("reasoning_effort", bodies[0])
        self.assertNotIn("reasoning_effort", bodies[1])
        self.assertNotIn("temperature", bodies[1])
        self.assertIn("ceasefire", picked["global"][0]["summary"])

    def test_sentence_fit_keeps_two_sentences_and_us_abbreviation(self):
        title = "Budget vote"
        summary = "U.S. officials approved the budget today. The vote was close. A third sentence should be dropped from the receipt."
        fitted = dp.fit_summary(title, summary, 300)
        self.assertEqual(
            fitted,
            "U.S. officials approved the budget today. The vote was close.",
        )
        self.assertIsNone(dp.fit_summary(title, title + " The Times", 200))


class WeatherTests(unittest.TestCase):
    def periods(self):
        return [
            {
                "name": "Overnight",
                "isDaytime": False,
                "startTime": "2026-11-25T22:00:00+00:00",
                "temperature": 31,
                "temperatureUnit": "F",
                "shortForecast": "Clear",
                "detailedForecast": "OVERNIGHT-ONLY cold and clear.",
            },
            {
                "name": "Thursday",
                "isDaytime": True,
                "startTime": "2026-11-26T12:00:00+00:00",
                "temperature": 70,
                "temperatureUnit": "F",
                "shortForecast": "Sunny",
                "detailedForecast": "Sunny and mild during the day with a light breeze.",
                "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 40},
            },
            {
                "name": "Thursday Night",
                "isDaytime": False,
                "startTime": "2026-11-26T23:00:00+00:00",
                "temperature": 55,
                "temperatureUnit": "F",
                "shortForecast": "Clear",
                "detailedForecast": "Clear and cooler overnight.",
            },
        ]

    def test_daytime_period_skips_overnight_and_includes_high_low_and_precip(self):
        day, night = dg.select_daytime_period(self.periods(), "2026-11-26", ZoneInfo("UTC"))
        item = dg.format_weather_item(day, night, "https://api.weather.gov/gridpoints/EXAMPLE/1,1/forecast")
        self.assertIn("high 70°F", item["headline"])
        self.assertIn("low 55°F", item["headline"])
        self.assertIn("40% chance of precipitation", item["headline"])
        self.assertNotIn("31", item["headline"])
        self.assertNotIn("OVERNIGHT-ONLY", item["summary"])
        self.assertIn("Sunny and mild", item["summary"])

    def test_missing_daytime_period_does_not_use_overnight(self):
        periods = [
            self.periods()[0],
            {
                "name": "Friday",
                "isDaytime": True,
                "startTime": "2026-11-27T12:00:00+00:00",
                "temperature": 72,
                "temperatureUnit": "F",
                "shortForecast": "Sunny",
                "detailedForecast": "Tomorrow only.",
            },
        ]
        with self.assertRaises(LookupError):
            dg.select_daytime_period(periods, "2026-11-26", ZoneInfo("UTC"))
        forecast_url = "https://api.weather.gov/gridpoints/EXAMPLE/1,1/forecast"

        def fetcher(url):
            if url == dg.WEATHER_POINT:
                return json.dumps({"properties": {"forecast": forecast_url}}).encode()
            if url == forecast_url:
                return json.dumps({"properties": {"periods": periods}}).encode()
            raise AssertionError(url)

        item = dg.weather(fetcher, "2026-11-26")
        self.assertIn("No daytime forecast", item["headline"])
        self.assertNotIn("31", item["headline"])
        self.assertNotIn("OVERNIGHT-ONLY", item["summary"])
        self.assertIn("overnight period was not used", item["summary"])


def bot_document(generated_at="2026-11-26T04:30:00-06:00", updates=None):
    if updates is None:
        updates = [
            {
                "bot": "Popcorn",
                "topic": "Film picks",
                "text": "A quiet anime about a night courier opens Friday, and the review is worth the two hours.",
                "for": "both",
            }
        ]
    return {"generated_at": generated_at, "updates": updates}


class BotUpdateTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 11, 26, 11, 0, tzinfo=dt.timezone.utc)

    def test_valid_stale_future_and_invalid_documents(self):
        items = dp.validate_bot_document(bot_document(), now=self.now)
        self.assertEqual(items[0]["title"], "Popcorn: Film picks")
        self.assertEqual(items[0]["source_label"], "Popcorn")
        self.assertNotIn("url", items[0])

        named = bot_document(
            updates=[
                {
                    "bot": "Listener",
                    "topic": "Morning queue",
                    "text": "Three new songs from the usual corner of the shelf, none of them loud.",
                    "for": "reader-a",
                    "url": "https://example.com/listener",
                }
            ]
        )
        named_items = dp.validate_bot_document(named, now=self.now)
        self.assertEqual(named_items[0]["title"], "Listener: Morning queue (reader-a)")
        self.assertEqual(named_items[0]["url"], "https://example.com/listener")

        recent = (self.now - dt.timedelta(hours=29)).isoformat()
        self.assertTrue(dp.validate_bot_document(bot_document(generated_at=recent), now=self.now))
        with self.assertRaises(dp.BotUpdatesError) as stale:
            dp.validate_bot_document(bot_document(generated_at="2026-11-24T04:00:00Z"), now=self.now)
        self.assertEqual(stale.exception.code, "stale")
        soon = (self.now + dt.timedelta(minutes=30)).isoformat()
        self.assertTrue(dp.validate_bot_document(bot_document(generated_at=soon), now=self.now))
        with self.assertRaises(dp.BotUpdatesError) as future:
            later = (self.now + dt.timedelta(hours=3)).isoformat()
            dp.validate_bot_document(bot_document(generated_at=later), now=self.now)
        self.assertEqual(future.exception.code, "future")

        invalids = [
            {**bot_document(), "note": "extra"},
            {"generated_at": "2026-11-26T04:30:00-06:00"},
            {"generated_at": "2026-11-26T04:30:00", "updates": []},
            {"generated_at": "2026-11-26T04:30:00-06:00", "updates": {}},
            bot_document(updates=[{"bot": "Popcorn", "topic": "Film picks", "text": "No", "for": "both"}]),
            bot_document(
                updates=[
                    {
                        "bot": "Popcorn",
                        "topic": "Film picks",
                        "text": "A quiet anime about a night courier opens Friday.",
                        "for": "everyone",
                    }
                ]
            ),
            bot_document(
                updates=[
                    {
                        "bot": "Popcorn",
                        "topic": "Film picks",
                        "text": "Line one.\nLine two is not allowed on the receipt.",
                        "for": "both",
                    }
                ]
            ),
            bot_document(
                updates=[
                    {
                        "bot": "Popcorn",
                        "topic": "Film picks",
                        "text": "A quiet anime about a night courier opens Friday.",
                        "for": "both",
                        "url": "http://example.com/insecure",
                    }
                ]
            ),
        ]
        for document in invalids:
            with self.assertRaises(dp.BotUpdatesError):
                dp.validate_bot_document(document, now=self.now)
        self.assertEqual(dp.validate_bot_document(bot_document(updates=[]), now=self.now), [])

    def test_loader_ignores_missing_stale_and_invalid_files(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            items, error = dp.load_bot_updates(directory, "2026-11-26", self.now)
            self.assertIsNone(items)
            self.assertIsNone(error)
            (directory / "2026-11-25.json").write_text(json.dumps(bot_document()), encoding="utf-8")
            items, error = dp.load_bot_updates(directory, "2026-11-26", self.now)
            self.assertIsNone(items)
            self.assertIsNone(error)
            (directory / "2026-11-26.json").write_text("{", encoding="utf-8")
            items, error = dp.load_bot_updates(directory, "2026-11-26", self.now)
            self.assertIsNone(items)
            self.assertIn("invalid JSON", error)
            (directory / "2026-11-26.json").write_text(
                json.dumps(bot_document(generated_at="2026-11-24T04:00:00Z")),
                encoding="utf-8",
            )
            items, error = dp.load_bot_updates(directory, "2026-11-26", self.now)
            self.assertIsNone(items)
            self.assertIn("stale", error)
            (directory / "2026-11-26.json").write_text(json.dumps(bot_document()), encoding="utf-8")
            items, error = dp.load_bot_updates(directory, "2026-11-26", self.now)
            self.assertIsNone(error)
            self.assertEqual(items[0]["source_label"], "Popcorn")
            (directory / "2026-11-26.json").write_text(json.dumps(bot_document(updates=[])), encoding="utf-8")
            items, error = dp.load_bot_updates(directory, "2026-11-26", self.now)
            self.assertIsNone(items)
            self.assertIsNone(error)


def rss(items):
    body = ["<?xml version='1.0'?><rss version='2.0'><channel>"]
    for entry in items:
        body.append(
            "<item>"
            f"<title>{entry['title']}</title>"
            f"<link>{entry['url']}</link>"
            f"<pubDate>{entry['date']}</pubDate>"
            f"<description>{entry['summary']}</description>"
            "</item>"
        )
    body.append("</channel></rss>")
    return "".join(body).encode()


class GeneratorTests(unittest.TestCase):
    def test_make_payload_uses_daytime_weather_fallback_picker_and_history(self):
        forecast_url = "https://api.weather.gov/gridpoints/EXAMPLE/1,1/forecast"
        periods = [
            {
                "name": "Overnight",
                "isDaytime": False,
                "startTime": "2026-11-26T04:00:00-06:00",
                "temperature": 31,
                "temperatureUnit": "F",
                "shortForecast": "Clear",
                "detailedForecast": "OVERNIGHT-ONLY cold.",
            },
            {
                "name": "Thursday",
                "isDaytime": True,
                "startTime": "2026-11-26T06:00:00-06:00",
                "temperature": 64,
                "temperatureUnit": "F",
                "shortForecast": "Partly cloudy",
                "detailedForecast": "Partly cloudy during the day, with a light breeze and a small shower chance late.",
                "probabilityOfPrecipitation": {"value": 20},
            },
            {
                "name": "Thursday Night",
                "isDaytime": False,
                "startTime": "2026-11-26T18:00:00-06:00",
                "temperature": 41,
                "temperatureUnit": "F",
                "shortForecast": "Clear",
                "detailedForecast": "Clear and colder after sunset.",
            },
        ]
        shared = "https://example.com/shared-story"
        feeds = {}
        for spec in settings.sections():
            if spec.kind != "news":
                continue
            url, _terms = settings.news_feed(spec.id)
            if spec.id == "film":
                entries = [
                    {
                        "title": "Studio announces a sequel",
                        "url": "https://www.hollywoodreporter.com/sequel",
                        "date": "Thu, 26 Nov 2026 10:00:00 GMT",
                        "summary": "The studio announced a sequel and named the director, the city, and the month cameras roll.",
                    },
                    {
                        "title": "A restored western screens all weekend",
                        "url": "https://www.hollywoodreporter.com/western",
                        "date": "Thu, 26 Nov 2026 08:00:00 GMT",
                        "summary": "The western screens all weekend at the small downtown house, with one show each afternoon.",
                    },
                ]
            elif spec.id == "markets":
                entries = [
                    {
                        "title": "ASTS stock last chance to buy before the open",
                        "url": "https://news.google.com/rss/articles/spam",
                        "date": "Thu, 26 Nov 2026 10:00:00 GMT",
                        "summary": "ASTS stock last chance to buy before the open Tip Sheet",
                    },
                    {
                        "title": "Launch firm wins a civil science contract",
                        "url": "https://example.com/contract",
                        "date": "Thu, 26 Nov 2026 08:00:00 GMT",
                        "summary": "The firm won a civil contract to fly two science missions next spring, including ground support.",
                    },
                ]
            elif spec.id == "global":
                entries = [
                    {
                        "title": "Editor dies at 90",
                        "url": "https://example.com/obit",
                        "date": "Thu, 26 Nov 2026 11:00:00 GMT",
                        "summary": "An obituary for an editor who dies at 90.",
                    },
                    {
                        "title": "Harbor talks end with a written ceasefire",
                        "url": shared,
                        "date": "Thu, 26 Nov 2026 09:00:00 GMT",
                        "summary": "Mediators said both sides signed the text before dawn and set Friday for the first aid ship.",
                    },
                ]
            elif spec.id == "uk":
                entries = [
                    {
                        "title": "Harbor talks end with a written ceasefire",
                        "url": shared + "?utm_source=uk",
                        "date": "Thu, 26 Nov 2026 09:30:00 GMT",
                        "summary": "Mediators said both sides signed the text before dawn and set Friday for the first aid ship.",
                    },
                    {
                        "title": "Rail board publishes the weekend map",
                        "url": "https://example.com/rail",
                        "date": "Thu, 26 Nov 2026 08:00:00 GMT",
                        "summary": "Engineers reopened the main line after replacing the cracked span and posted a shorter weekend map.",
                    },
                ]
            else:
                entries = [
                    {
                        "title": f"{spec.title} council adopts the annual budget - Springfield Ledger",
                        "url": f"https://news.google.com/rss/articles/{spec.id}",
                        "date": "Thu, 26 Nov 2026 08:00:00 GMT",
                        "summary": f"{spec.title} council adopts the annual budget Springfield Ledger",
                    }
                ]
            feeds[url] = rss(entries)

        def fetcher(url):
            if url == dg.WEATHER_POINT:
                return json.dumps({"properties": {"forecast": forecast_url}}).encode()
            if url == forecast_url:
                return json.dumps({"properties": {"periods": periods}}).encode()
            if url in {dg.NEIGHBORHOOD_EVENTS_URL, dg.CHAMBER_EVENTS_URL}:
                return b""
            if url in feeds:
                return feeds[url]
            raise AssertionError(url)

        moment = dt.datetime(2026, 11, 26, 11, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            history = root / "history.json"
            bots = root / "bots"
            bots.mkdir()
            first = dg.make_payload(
                "2026-11-26",
                fetcher,
                now=moment,
                history_file=history,
                bot_directory=bots,
            )
            names = [section["section"] for section in first["sections"]]
            self.assertEqual(names, list(settings.required_titles()))
            self.assertNotIn("MUSIC", names)
            self.assertNotIn("BOT UPDATES", names)
            self.assertEqual(first["picker"]["mode"], "fallback")
            weather = first["sections"][0]["items"][0]
            self.assertIn("high 64°F", weather["headline"])
            self.assertIn("low 41°F", weather["headline"])
            self.assertIn("20% chance of precipitation", weather["headline"])
            self.assertNotIn("31", weather["headline"])
            self.assertNotIn("OVERNIGHT-ONLY", weather["summary"])
            by_name = {section["section"]: section for section in first["sections"]}
            global_titles = [item["title"] for item in by_name["GLOBAL"]["items"]]
            self.assertIn("Harbor talks end with a written ceasefire", global_titles)
            self.assertNotIn("Editor dies at 90", global_titles)
            uk_blob = json.dumps(by_name["UK"])
            self.assertNotIn(shared, uk_blob)
            self.assertIn("Rail board publishes the weekend map", uk_blob)
            markets_blob = json.dumps(by_name["SPACEX/SPACE STOCK"]).casefold()
            self.assertNotIn("last chance", markets_blob)
            self.assertIn("civil science contract", markets_blob)
            film_titles = [item["title"] for item in by_name["FILM/TV"]["items"]]
            self.assertEqual(film_titles[0], "A restored western screens all weekend")
            self.assertEqual(len(film_titles), 2)
            local_item = by_name["LOCAL"]["items"][0]
            self.assertNotIn(local_item["title"].casefold(), local_item["summary"].casefold())
            second = dg.make_payload(
                "2026-11-26",
                fetcher,
                now=moment,
                history_file=history,
                bot_directory=bots,
            )
            self.assertEqual(first["digest_id"], second["digest_id"])

            (bots / "2026-11-27.json").write_text(
                json.dumps(
                    bot_document(
                        generated_at="2026-11-27T04:30:00-06:00",
                        updates=[
                            {
                                "bot": "Popcorn",
                                "topic": "Film picks",
                                "text": "A quiet anime about a night courier opens Friday, and the review is worth the two hours.",
                                "for": "both",
                            }
                        ],
                    )
                ),
                encoding="utf-8",
            )
            with_bots = dg.make_payload(
                "2026-11-27",
                fetcher,
                now=dt.datetime(2026, 11, 27, 11, 0, tzinfo=dt.timezone.utc),
                history_file=history,
                bot_directory=bots,
            )
            bot_names = [section["section"] for section in with_bots["sections"]]
            self.assertEqual(bot_names[-1], "BOT UPDATES")
            self.assertEqual(with_bots["sections"][-1]["items"][0]["source_label"], "Popcorn")
            for section in with_bots["sections"]:
                if section["section"] in {"GLOBAL", "LOCAL", "UK", "FILM/TV", "SPACEX/SPACE STOCK"}:
                    self.assertEqual(len(section["items"]), 1)

            def timed_out(request, timeout):
                raise TimeoutError("slow")

            with mock.patch.dict(os.environ, {"DIGEST_LLM_API_KEY": SECRET}):
                failed = dg.make_payload(
                    "2026-11-28",
                    fetcher,
                    opener=timed_out,
                    now=dt.datetime(2026, 11, 28, 11, 0, tzinfo=dt.timezone.utc),
                    history_file=root / "timeout-history.json",
                    bot_directory=root / "missing-bots",
                )
            self.assertEqual(failed["picker"]["reason"], "timeout")
            self.assertTrue(failed["sections"])
            self.assertNotIn(SECRET, json.dumps(failed))
            self.assertNotIn("MUSIC", [section["section"] for section in failed["sections"]])

    def test_settings_move_markets_query_uk_feed_and_titles(self):
        with mock.patch.dict(
            os.environ,
            {
                "DIGEST_MARKETS_NEWS_QUERY": "railroad stocks",
                "DIGEST_UK_FEED": "https://example.com/uk.xml",
                "DIGEST_SECTION_TITLES": '{"uk": "BRITAIN"}',
                "DIGEST_SECTION_TITLE_MARKETS": "MARKETS",
            },
        ):
            markets, _terms = settings.news_feed("markets")
            uk, _terms = settings.news_feed("uk")
            self.assertIn("railroad+stocks", markets)
            self.assertEqual(uk, "https://example.com/uk.xml")
            self.assertEqual(settings.section_by_id("markets").title, "MARKETS")
            self.assertEqual(settings.section_by_id("uk").title, "BRITAIN")
        with mock.patch.dict(os.environ, {"DIGEST_SECTION_ORDER": "nope"}):
            self.assertEqual(settings.resolve_order(), settings.DEFAULT_SECTION_ORDER)
            self.assertTrue(any("DIGEST_SECTION_ORDER" in problem for problem in settings.configuration_problems()))


if __name__ == "__main__":
    unittest.main()
