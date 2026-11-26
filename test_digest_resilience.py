#!/usr/bin/env python3
"""Failure cases that must still put a receipt on paper."""

import contextlib
import datetime as dt
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock
from zoneinfo import ZoneInfo

for _name in list(os.environ):
    if _name.startswith("DIGEST_") and _name not in {"DIGEST_HOME"}:
        os.environ.pop(_name, None)
os.environ["DIGEST_LLM_API_KEY"] = ""
os.environ["DIGEST_TZ"] = "America/Chicago"

import digest_generator as dg
import digest_picker as dp
import digest_settings as settings
import receipt_digest as rd
import receipt_digest_trigger as trigger
import test_receipt_digest as fixtures


SECRET = "sk-test-secret-value"
NOW = dt.datetime(2026, 11, 26, 11, 0, tzinfo=dt.timezone.utc)


def bot_document(**kwargs):
    from test_digest_picker import bot_document as build

    return build(**kwargs)


class HostileFileTests(unittest.TestCase):
    def test_bad_bot_file_does_not_abort_and_refuses_links(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            day = directory / "2026-11-26.json"
            day.write_bytes("\ufeff".encode("utf-16"))
            items, error = dp.load_bot_updates(directory, "2026-11-26", NOW)
            self.assertIsNone(items)
            self.assertIn("invalid", error)

            nested = "[" * 10000 + "]" * 10000
            day.write_text(nested, encoding="utf-8")
            items, error = dp.load_bot_updates(directory, "2026-11-26", NOW)
            self.assertIsNone(items)
            self.assertIn("invalid", error)

            day.write_bytes(b"x" * 70000)
            items, error = dp.load_bot_updates(directory, "2026-11-26", NOW)
            self.assertIsNone(items)
            self.assertIn("too large", error)

            outside = directory.parent / "outside.json"
            outside.write_text(json.dumps(bot_document()), encoding="utf-8")
            day.unlink()
            day.symlink_to(outside)
            items, error = dp.load_bot_updates(directory, "2026-11-26", NOW)
            self.assertIsNone(items)
            self.assertIn("unreadable", error)
            self.assertNotIn("Popcorn", json.dumps(items))

            def fetcher(_url):
                raise AssertionError("network")

            payload = dg.make_payload(
                "2026-11-26",
                fetcher,
                now=NOW,
                history_file=directory / "missing-history.json",
                bot_directory=directory,
                budget_seconds=5,
            )
            self.assertTrue(payload["sections"])
            self.assertNotIn("BOT UPDATES", [section["section"] for section in payload["sections"]])
            self.assertNotIn(str(outside), json.dumps(payload))
            document = bot_document(
                updates=[
                    {
                        "bot": "Popcorn",
                        "topic": "Film picks",
                        "text": "A quiet anime about a night courier opens Friday, and the review is worth the two hours.",
                        "for": "both",
                        "url": "https://token:secret@example.com/night",
                    }
                ]
            )
            cleaned = dp.validate_bot_document(document, now=NOW)
            self.assertEqual(cleaned[0]["url"], "https://example.com/night")
            self.assertNotIn("secret", json.dumps(cleaned))

    def test_corrupt_history_is_kept_and_does_not_abort(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "digest-history.json"
            path.write_text("[" * 10000 + "]" * 10000, encoding="utf-8")
            before = path.read_bytes()
            loaded, error = dp.load_history(path)
            self.assertEqual(loaded, [])
            self.assertIn("unreadable", error)
            saved = dp.save_history(
                path,
                "2026-11-26",
                [{"digest_date": "2026-11-26", "section": "global", "url": "https://example.com/a", "title": "a story title here"}],
            )
            self.assertFalse(saved)
            self.assertEqual(path.read_bytes(), before)

            oversized = Path(temp) / "big.json"
            oversized.write_bytes(b"{" + b" " * 70000 + b"}")
            loaded, error = dp.load_history(oversized)
            self.assertIn("unreadable", error)
            self.assertGreater(oversized.stat().st_size, dp.HISTORY_MAX_BYTES)

    def test_history_save_waits_for_the_print_lock(self):
        import fcntl

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / "print.lock"
            path = root / "digest-history.json"
            held = threading.Event()
            release = threading.Event()

            def holder():
                fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(fd, fcntl.LOCK_EX)
                held.set()
                self.assertTrue(release.wait(3))
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(held.wait(2))
            started = time.monotonic()

            def let_go():
                time.sleep(0.4)
                release.set()

            threading.Thread(target=let_go, daemon=True).start()
            with mock.patch.dict(os.environ, {"DIGEST_LOCK_PATH": str(lock), "DIGEST_LOCK_HELD": ""}):
                os.environ.pop("DIGEST_LOCK_HELD", None)
                saved = dp.save_history(path, "2026-11-26", [])
            self.assertTrue(saved)
            self.assertGreaterEqual(time.monotonic() - started, 0.3)
            thread.join(2)
            self.assertFalse(thread.is_alive())


class ModelFailureTests(unittest.TestCase):
    def test_dropped_socket_and_partial_body_fall_back(self):
        import http.client

        requests = [
            {
                "section_id": "global",
                "title": "GLOBAL",
                "purpose": "world",
                "feed_url": "https://example.com/global",
                "prefer_terms": (),
                "candidates": [
                    {
                        "title": "Harbor talks end with a written ceasefire",
                        "url": "https://example.com/harbor",
                        "summary": "Mediators said both sides signed the text before dawn and set Friday for the first aid ship.",
                        "published_at": "Thu, 26 Nov 2026 09:00:00 GMT",
                    }
                ],
            }
        ]

        def reset(request, timeout):
            raise ConnectionResetError("reset")

        class Incomplete:
            def read(self, _limit=-1):
                raise http.client.IncompleteRead(b"partial")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def incomplete(_request, _timeout):
            return Incomplete()

        with mock.patch.dict(os.environ, {"DIGEST_LLM_API_KEY": SECRET, "DIGEST_LLM_BASE_URL": "https://user:token@api.example.test/v1"}):
            for opener in (reset, incomplete):
                picked, meta, errors, _records = dp.pick_news(
                    requests,
                    digest_date="2026-11-26",
                    history=[],
                    has_bots=False,
                    opener=opener,
                    item_count=1,
                    summary_chars=300,
                )
                self.assertEqual(meta["mode"], "fallback")
                self.assertTrue(picked["global"])
                blob = json.dumps({"picked": picked, "errors": errors})
                self.assertNotIn(SECRET, blob)
                self.assertNotIn("token", blob)
                self.assertNotIn("user:token", blob)
                self.assertTrue(errors[0]["error"].startswith("llm fallback:"))

    def test_http_400_retry_uses_the_remaining_budget(self):
        calls = []

        def opener(request, timeout):
            calls.append(timeout)
            raise urllib.error.HTTPError(request.full_url, 400, "bad", Message(), None)

        deadline = time.monotonic() + 1.2
        with mock.patch.dict(os.environ, {"DIGEST_LLM_API_KEY": SECRET}):
            with self.assertRaises(dp.LLMError) as caught:
                dp.llm_pick(
                    [("GLOBAL", "world", [{"title": "Harbor talks end with a written ceasefire", "url": "https://example.com/harbor", "summary": "Mediators said both sides signed the text before dawn and set Friday for the first aid ship."}])],
                    count=1,
                    max_chars=200,
                    taste="fixture",
                    opener=opener,
                    deadline=deadline,
                )
        self.assertEqual(caught.exception.code, "http_400")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(timeout <= 1.2 for timeout in calls))

    def test_zero_budget_skips_fetches_and_still_returns_sections(self):
        def fetcher(_url):
            raise AssertionError("fetcher should not run once the budget is spent")

        payload = dg.make_payload(
            "2026-11-26",
            fetcher,
            now=NOW,
            history_file=Path(tempfile.gettempdir()) / "unused-history-does-not-exist.json",
            bot_directory=Path(tempfile.gettempdir()) / "unused-bots",
            budget_seconds=0,
            persist_history=False,
        )
        self.assertGreaterEqual(len(payload["sections"]), 4)
        names = [section["section"] for section in payload["sections"]]
        self.assertNotIn("UK", names)
        self.assertNotIn("FILM/TV", names)
        self.assertNotIn("EVENTS TODAY / SOON", names)
        self.assertNotIn("taste_profile", payload)
        self.assertTrue(payload["digest_id"])

    def test_parallel_fetch_runs_companies_and_artists_on_a_slow_clock(self):
        artists = Path(tempfile.gettempdir()) / "digest-artists-slow.txt"
        artists.write_text("\n".join(f"Sample Band {index}" for index in range(8)), encoding="utf-8")
        self.addCleanup(artists.unlink, missing_ok=True)
        env = {
            "DIGEST_GLOBAL_FEED": "https://example.test/global.xml",
            "DIGEST_LOCAL_FEED": "https://example.test/local.xml",
            "DIGEST_SPACEX_FEED": "https://example.test/spacex.xml",
            "DIGEST_EARTH_FUNERAL_FEED": "https://example.test/earth.xml",
            "DIGEST_TICKETMASTER_KEY": "tm-test-key",
            "DIGEST_ARTISTS_FILE": str(artists),
        }
        for delay in (3, 8):
            calls = []
            state = {"t": 0.0}

            def fetcher(url, _calls=calls):
                _calls.append(url)
                return b"<rss><channel></channel></rss>"

            def clock():
                return state["t"]

            def wait(inflight, timeout, done):
                if timeout <= 0:
                    return []
                finished = []
                want = set(inflight)
                deadline = time.monotonic() + 2
                while want and time.monotonic() < deadline:
                    try:
                        key, value = done.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if key in want:
                        want.discard(key)
                        finished.append((key, value))
                state["t"] += delay
                return finished

            state["t"] = 0.0
            calls.clear()
            with mock.patch.dict(os.environ, env), mock.patch.object(dg, "_clock", clock), mock.patch.object(
                dg, "_wait_wave", wait
            ):
                payload = dg.make_payload(
                    "2026-11-26",
                    fetcher,
                    now=NOW,
                    history_file=Path(tempfile.gettempdir()) / "unused-history-parallel.json",
                    bot_directory=Path(tempfile.gettempdir()) / "unused-bots-parallel",
                    budget_seconds=75,
                    persist_history=False,
                )
            joined = "\n".join(calls)
            self.assertIn("https://example.test/spacex.xml", joined, delay)
            self.assertIn("https://example.test/earth.xml", joined, delay)
            for index in range(8):
                self.assertIn(f"Sample+Band+{index}", joined, delay)
            self.assertLessEqual(state["t"], 35, delay)
            self.assertIn("fetched, no story", json.dumps(payload["source_errors"]))

        def quiet(_url):
            raise AssertionError("a spent budget must not request a feed")

        skipped = dg.make_payload(
            "2026-11-26",
            quiet,
            now=NOW,
            history_file=Path(tempfile.gettempdir()) / "unused-history-unfetched.json",
            bot_directory=Path(tempfile.gettempdir()) / "unused-bots-unfetched",
            budget_seconds=0,
            persist_history=False,
        )
        blob = json.dumps(skipped)
        self.assertNotIn("No new update today", blob)
        self.assertIn("not fetched", blob)
        self.assertIn("Feed not fetched", blob)

    def test_a_sleeping_worker_cannot_hold_the_fetch_or_the_process(self):
        artists = Path(tempfile.gettempdir()) / "digest-artists-wall.txt"
        artists.write_text("\n".join(f"Sample Band {index}" for index in range(8)), encoding="utf-8")
        self.addCleanup(artists.unlink, missing_ok=True)
        script = """
import json, os, time
from pathlib import Path
os.environ["DIGEST_GLOBAL_FEED"] = "https://example.test/global.xml"
os.environ["DIGEST_LOCAL_FEED"] = "https://example.test/local.xml"
os.environ["DIGEST_SPACEX_FEED"] = "https://example.test/spacex.xml"
os.environ["DIGEST_EARTH_FUNERAL_FEED"] = "https://example.test/earth.xml"
os.environ["DIGEST_TICKETMASTER_KEY"] = "tm-test-key"
os.environ["DIGEST_ARTISTS_FILE"] = %r
import digest_generator as dg
dg.FETCH_STAGE_SECONDS = 0.4
def fetcher(url):
    time.sleep(30)
    return b"<rss><channel></channel></rss>"
started = time.monotonic()
payload = dg.make_payload(
    "2026-11-26",
    fetcher,
    history_file=Path("/tmp/unused-wall-history.json"),
    bot_directory=Path("/tmp/unused-wall-bots"),
    budget_seconds=75,
    persist_history=False,
)
elapsed = time.monotonic() - started
print(json.dumps({"elapsed": elapsed, "errors": payload["source_errors"], "sections": payload["sections"]}))
""" % str(artists)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd="/workspace",
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        data = json.loads(proc.stdout)
        self.assertLess(data["elapsed"], 3.0)
        blob = json.dumps(data)
        self.assertIn("timed out", blob)
        self.assertIn("not fetched", blob)
        self.assertNotIn("No new update today", blob)
        self.assertNotIn("tm-test-key", blob)

    def test_google_and_ticketmaster_stay_under_their_caps(self):
        state = {"google": 0, "ticketmaster": 0, "google_max": 0, "ticketmaster_max": 0}
        lock = threading.Lock()

        def bump(bucket: str):
            with lock:
                state[bucket] += 1
                state[bucket + "_max"] = max(state[bucket + "_max"], state[bucket])
            time.sleep(0.15)
            with lock:
                state[bucket] -= 1
            return b"ok"

        jobs = []
        for index in range(6):
            jobs.append((f"g{index}", f"https://news.google.com/rss/search?q={index}", lambda bucket="google": bump(bucket)))
        for index in range(6):
            jobs.append(
                (f"t{index}", f"https://app.ticketmaster.com/discovery/v2/events.json?q={index}", lambda bucket="ticketmaster": bump(bucket))
            )
        results, missed, overdue = dg._parallel_map(jobs, 10)
        self.assertEqual(len(results), 12, (missed, overdue))
        self.assertLessEqual(state["google_max"], 2)
        self.assertGreaterEqual(state["google_max"], 2)
        self.assertLessEqual(state["ticketmaster_max"], 3)
        self.assertGreaterEqual(state["ticketmaster_max"], 2)

    def test_fetch_retries_a_server_error_once(self):
        calls = []

        class Body:
            def __init__(self):
                self._sent = False

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, _n=8192):
                if self._sent:
                    return b""
                self._sent = True
                return b"<rss></rss>"

        def open_bounded(request, timeout, deadline):
            calls.append(getattr(request, "full_url", ""))
            if len(calls) == 1:
                raise urllib.error.HTTPError(calls[-1], 503, "busy", Message(), None)
            return Body()

        with mock.patch.object(dp, "open_bounded", open_bounded), mock.patch.object(dg.time, "sleep"):
            body = dg.fetch("https://example.test/feed.xml")
        self.assertEqual(body, b"<rss></rss>")
        self.assertEqual(len(calls), 2)

        calls.clear()

        def always_busy(request, timeout, deadline):
            calls.append("x")
            raise urllib.error.HTTPError("https://example.test/feed.xml", 429, "slow", Message(), None)

        token = dg._budget_var.set(dg.Budget(75))
        self.addCleanup(dg._budget_var.reset, token)
        budget = dg._budget_var.get()
        budget.deadline = time.monotonic() + 0.2
        with mock.patch.object(dp, "open_bounded", always_busy):
            with self.assertRaises(urllib.error.HTTPError):
                dg.fetch("https://example.test/feed.xml")
        self.assertEqual(len(calls), 1)

    def test_tls_handshake_sets_the_timeout_before_wrap(self):
        order = []
        sock = mock.Mock()
        sock.fileno.side_effect = OSError("no real socket")
        sock.settimeout.side_effect = lambda _value: order.append("timeout")
        context = mock.Mock()

        def wrap(_raw, server_hostname=None):
            order.append("wrap")
            wrapped = mock.Mock()
            wrapped.fileno.side_effect = OSError("no real socket")
            return wrapped

        context.wrap_socket.side_effect = wrap
        watch = dp._DeadlineWatch(time.monotonic() + 5)
        self.addCleanup(watch.disarm)
        conn = dp._DeadlineHTTPSConnection("example.test", timeout=5, context=context, watch=watch)

        def skip_tcp(self):
            self.sock = sock

        with mock.patch.object(dp._DeadlineHTTPConnection, "connect", skip_tcp):
            conn.connect()
        self.assertEqual(order, ["timeout", "wrap"])


class WeatherSplitTests(unittest.TestCase):
    def test_split_day_uses_the_higher_daytime_and_a_later_night(self):
        periods = [
            {
                "name": "This Morning",
                "isDaytime": True,
                "startTime": "2026-11-26T06:00:00-06:00",
                "temperature": 48,
                "temperatureUnit": "F",
                "shortForecast": "Fog",
                "detailedForecast": "Fog early.",
                "probabilityOfPrecipitation": {"value": 0},
            },
            {
                "name": "This Afternoon",
                "isDaytime": True,
                "startTime": "2026-11-26T12:00:00-06:00",
                "temperature": 70,
                "temperatureUnit": "F",
                "shortForecast": "Sunny",
                "detailedForecast": "Sunny later.",
                "probabilityOfPrecipitation": {"value": 10},
            },
            {
                "name": "Tonight",
                "isDaytime": False,
                "startTime": "2026-11-26T18:00:00-06:00",
                "temperature": 41,
                "temperatureUnit": "F",
                "shortForecast": "Clear",
                "detailedForecast": "Clear overnight.",
            },
        ]
        day, night = dg.select_daytime_period(periods, "2026-11-26", ZoneInfo("America/Chicago"))
        item = dg.format_weather_item(day, night, "https://api.weather.gov/forecast")
        self.assertEqual(day["name"], "This Afternoon")
        self.assertIn("high 70°F", item["headline"])
        self.assertIn("low 41°F", item["headline"])
        self.assertNotIn("48", item["headline"])

    def test_dst_dates_and_digest_zone(self):
        zone = ZoneInfo("America/Chicago")
        spring = [{"name": "Sunday", "isDaytime": True, "startTime": "2026-03-08T06:00:00-05:00", "temperature": 55, "temperatureUnit": "F"}]
        fall = [{"name": "Sunday", "isDaytime": True, "startTime": "2026-11-01T06:00:00-06:00", "temperature": 60, "temperatureUnit": "F"}]
        self.assertEqual(dg.select_daytime_period(spring, "2026-03-08", zone)[0]["temperature"], 55)
        self.assertEqual(dg.select_daytime_period(fall, "2026-11-01", zone)[0]["temperature"], 60)
        evening = [{"name": "Evening", "isDaytime": True, "startTime": "2026-11-26T20:00:00-06:00", "temperature": 52, "temperatureUnit": "F"}]
        self.assertEqual(dg.select_daytime_period(evening, "2026-11-26", zone)[0]["temperature"], 52)
        with self.assertRaises(LookupError):
            dg.select_daytime_period(evening, "2026-11-26", ZoneInfo("UTC"))
        moment = dt.datetime(2026, 11, 27, 2, 0, tzinfo=dt.timezone.utc)
        with mock.patch.dict(os.environ, {"DIGEST_TZ": "America/Chicago"}):
            self.assertEqual(dg.digest_date(moment), "2026-11-26")
        with mock.patch.dict(os.environ, {"DIGEST_TZ": "UTC"}):
            self.assertEqual(dg.digest_date(moment), "2026-11-27")


class LayoutFailureTests(unittest.TestCase):
    def test_illegal_character_and_huge_title_still_render(self):
        value = fixtures.payload()
        value["sections"][0]["items"][0]["headline"] = "The council approved the budget today.\x01"
        value["sections"][0]["items"][0]["summary"] = "The council approved the budget today and posted the full text.\x01"
        value["digest_id"] = rd.payload_digest_id(value)
        with tempfile.TemporaryDirectory() as temp:
            metadata = fixtures.ReceiptDigestTests().render_payload(value, Path(temp))
            svg = Path(metadata["svg"]).read_text(encoding="utf-8")
            self.assertNotIn("\x01", svg)
            rd.ET.fromstring(svg.split("-->", 1)[1] if svg.startswith("<!--") else svg)
            self.assertTrue(Path(metadata["png"]).exists())

        huge = fixtures.payload()
        huge["sections"][1]["items"][0]["title"] = ("Word " * 4000).strip()
        huge["digest_id"] = rd.payload_digest_id(huge)
        _blocks, height, _lines, _head, _chars, clipped = rd.layout_payload(huge)
        self.assertLessEqual(height, rd.MAX_HEIGHT_DOTS)
        self.assertTrue(clipped)
        with tempfile.TemporaryDirectory() as temp:
            metadata = fixtures.ReceiptDigestTests().render_payload(huge, Path(temp))
            self.assertLessEqual(metadata["geometry"]["height_dots"], rd.MAX_HEIGHT_DOTS)

    def test_short_bot_day_is_not_refused(self):
        value = fixtures.payload()
        for section in value["sections"]:
            for item in section["items"]:
                if "headline" in item:
                    item["headline"] = "Sunny."
                    item["summary"] = "Sunny."
                else:
                    item["title"] = "From the wire"
                    item["summary"] = "From example.com, 26 Nov 2026."
        value["sections"].append(
            {
                "section": "BOT UPDATES",
                "source_feed": "bot-updates",
                "items": [
                    {
                        "title": "Popcorn: Film picks",
                        "summary": "From the shelf, 26 Nov 2026.",
                        "source_label": "Popcorn",
                    }
                ],
            }
        )
        value["digest_id"] = rd.payload_digest_id(value)
        _blocks, height, _lines, _head, chars, _clipped = rd.layout_payload(value)
        self.assertLess(chars, 870)
        self.assertLessEqual(height, rd.MAX_HEIGHT_DOTS)
        with tempfile.TemporaryDirectory() as temp:
            metadata = fixtures.ReceiptDigestTests().render_payload(value, Path(temp))
            self.assertEqual(metadata["geometry"]["section_count"], len(rd.EXPECTED_SECTIONS) + 1)

    def test_control_character_is_removed_from_a_summary(self):
        fitted = dp.fit_summary(
            "Council budget",
            "The council approved the budget today and posted the full text.\x01",
            300,
        )
        self.assertIsNotNone(fitted)
        self.assertNotIn("\x01", fitted)
        self.assertNotIn("\x01", dg.clean("Approved\x01 today"))


class TriggerTests(unittest.TestCase):
    def test_response_has_no_stderr(self):
        body = trigger.bridge_body(1, "token-in-stdout-but-not-json")
        self.assertEqual(set(body), {"status", "returncode"})
        self.assertEqual(body["status"], "error")
        parsed = trigger.bridge_body(0, json.dumps({"status": "dry_run", "digest_id": "abc"}))
        self.assertEqual(parsed["result"]["digest_id"], "abc")
        self.assertNotIn("stderr", parsed)

    def test_timeout_kills_the_flock_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / "print.lock"
            pidfile = root / "child.pid"
            script = root / "bridge.sh"
            script.write_text(
                "#!/bin/bash\n"
                f"exec flock --exclusive --nonblock {lock} bash -c 'sleep 30 & echo $! > {pidfile}; wait'\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                trigger.run_bridge("--submit", timeout=1, bridge=str(script))
            self.assertLess(time.monotonic() - started, 8)
            pid = int(pidfile.read_text(encoding="utf-8").strip())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.05)
            self.fail("sleep child was still alive after the process group was killed")


class _QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.gets += 1
        if self.path.startswith("/points"):
            body = json.dumps({"properties": {"forecast": self.server.forecast_url}}).encode()
        elif self.path.startswith("/forecast"):
            body = json.dumps({"properties": {"periods": self.server.periods}}).encode()
        elif self.path.startswith("/rss/"):
            section = self.path.rsplit("/", 1)[-1]
            body = (
                "<?xml version='1.0'?><rss version='2.0'><channel><item>"
                f"<title>Council funds the {section} reading room</title>"
                f"<link>https://example.com/{section}-room</link>"
                "<pubDate>Thu, 26 Nov 2026 08:00:00 GMT</pubDate>"
                "<description>The council funded the reading room and posted the weekend hours for residents.</description>"
                "</item></channel></rss>"
            ).encode()
        else:
            body = b"<html></html>"
        self._send(body)

    def do_POST(self):
        self.server.posts += 1
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        content = json.dumps({"picks": []})
        payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self._send(payload)


class ShellEndToEndTests(unittest.TestCase):
    def test_print_script_reuses_the_file_and_can_reprint(self):
        for tool in ("flock", "python3", "rsvg-convert", "date", "bash", "rm", "mkdir"):
            if shutil.which(tool) is None:
                self.skipTest(f"{tool} is not installed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bindir = root / "bin"
            bindir.mkdir()
            lp_log = root / "lp.log"
            for name in ("python3", "flock", "rsvg-convert", "date", "bash", "rm", "mkdir"):
                os.symlink(shutil.which(name), bindir / name)
            (bindir / "lp").write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$LP_LOG\"\necho \"request id is receipt-1 (1 file(s))\"\n",
                encoding="utf-8",
            )
            (bindir / "lpstat").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(bindir / "lp", 0o755)
            os.chmod(bindir / "lpstat", 0o755)
            repo = Path(__file__).resolve().parent
            for name in (
                "digest_generator.py",
                "digest_picker.py",
                "digest_settings.py",
                "receipt_digest.py",
                "print_digest_safe.sh",
            ):
                os.symlink(repo / name, root / name)

            zone = ZoneInfo("America/Chicago")
            today = dt.datetime.now(zone).date()
            start = dt.datetime.combine(today, dt.time(6, 0), zone).isoformat()
            night = dt.datetime.combine(today, dt.time(18, 0), zone).isoformat()
            server = ThreadingHTTPServer(("127.0.0.1", 0), _QuietHandler)
            server.gets = 0
            server.posts = 0
            port = server.server_address[1]
            server.forecast_url = f"http://127.0.0.1:{port}/forecast"
            server.periods = [
                {
                    "name": "Today",
                    "isDaytime": True,
                    "startTime": start,
                    "temperature": 64,
                    "temperatureUnit": "F",
                    "shortForecast": "Partly cloudy",
                    "detailedForecast": "Partly cloudy during the day with a light breeze.",
                    "probabilityOfPrecipitation": {"value": 20},
                },
                {
                    "name": "Tonight",
                    "isDaytime": False,
                    "startTime": night,
                    "temperature": 41,
                    "temperatureUnit": "F",
                    "shortForecast": "Clear",
                    "detailedForecast": "Clear after sunset.",
                },
            ]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{port}"
            bots = root / "bot-updates"
            bots.mkdir()
            (bots / f"{today.isoformat()}.json").write_bytes("not utf-8".encode("utf-16"))
            env = os.environ.copy()
            env.update(
                {
                    "DIGEST_HOME": str(root),
                    "DIGEST_TOOL_PATH": str(bindir),
                    "DIGEST_TZ": "America/Chicago",
                    "DIGEST_PRINTER_QUEUE": "receipt",
                    "DIGEST_LLM_API_KEY": SECRET,
                    "DIGEST_LLM_BASE_URL": base + "/v1",
                    "DIGEST_LLM_TIMEOUT": "5",
                    "LP_LOG": str(lp_log),
                    "DIGEST_BOT_UPDATES_DIR": str(bots),
                    "DIGEST_HISTORY_PATH": str(root / "digest-history.json"),
                    "DIGEST_WEATHER_POINT": base + "/points",
                    "DIGEST_GLOBAL_FEED": base + "/rss/global",
                    "DIGEST_LOCAL_FEED": base + "/rss/local",
                    "DIGEST_COMPANIES_FEED": base + "/rss/companies",
                    "DIGEST_WEATHER_LAT": "0",
                    "DIGEST_WEATHER_LON": "0",
                    "DIGEST_LOCATION_LABEL": "Bastrop, TX",
                    "DIGEST_LOCAL_CITY": "Bastrop",
                }
            )
            # The generator reads the point URL from its own defaults unless the lat/lon
            # match. Point the weather constant by serving whatever host the process calls
            # only if the URL is our server. Override by a tiny wrapper is unnecessary:
            # the imported module builds WEATHER_POINT at import from the environment.
            script = root / "print_digest_safe.sh"

            def run(mode: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["bash", str(script), mode],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )

            try:
                # Weather URL is compiled into the fresh interpreter from DIGEST_WEATHER_LAT.
                # Confirm the subprocess will call our server by checking the env-built URL
                # shape used in digest_generator: points/{lat},{lon}. The handler accepts /points.
                first = run("--submit")
                self.assertEqual(first.returncode, 0, first.stderr[-2000:])
                self.assertNotIn(SECRET, first.stdout)
                self.assertNotIn(SECRET, first.stderr)
                result = json.loads(first.stdout)
                self.assertIn(result["status"], {"submitted", "completed"})
                self.assertEqual(len(lp_log.read_text(encoding="utf-8").splitlines()), 1)
                posts_after_first = server.posts
                self.assertGreaterEqual(posts_after_first, 1)
                source = root / "output" / f"digest-source-{today.isoformat()}.json"
                self.assertTrue(source.is_file())
                stored = json.loads(source.read_text(encoding="utf-8"))
                self.assertNotIn(SECRET, json.dumps(stored))
                self.assertNotIn("taste_profile", stored)
                self.assertIn("high 64", json.dumps(stored))

                second = run("--submit")
                self.assertEqual(second.returncode, 0, second.stderr[-2000:])
                again = json.loads(second.stdout)
                self.assertEqual(again["status"], "already_submitted")
                self.assertEqual(server.posts, posts_after_first)
                self.assertEqual(len(lp_log.read_text(encoding="utf-8").splitlines()), 1)

                reprint = run("--reprint")
                self.assertEqual(reprint.returncode, 0, reprint.stderr[-2000:])
                self.assertEqual(server.posts, posts_after_first)
                self.assertEqual(len(lp_log.read_text(encoding="utf-8").splitlines()), 2)

                state_path = root / "state.json"
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["digests"][result["digest_id"]]["status"] = "failed"
                state_path.write_text(json.dumps(state), encoding="utf-8")
                retried = run("--submit")
                self.assertEqual(retried.returncode, 0, retried.stderr[-2000:])
                self.assertEqual(server.posts, posts_after_first)
                self.assertEqual(len(lp_log.read_text(encoding="utf-8").splitlines()), 3)

                source.write_bytes(b"\xff\xfe not a source")
                rebuilt = run("--dry-run")
                self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr[-2000:])
                self.assertGreater(server.posts, posts_after_first)
                self.assertTrue(source.is_file())
                json.loads(source.read_text(encoding="utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)


def _accept_one(handler):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def serve():
        try:
            conn, _addr = sock.accept()
        except OSError:
            return
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(5)
            handler(conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return sock, port, thread


def _read_request(conn) -> None:
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 65536:
        piece = conn.recv(4096)
        if not piece:
            break
        data += piece


def _send_split_chunks(conn, body: bytes) -> None:
    """Flush after 40 payload bytes, leaving the chunk's trailing CRLF for the next write."""
    _read_request(conn)
    first, rest = body[:40], body[40:]
    conn.sendall(
        b"HTTP/1.1 200 OK\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        + f"{len(first):X}\r\n".encode()
        + first
    )
    time.sleep(0.3)
    conn.sendall(b"\r\n" + f"{len(rest):X}\r\n".encode() + rest + b"\r\n0\r\n\r\n")


class FetchBudgetTests(unittest.TestCase):
    def test_trickling_body_cannot_hold_the_budget(self):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]

        def serve():
            try:
                conn, _addr = sock.accept()
            except OSError:
                return
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\nConnection: close\r\n\r\n")
                for _ in range(50):
                    conn.sendall(b"x" * 30)
                    time.sleep(0.4)
            except OSError:
                pass
            finally:
                conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        token = dg._budget_var.set(dg.Budget(1.0))
        started = time.monotonic()
        try:
            with self.assertRaises((TimeoutError, OSError)):
                dg.fetch(f"http://127.0.0.1:{port}/slow")
        finally:
            dg._budget_var.reset(token)
            sock.close()
        self.assertLess(time.monotonic() - started, 4.0)
        thread.join(2)

    def test_trickling_headers_cannot_hold_the_budget(self):
        """A byte of status line every 0.25s must not run past a 1s deadline."""

        def serve(conn):
            _read_request(conn)
            payload = b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nConnection: close\r\n\r\nx"
            for index in range(len(payload)):
                conn.sendall(payload[index : index + 1])
                time.sleep(0.25)

        sock, port, thread = _accept_one(serve)
        token = dg._budget_var.set(dg.Budget(1.0))
        started = time.monotonic()
        try:
            with self.assertRaises((TimeoutError, OSError)):
                dg.fetch(f"http://127.0.0.1:{port}/headers")
        finally:
            dg._budget_var.reset(token)
            sock.close()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.75)
        self.assertLess(elapsed, 4.0)
        thread.join(2)

    def test_chunk_split_at_the_boundary_returns_the_whole_feed(self):
        body = (
            b"<?xml version='1.0'?><rss version='2.0'><channel><item>"
            b"<title>Council funds the harbor reading room</title>"
            b"<link>https://example.com/harbor-room</link>"
            b"<description>The council funded the reading room and posted the weekend hours for residents.</description>"
            b"</item></channel></rss>"
        )
        self.assertGreater(len(body), 40)
        sock, port, thread = _accept_one(lambda conn: _send_split_chunks(conn, body))
        token = dg._budget_var.set(dg.Budget(10.0))
        try:
            raw = dg.fetch(f"http://127.0.0.1:{port}/rss")
        finally:
            dg._budget_var.reset(token)
            sock.close()
            thread.join(2)
        self.assertEqual(raw, body)
        items = dg.rss_items(raw, limit=5)
        self.assertEqual(items[0]["title"], "Council funds the harbor reading room")

    def test_chunk_split_at_the_boundary_returns_the_whole_model_response(self):
        content = json.dumps(
            {
                "picks": [
                    {
                        "section": "GLOBAL",
                        "items": [
                            {
                                "url": "https://example.com/harbor",
                                "summary": "Mediators finished a written ceasefire before dawn.",
                            }
                        ],
                    }
                ]
            }
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.assertGreater(len(body), 40)
        sock, port, thread = _accept_one(lambda conn: _send_split_chunks(conn, body))
        try:
            text = dp._post_chat(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                "test-key",
                {"model": "test"},
                dp._default_opener,
                8,
                time.monotonic() + 10,
            )
        finally:
            sock.close()
            thread.join(2)
        self.assertEqual(text, content)
        self.assertGreater(len(text.encode()), 40)


class StateRecoveryTests(unittest.TestCase):
    def test_broken_state_is_set_aside_and_submit_continues(self):
        if shutil.which("rsvg-convert") is None:
            self.skipTest("rsvg-convert is not installed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bindir = root / "bin"
            bindir.mkdir()
            (bindir / "lp").write_text(
                "#!/bin/sh\necho 'request id is receipt-9 (1 file(s))'\n",
                encoding="utf-8",
            )
            (bindir / "lpstat").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(bindir / "lp", 0o755)
            os.chmod(bindir / "lpstat", 0o755)
            source = root / "source.json"
            value = fixtures.payload()
            source.write_text(json.dumps(value), encoding="utf-8")
            state = root / "state.json"
            state.write_text("{", encoding="utf-8")
            output = root / "output"
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}):
                with contextlib.redirect_stdout(stdout):
                    code = rd.main(
                        [
                            "--submit",
                            "--source",
                            str(source),
                            "--output-dir",
                            str(output),
                            "--state",
                            str(state),
                            "--queue",
                            "receipt",
                            "--poll-seconds",
                            "1",
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "completed")
            kept = list(root.glob("state.json.corrupt-*"))
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0].read_text(encoding="utf-8"), "{")
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 1)
            self.assertIn(value["digest_id"], saved["digests"])
            self.assertIn(saved["digests"][value["digest_id"]]["status"], {"submitted", "completed"})

            huge = root / "huge.json"
            huge.write_bytes(b"{" + b" " * rd.STATE_MAX_BYTES)
            self.assertEqual(rd.read_state(huge), {"version": 1, "digests": {}})
            self.assertFalse(huge.exists())
            quarantined = list(root.glob("huge.json.corrupt-*"))
            self.assertEqual(len(quarantined), 1)
            self.assertGreater(quarantined[0].stat().st_size, rd.STATE_MAX_BYTES)

            shaped = root / "shape.json"
            shaped.write_text('{"version": 2, "digests": {}}', encoding="utf-8")
            self.assertEqual(rd.read_state(shaped), {"version": 1, "digests": {}})
            self.assertFalse(shaped.exists())
            self.assertEqual(len(list(root.glob("shape.json.corrupt-*"))), 1)

            nondict = root / "nonstr.json"
            nondict.write_text('{"version": 1, "digests": {"abc": "nope"}}', encoding="utf-8")
            self.assertEqual(rd.read_state(nondict), {"version": 1, "digests": {}})
            self.assertFalse(nondict.exists())
            self.assertEqual(len(list(root.glob("nonstr.json.corrupt-*"))), 1)

            replaced = root / "replaced.json"
            replaced.write_text("{", encoding="utf-8")
            identity = (replaced.stat().st_dev, replaced.stat().st_ino)
            held = root / "held.json"
            replaced.rename(held)
            replaced.write_text('{"version": 1, "digests": {}}', encoding="utf-8")
            self.assertNotEqual((replaced.stat().st_dev, replaced.stat().st_ino), identity)
            self.assertFalse(rd._quarantine_state(replaced, identity))
            self.assertEqual(replaced.read_text(encoding="utf-8"), '{"version": 1, "digests": {}}')
            self.assertEqual(held.read_text(encoding="utf-8"), "{")

            stale = root / "fresh.json.corrupt-old"
            stale.write_text("{", encoding="utf-8")
            old = time.time() - (20 * 24 * 60 * 60)
            os.utime(stale, (old, old))
            fresh_state = root / "fresh.json"
            fresh_state.write_text('{"version": 1, "digests": {}}', encoding="utf-8")
            self.assertEqual(rd.read_state(fresh_state)["version"], 1)
            self.assertFalse(stale.exists())

    def test_fifo_state_returns_without_blocking(self):
        with tempfile.TemporaryDirectory() as temp:
            fifo = Path(temp) / "state.json"
            os.mkfifo(fifo)
            result: dict = {}

            def run():
                result["state"] = rd.read_state(fifo)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["state"], {"version": 1, "digests": {}})
            self.assertTrue(fifo.exists())


class SourceUrlTests(unittest.TestCase):
    def test_event_and_forecast_urls_drop_userinfo(self):
        secret = "forecast-userinfo-secret"
        start = dt.datetime(2026, 11, 26, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
        body = json.dumps(
            {
                "_embedded": {
                    "events": [
                        {
                            "id": "show-9",
                            "name": "Sample Band",
                            "url": f"https://user:{secret}@www.ticketmaster.com/event/show-9",
                            "dates": {"start": {"localDate": "2026-12-02", "localTime": "19:30:00"}},
                            "_embedded": {
                                "attractions": [{"name": "Sample Band"}],
                                "venues": [
                                    {
                                        "name": "Mohawk",
                                        "city": {"name": "Austin"},
                                        "state": {"stateCode": "TX"},
                                    }
                                ],
                            },
                        }
                    ]
                }
            }
        ).encode()
        shows = dp.shows_from_ticketmaster(body, "Sample Band", "2026-11-26", [], 90)
        self.assertEqual(shows[0]["url"], "https://www.ticketmaster.com/event/show-9")
        self.assertNotIn(secret, json.dumps(shows))

        forecast = f"https://user:{secret}@api.weather.gov/gridpoints/TOP/31,80/forecast"
        day = {
            "name": "Thursday",
            "isDaytime": True,
            "startTime": start.isoformat(),
            "temperature": 64,
            "temperatureUnit": "F",
            "shortForecast": "Fair",
            "detailedForecast": "Fair during the afternoon with a light breeze.",
        }
        formatted = dg.format_weather_item(day, None, forecast)
        self.assertEqual(formatted["url"], "https://api.weather.gov/gridpoints/TOP/31,80/forecast")
        self.assertNotIn(secret, json.dumps(formatted))

        seen = []

        def fetcher(url):
            seen.append(url)
            if url == dg.WEATHER_POINT:
                return json.dumps({"properties": {"forecast": forecast}}).encode()
            return json.dumps({"properties": {"periods": [day]}}).encode()

        weather_item = dg.weather(fetcher, "2026-11-26")
        self.assertEqual(seen[1], "https://api.weather.gov/gridpoints/TOP/31,80/forecast")
        self.assertNotIn(secret, json.dumps(weather_item))
        self.assertNotIn(secret, " ".join(seen))


if __name__ == "__main__":
    unittest.main()
