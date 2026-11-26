#!/usr/bin/env python3
"""Failure cases that must still put a receipt on paper."""

from email.message import Message
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
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
        self.assertGreaterEqual(len(payload["sections"]), 7)
        self.assertNotIn("taste_profile", payload)
        self.assertTrue(payload["digest_id"])


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
                    "DIGEST_UK_FEED": base + "/rss/uk",
                    "DIGEST_FILM_FEED": base + "/rss/film",
                    "DIGEST_MARKETS_FEED": base + "/rss/markets",
                    "DIGEST_WEATHER_LAT": "0",
                    "DIGEST_WEATHER_LON": "0",
                    "DIGEST_NEIGHBORHOOD_EVENTS_URL": base + "/events",
                    "DIGEST_CHAMBER_EVENTS_URL": base + "/chamber",
                    "DIGEST_LOCATION_LABEL": "near Springfield",
                    "DIGEST_NEIGHBORHOOD_NAME": "Neighborhood",
                    "DIGEST_LOCAL_CITY": "Springfield",
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


if __name__ == "__main__":
    unittest.main()
