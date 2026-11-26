#!/usr/bin/env python3
"""Avatars, weather icons, and a receipt that uses both."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

for _name in list(os.environ):
    if _name.startswith("DIGEST_SECTION") or _name in {
        "DIGEST_LLM_API_KEY",
        "DIGEST_READER_IDS",
        "DIGEST_BOT_UPDATES_DIR",
        "DIGEST_AVATARS_DIR",
        "DIGEST_AVATAR_THRESHOLD",
        "DIGEST_HISTORY_PATH",
    }:
        os.environ.pop(_name, None)
os.environ["DIGEST_LLM_API_KEY"] = ""

import digest_picker as dp
import digest_settings as settings
import receipt_art as art
import receipt_digest as rd
import test_receipt_digest as fixtures
from PIL import Image
from test_digest_picker import bot_document


NOW = __import__("datetime").datetime(2026, 11, 26, 11, 0, tzinfo=__import__("datetime").timezone.utc)


def _black(image: Image.Image) -> int:
    histogram = image.histogram()
    return sum(histogram[:185])


class AvatarFileTests(unittest.TestCase):
    def test_missing_avatar_falls_back_and_bad_files_are_rejected(self):
        self.assertIsNone(art.load_avatar(Path("/no/such/avatars"), "popcorn"))
        self.assertIsNone(art.load_avatar(Path("."), "../popcorn"))
        self.assertIsNone(art.load_avatar(Path("."), "popcorn/x"))
        self.assertIsNone(art.load_avatar(Path("."), ""))
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.assertIsNone(art.load_avatar(directory, "scout"))

            target = directory / "popcorn.png"
            target.write_text("this is not an image", encoding="utf-8")
            self.assertIsNone(art.load_avatar(directory, "popcorn"))

            target.write_bytes(b"x" * (art.AVATAR_MAX_BYTES + 8))
            self.assertIsNone(art.load_avatar(directory, "popcorn"))

            target.unlink()
            target.mkdir()
            self.assertIsNone(art.load_avatar(directory, "popcorn"))
            target.rmdir()

            real = directory / "real.png"
            Image.new("RGB", (48, 48), (20, 20, 20)).save(real)
            target.symlink_to(real)
            self.assertIsNone(art.load_avatar(directory, "popcorn"))
            target.unlink()

            fifo = directory / "listener.png"
            os.mkfifo(fifo)
            self.assertIsNone(art.load_avatar(directory, "listener"))
            fifo.unlink()

            bomb = directory / "garden.png"
            Image.new("RGB", (2000, 2000), (10, 80, 10)).save(bomb)
            self.assertLess(bomb.stat().st_size, art.AVATAR_MAX_BYTES)
            self.assertGreater(2000 * 2000, art.AVATAR_MAX_PIXELS)
            self.assertIsNone(art.load_avatar(directory, "garden"))
            bomb.unlink()

            broken = directory / "popcorn.png"
            broken.write_bytes(b"not-a-png")
            photo = directory / "popcorn.jpg"
            Image.new("RGB", (90, 40), (200, 80, 40)).save(photo, format="JPEG")
            loaded = art.load_avatar(directory, "popcorn")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.size, (art.AVATAR_PX, art.AVATAR_PX))
            self.assertEqual(loaded.mode, "L")
            histogram = loaded.histogram()
            self.assertEqual(sum(histogram[1:255]), 0)
            self.assertGreater(histogram[0], 0)

    def test_threshold_shifts_a_flat_gray_toward_black(self):
        gray = Image.new("RGB", (80, 80), (100, 100, 100))
        light = art.dither_for_thermal(gray, art.AVATAR_PX, threshold=40)
        dark = art.dither_for_thermal(gray, art.AVATAR_PX, threshold=220)
        self.assertGreater(_black(dark), _black(light))

    def test_optional_avatar_key_stays_backward_compatible(self):
        items = dp.validate_bot_document(bot_document(), now=NOW)
        self.assertEqual(items[0]["avatar"], "popcorn")
        named = bot_document(
            updates=[
                {
                    "bot": "Night Owl",
                    "topic": "Late note",
                    "text": "I left the porch light on and I will turn it off at midnight.",
                    "for": "both",
                    "avatar": "night-owl",
                }
            ]
        )
        self.assertEqual(dp.validate_bot_document(named, now=NOW)[0]["avatar"], "night-owl")
        self.assertEqual(dp.slugify_bot("Night Owl"), "night-owl")
        for bad in ("../x", "Popcorn", "a/b", "", "has space", "a" * 41, 3, "white space"):
            document = bot_document(
                updates=[
                    {
                        "bot": "Popcorn",
                        "topic": "Film picks",
                        "text": "I found a quiet anime about a night courier.",
                        "for": "both",
                        "avatar": bad,
                    }
                ]
            )
            with self.assertRaises(dp.BotUpdatesError):
                dp.validate_bot_document(document, now=NOW)
        extra = bot_document(
            updates=[
                {
                    "bot": "Popcorn",
                    "topic": "Film picks",
                    "text": "I found a quiet anime about a night courier.",
                    "for": "both",
                    "note": "nope",
                }
            ]
        )
        with self.assertRaises(dp.BotUpdatesError):
            dp.validate_bot_document(extra, now=NOW)

    def test_heading_env_still_overrides_the_printed_name(self):
        self.assertEqual(settings.section_by_id("bot_updates").title, "GROK BOT UPDATES")
        with mock.patch.dict(os.environ, {"DIGEST_SECTION_TITLE_BOT_UPDATES": "BOT UPDATES"}):
            self.assertEqual(settings.section_by_id("bot_updates").title, "BOT UPDATES")
        self.assertEqual(settings.avatars_dir().name, "avatars")
        with mock.patch.dict(os.environ, {"DIGEST_AVATARS_DIR": "/tmp/faces", "DIGEST_AVATAR_THRESHOLD": "180"}):
            self.assertEqual(str(settings.avatars_dir()), "/tmp/faces")
            self.assertEqual(settings.avatar_threshold(), 180)
        self.assertIsNone(settings.avatar_threshold())


class WeatherIconTests(unittest.TestCase):
    def test_short_forecast_and_icon_url_map_to_a_picture(self):
        day = "https://api.weather.gov/icons/land/day/{code}?size=medium"
        night = "https://api.weather.gov/icons/land/night/{code}?size=medium"
        cases = (
            ("Sunny", "", False, "sun"),
            ("Clear", "", True, "night-clear"),
            ("Mostly Clear", night.format(code="few"), True, "night-partly"),
            ("Partly Cloudy", day.format(code="sct"), False, "partly"),
            ("Mostly Cloudy", "", False, "cloud"),
            ("Mostly Sunny", "", False, "sun"),
            ("Chance Rain Showers", day.format(code="rain_showers"), False, "rain"),
            ("Partly cloudy then Chance Rain", "", False, "rain"),
            ("Slight Chance Showers And Thunderstorms", night.format(code="tsra"), True, "night-storm"),
            ("Chance Snow Showers", "", False, "snow"),
            ("Patchy Fog", "", False, "fog"),
            ("Breezy", day.format(code="wind_skc"), False, "wind"),
            ("Sunny and breezy", "", False, "sun"),
            ("Hot", "", False, "sun"),
            ("Blizzard Warning", "", False, "snow"),
            ("", "", False, None),
            ("No daytime forecast for 2026-11-26", "", False, None),
        )
        for short, icon, nighttime, expected in cases:
            found = art.classify_weather(short, icon, nighttime=nighttime)
            self.assertEqual(found, expected, short or icon)
        sunny = art.draw_weather_icon("sun")
        storm = art.draw_weather_icon("night-storm")
        self.assertEqual(sunny.size, (art.WEATHER_ICON_PX, art.WEATHER_ICON_PX))
        self.assertNotEqual(sunny.tobytes(), storm.tobytes())
        self.assertGreater(_black(sunny), 0)
        self.assertGreater(_black(storm), 0)
        for kind in art.WEATHER_ICONS:
            icon = art.draw_weather_icon(kind)
            self.assertEqual(icon.mode, "L")
            self.assertGreater(_black(icon), 0)
            self.assertLess(_black(icon), icon.size[0] * icon.size[1])

    def test_bubble_tail_points_at_the_avatar_side(self):
        path = art.bubble_path(120, 40, 400, 80, 76)
        self.assertIn("L106,76", path)
        self.assertTrue(path.startswith("M"))


class ReceiptArtRenderTests(unittest.TestCase):
    def test_four_updates_render_with_a_fallback_and_two_weather_icons(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            avatars = root / "avatars"
            avatars.mkdir()
            fixtures.portrait(avatars / "popcorn.png", (220, 140, 60))
            fixtures.portrait(avatars / "listener.png", (70, 120, 190))
            fixtures.portrait(avatars / "garden.png", (70, 150, 80))
            heights = []
            icons = []
            with mock.patch.dict(os.environ, {"DIGEST_AVATARS_DIR": str(avatars)}):
                for icon, headline in (
                    ("sun", "Thursday: high 70°F, low 48°F. Sunny"),
                    ("night-storm", "Thursday night: low 55°F. Thunderstorms"),
                ):
                    value = fixtures.showcase_payload(
                        icon,
                        headline,
                        "The detailed forecast stays inside the summary budget and wraps on the strip.",
                    )
                    metadata = fixtures.ReceiptDigestTests().render_payload(value, root / icon)
                    heights.append(metadata["geometry"]["height_dots"])
                    icons.append(metadata["geometry"]["art"])
                    self.assertLess(metadata["geometry"]["height_dots"], 3600)
                    self.assertLessEqual(metadata["geometry"]["height_dots"], rd.MAX_HEIGHT_DOTS)
                    self.assertFalse(metadata["geometry"]["clipping"])
                    self.assertLessEqual(
                        metadata["geometry"]["max_line_width_dots"],
                        metadata["geometry"]["printable_text_width_dots"],
                    )
                    bot = metadata["geometry"]["sections"][-1]
                    self.assertEqual(bot["name"], "GROK BOT UPDATES")
                    self.assertEqual(len(bot["items"]), 4)
                    self.assertEqual(bot["items"][2]["source"], "Source: Scout")
                    bubbles = [mark for mark in metadata["geometry"]["art"] if mark["type"] == "bubble"]
                    avatars_drawn = [mark for mark in metadata["geometry"]["art"] if mark["type"] == "avatar"]
                    fallbacks = [mark for mark in metadata["geometry"]["art"] if mark["type"] == "fallback"]
                    weather = [mark for mark in metadata["geometry"]["art"] if mark["type"] == "weather"]
                    self.assertEqual(len(bubbles), 4)
                    self.assertEqual(len(avatars_drawn), 3)
                    self.assertEqual([mark["letter"] for mark in fallbacks], ["S"])
                    self.assertEqual(weather[0]["icon"], icon)
                    svg = Path(metadata["svg"]).read_text(encoding="utf-8")
                    self.assertIn("GROK BOT UPDATES", svg)
                    self.assertIn(">S<", svg)
                    self.assertEqual(svg.count("<circle "), 1)
                    image = Image.open(metadata["png"]).convert("L")
                    self.assertEqual(image.size[0], rd.WIDTH_DOTS)
                    avatar = avatars_drawn[0]
                    crop = image.crop((avatar["x"], avatar["y"], avatar["x"] + avatar["size"], avatar["y"] + avatar["size"]))
                    self.assertGreater(_black(crop), 20)
                    icon_mark = weather[0]
                    icon_crop = image.crop(
                        (
                            icon_mark["x"],
                            icon_mark["y"],
                            icon_mark["x"] + icon_mark["size"],
                            icon_mark["y"] + icon_mark["size"],
                        )
                    )
                    self.assertGreater(_black(icon_crop), 10)
                    # The long garden note has to wrap inside the bubble, so that bubble is taller than the avatar.
                    self.assertGreater(bubbles[-1]["h"], art.AVATAR_PX)
            self.assertNotEqual(icons[0], icons[1])
            self.assertLess(max(heights), rd.MAX_HEIGHT_DOTS // 2)


if __name__ == "__main__":
    unittest.main()
