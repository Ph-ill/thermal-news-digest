#!/usr/bin/env python3
"""Avatars, weather icons, and a receipt that uses both."""

import math
import os
from pathlib import Path
import re
import subprocess
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
        "DIGEST_BOT_UPDATES_STYLE",
        "DIGEST_HISTORY_PATH",
    }:
        os.environ.pop(_name, None)
os.environ["DIGEST_LLM_API_KEY"] = ""

import digest_picker as dp
import digest_settings as settings
import receipt_art as art
import receipt_digest as rd
import test_receipt_digest as fixtures
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
from test_digest_picker import bot_document


NOW = __import__("datetime").datetime(2026, 11, 26, 11, 0, tzinfo=__import__("datetime").timezone.utc)


def _black(image: Image.Image) -> int:
    histogram = image.histogram()
    return sum(histogram[:185])


def _dither_opaque_reference(image: Image.Image, size: int) -> Image.Image:
    """The pre-outline pipeline: square crop, contrast, Floyd-Steinberg. No ring."""
    fitted = art._cover_square(image.convert("RGB"), size)
    gray = ImageOps.autocontrast(ImageOps.grayscale(fitted), cutoff=1)
    gray = ImageEnhance.Contrast(gray).enhance(1.85)
    return gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")


def _balloon_fill_mask(svg_text: str, width: int, height: int, directory: Path) -> Image.Image:
    paths = re.findall(
        rf'<path d="([^"]+)" fill="white" stroke="black" stroke-width="{art.BUBBLE_STROKE}"',
        svg_text,
    )
    if not paths:
        raise AssertionError("rendered svg has no balloon path")
    body = "".join(f'<path d="{path}" fill="black"/>' for path in paths)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="white"/>{body}</svg>'
    )
    svg_path = directory / "balloon-fill.svg"
    png_path = directory / "balloon-fill.png"
    svg_path.write_text(svg, encoding="utf-8")
    subprocess.run(
        ["rsvg-convert", "-w", str(width), "-h", str(height), "-o", str(png_path), str(svg_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return Image.open(png_path).convert("L")


def _assert_names_under_avatars(test: unittest.TestCase, marks: list) -> None:
    names = sorted((mark for mark in marks if mark["type"] == "name"), key=lambda mark: mark["y"])
    faces = sorted((mark for mark in marks if mark["type"] in {"avatar", "fallback"}), key=lambda mark: mark["y"])
    test.assertEqual(len(names), len(faces))
    test.assertGreaterEqual(len(names), 1)
    for name, face in zip(names, faces):
        size = face.get("size", art.AVATAR_PX)
        test.assertGreaterEqual(name["y"], face["y"] + size)
        face_center = face["x"] + size / 2
        name_center = name["x"] + name["w"] / 2
        test.assertLessEqual(abs(name_center - face_center), 2)
        test.assertLessEqual(name["x"] + name["w"], face["x"] + size + 2)
    for name, later in zip(names, faces[1:]):
        test.assertLessEqual(name["y"] + name["h"], later["y"])


def _assert_balloon_ink(test: unittest.TestCase, image: Image.Image, svg_text: str, bubbles: list, directory: Path) -> None:
    """White rows between strokes, and text ink inset from the balloon stroke."""
    width, height = image.size
    mask = _balloon_fill_mask(svg_text, width, height, directory)
    filled = mask.point(lambda value: 255 if value < 128 else 0)
    allowed = filled.filter(ImageFilter.MaxFilter(art.BUBBLE_STROKE * 2 + 1))
    # 4 dots clears the centered stroke. 18 dots is the air the words must keep.
    past_stroke = filled.filter(ImageFilter.MinFilter(art.BUBBLE_STROKE * 2 + 1))
    deep = filled.filter(ImageFilter.MinFilter(18 * 2 + 1))
    page = image.tobytes()
    keep = allowed.tobytes()
    inner = past_stroke.tobytes()
    core = deep.tobytes()
    for bubble in bubbles:
        x0 = max(0, bubble["x"] - art.TAIL_W - art.BUBBLE_STROKE)
        y0 = max(0, bubble["y"] - art.BUBBLE_STROKE)
        x1 = min(width, bubble["x"] + bubble["w"] + art.BUBBLE_STROKE)
        y1 = min(height, bubble["y"] + bubble["h"] + art.BUBBLE_STROKE)
        outside = 0
        crowded = 0
        text = 0
        for y in range(y0, y1):
            row = y * width
            for x in range(max(0, bubble["x"]), min(width, bubble["x"] + bubble["w"])):
                index = row + x
                if inner[index] < 128:
                    continue
                if page[index] >= 185:
                    continue
                if core[index] < 128:
                    crowded += 1
                else:
                    text += 1
            for x in range(x0, x1):
                if page[row + x] < 185 and keep[row + x] < 128:
                    outside += 1
        test.assertEqual(outside, 0)
        test.assertEqual(crowded, 0)
        test.assertGreater(text, 20)
    ordered = sorted(bubbles, key=lambda mark: mark["y"])
    for earlier, later in zip(ordered, ordered[1:]):
        x0 = earlier["x"]
        x1 = earlier["x"] + earlier["w"]
        y0 = earlier["y"] + earlier["h"]
        y1 = later["y"]
        best = run = 0
        span = x1 - x0
        for y in range(y0, y1):
            row = page[y * width + x0 : y * width + x1]
            if len(row) == span and all(pixel >= 185 for pixel in row):
                run += 1
                if run > best:
                    best = run
            else:
                run = 0
        test.assertGreaterEqual(best, 12)


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
            ("Mostly Cloudy then Sunny", "", False, "cloud"),
            ("Sunny then Mostly Cloudy", "", False, "sun"),
            ("Sunday", "", False, None),
            ("Sunday: high 70°F. Mostly Cloudy", "", False, "cloud"),
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
        sunday = {"headline": "Sunday: high 70°F. Mostly Cloudy"}
        self.assertEqual(art.icon_for_item(sunday), "cloud")
        self.assertIsNone(art.icon_for_item({"title": "Sunday"}))
        self.assertEqual(art.icon_for_item({"headline": "Mostly Cloudy then Sunny"}), "cloud")

    def test_bubble_tail_points_at_the_avatar_side(self):
        path = art.bubble_path(200, 40, 380, 160, 120)
        self.assertTrue(path.startswith("M"))
        self.assertTrue(path.endswith("Z"))
        self.assertIn("C", path)
        self.assertIn("Q", path)
        tip_x = 200 - art.TAIL_W
        self.assertIn(f"{tip_x},120", path)
        self.assertLess(tip_x, 200)
        self.assertGreaterEqual(art.AVATAR_PX, 128)
        self.assertLessEqual(art.AVATAR_PX, 144)
        self.assertGreaterEqual(art.BUBBLE_PAD_Y, 24)
        self.assertGreaterEqual(art.NOTE_GAP, 24)
        self.assertGreaterEqual(art.BUBBLE_STROKE, 4)
        _origin, _width, text_width = rd._bubble_origin()
        self.assertGreaterEqual(text_width, 340)


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
                    self.assertEqual(avatars_drawn[0]["size"], art.AVATAR_PX)
                    self.assertEqual([mark["letter"] for mark in fallbacks], ["S"])
                    bubbles_by_y = sorted(bubbles, key=lambda mark: mark["y"])
                    for earlier, later in zip(bubbles_by_y, bubbles_by_y[1:]):
                        self.assertGreaterEqual(later["y"] - (earlier["y"] + earlier["h"]), art.NOTE_GAP)
                    self.assertEqual(weather[0]["icon"], icon)
                    svg = Path(metadata["svg"]).read_text(encoding="utf-8")
                    self.assertIn(f'stroke-width="{art.BUBBLE_STROKE}"', svg)
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
                    self.assertNotIn("(reader", svg)
                    self.assertNotIn("Listener:", svg)
                    _assert_names_under_avatars(self, metadata["geometry"]["art"])
                    _assert_balloon_ink(self, image, svg, bubbles, root)
                    # The long garden note has to wrap inside the bubble, so that bubble is taller than the avatar.
                    self.assertGreater(bubbles[-1]["h"], art.AVATAR_PX)
            self.assertNotEqual(icons[0], icons[1])
            self.assertLess(max(heights), rd.MAX_HEIGHT_DOTS)

    def test_four_max_length_notes_stay_under_the_length_cap(self):
        sentence = "I am writing a long first person note so the bubble has to wrap this line. "
        text = (sentence * 8)[:360]
        self.assertEqual(len(text), 360)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            value = fixtures.showcase_payload(
                "sun",
                "Thursday: high 70°F, low 48°F. Sunny",
                "Clear and mild through the afternoon.",
            )
            for item in value["sections"][-1]["items"]:
                item["summary"] = text
            value["digest_id"] = rd.payload_digest_id(value)
            metadata = fixtures.ReceiptDigestTests().render_payload(value, root / "long")
            self.assertLessEqual(metadata["geometry"]["height_dots"], rd.MAX_HEIGHT_DOTS)
            self.assertFalse(metadata["geometry"]["clipping"])
            bubbles = [mark for mark in metadata["geometry"]["art"] if mark["type"] == "bubble"]
            self.assertEqual(len(bubbles), 4)
            _origin, bubble_w, text_width = rd._bubble_origin()
            self.assertGreaterEqual(text_width, 340)
            self.assertLess(bubble_w, rd.WIDTH_DOTS)
            self.assertGreater(bubbles[-1]["h"], art.AVATAR_PX)

    def test_transparent_sprite_gains_a_ring_and_opaque_rgb_is_unchanged(self):
        canvas = Image.new("RGBA", (144, 144), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((8, 8, 136, 136), fill=(255, 255, 255, 255))
        draw.ellipse((48, 52, 66, 70), fill=(0, 0, 0, 255))
        draw.ellipse((84, 52, 102, 70), fill=(0, 0, 0, 255))
        dithered = art.dither_for_thermal(canvas, art.AVATAR_PX)
        ink = 1 - (dithered.histogram()[255] / (art.AVATAR_PX * art.AVATAR_PX))
        self.assertGreater(ink, 0.08)
        pixels = dithered.load()
        center = art.AVATAR_PX // 2
        hits = 0
        for step in range(36):
            angle = step * math.tau / 36
            for radius in range(art.AVATAR_PX // 2 - 1, art.AVATAR_PX // 3, -1):
                x = int(round(center + radius * math.cos(angle)))
                y = int(round(center + radius * math.sin(angle)))
                if pixels[x, y] == 0:
                    hits += 1
                    break
        self.assertEqual(hits, 36)

        opaque = Image.new("RGB", (80, 60), (180, 40, 40))
        ImageDraw.Draw(opaque).rectangle((10, 10, 50, 40), fill=(20, 20, 20))
        self.assertEqual(
            art.dither_for_thermal(opaque, 48).tobytes(),
            _dither_opaque_reference(opaque, 48).tobytes(),
        )

    def test_printed_name_is_the_first_word_under_the_picture(self):
        self.assertEqual(art.bot_display_name("Popcorn · Movie Goblin"), "Popcorn")
        self.assertEqual(art.bot_display_name("Night Owl"), "Night")
        self.assertEqual(art.bot_topic("Listener: Morning queue (reader-a)"), "Morning queue")
        self.assertEqual(settings.bot_updates_style(), "bubble")
        with mock.patch.dict(os.environ, {"DIGEST_BOT_UPDATES_STYLE": "comic"}):
            self.assertEqual(settings.bot_updates_style(), "bubble")
        with mock.patch.dict(os.environ, {"DIGEST_BOT_UPDATES_STYLE": " PLAIN "}):
            self.assertEqual(settings.bot_updates_style(), "plain")

    def test_plain_style_centers_a_dash_between_notes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            avatars = root / "avatars"
            avatars.mkdir()
            fixtures.portrait(avatars / "popcorn.png", (220, 140, 60))
            value = fixtures.showcase_payload(
                "sun",
                "Thursday: high 70°F, low 48°F. Sunny",
                "Clear and mild through the afternoon.",
            )
            with mock.patch.dict(
                os.environ,
                {"DIGEST_AVATARS_DIR": str(avatars), "DIGEST_BOT_UPDATES_STYLE": "plain"},
            ):
                metadata = fixtures.ReceiptDigestTests().render_payload(value, root / "plain")
            marks = metadata["geometry"]["art"]
            self.assertEqual([mark for mark in marks if mark["type"] == "bubble"], [])
            notes = [mark for mark in marks if mark["type"] == "note"]
            separators = [mark for mark in marks if mark["type"] == "separator"]
            self.assertEqual(len(notes), 4)
            self.assertEqual(len(separators), 3)
            page_center = rd.WIDTH_DOTS / 2
            for earlier, dash, later in zip(notes, separators, notes[1:]):
                self.assertEqual(dash["y"], earlier["bottom"] + (later["top"] - earlier["bottom"]) // 2)
                self.assertEqual(dash["y"], earlier["bottom"] + art.SEPARATOR_GAP // 2)
                self.assertLess(earlier["bottom"], dash["y"])
                self.assertLess(dash["y"] + 2, later["top"])
                self.assertEqual(dash["x"] + dash["w"] / 2, page_center)
            svg = Path(metadata["svg"]).read_text(encoding="utf-8")
            self.assertIn('stroke-dasharray="8 6"', svg)
            self.assertNotIn('stroke-linejoin="round"', svg)
            _assert_names_under_avatars(self, marks)


if __name__ == "__main__":
    unittest.main()
