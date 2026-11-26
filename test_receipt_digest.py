#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

# Fictional fixture location; set before importing the generator.
for _name in list(os.environ):
    if _name.startswith("DIGEST_SECTION") or _name in {
        "DIGEST_LLM_API_KEY",
        "DIGEST_READER_IDS",
        "DIGEST_SECTION_ORDER",
        "DIGEST_BOT_UPDATES_DIR",
        "DIGEST_BOT_UPDATES_STYLE",
        "DIGEST_HISTORY_PATH",
    }:
        os.environ.pop(_name, None)
os.environ["DIGEST_LLM_API_KEY"] = ""
os.environ.update(
    {
        "DIGEST_LOCATION_LABEL": "Bastrop, TX",
    }
)

import digest_generator as dg
import receipt_digest as rd
from PIL import Image, ImageDraw


def payload(headline_suffix: str = "") -> dict:
    sections = []
    for name in rd.EXPECTED_SECTIONS:
        headline = f"Current item for {name}{headline_suffix}"
        key = "headline" if name == "WEATHER" else "title"
        sections.append({"section": name, "source_feed": "https://example.com/feed", "items": [{key: headline, "summary": "Useful context and implications for this item, preserved in full so the receipt is a real digest rather than a headline list.", "url": "https://example.com/item", "published_at": "2026-09-15T12:00:00+00:00"}]})
    result = {
        "schema_version": "1.0",
        "digest_date": "2026-09-15",
        "location": "Bastrop, TX",
        "taste_profile": "fixture",
        "sections": sections,
        "source_errors": [],
        "generated_at": "2026-09-15T12:01:00+00:00",
        "credentials_in_label": False,
    }
    result["digest_id"] = rd.payload_digest_id(result)
    return result


class ReceiptDigestTests(unittest.TestCase):
    def render_payload(self, value: dict, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / "source.json"
        source.write_text(json.dumps(value), encoding="utf-8")
        return rd.render(source, directory / "output")

    def test_digest_id_ignores_generated_at_but_not_content(self):
        first = payload()
        second = json.loads(json.dumps(first))
        second["generated_at"] = "2026-09-15T12:09:00+00:00"
        self.assertEqual(rd.payload_digest_id(first), rd.payload_digest_id(second))
        second["sections"][0]["items"][0]["headline"] += " changed"
        self.assertNotEqual(rd.payload_digest_id(first), rd.payload_digest_id(second))

    def test_dynamic_height_expands_without_truncation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            short_value = payload()
            long_value = payload(" with a deliberately long continuation that must wrap onto as many lines as required and may never be ellipsized or clipped by a fixed page height")
            long_value["digest_id"] = rd.payload_digest_id(long_value)
            short = self.render_payload(short_value, root / "short")
            long = self.render_payload(long_value, root / "long")
            self.assertGreater(long["geometry"]["height_dots"], short["geometry"]["height_dots"])
            self.assertTrue(long["geometry"]["dynamic_height"])
            self.assertEqual(long["geometry"]["section_count"], len(rd.EXPECTED_SECTIONS))
            self.assertGreater(long["geometry"]["body_line_count"], short["geometry"]["body_line_count"])
            for metadata in (short, long):
                geometry = metadata["geometry"]
                self.assertFalse(geometry["clipping"])
                self.assertLessEqual(geometry["max_line_width_dots"], geometry["printable_text_width_dots"])
                self.assertLess(geometry["content_bottom_dots"], geometry["footer_y_dots"])
                self.assertEqual(geometry["height_dots"] % rd.DOTS_PER_MM, 0)
                self.assertGreater(metadata["raster_black_bbox"][0], 0)
                self.assertLess(metadata["raster_black_bbox"][2], rd.WIDTH_DOTS)
                self.assertGreaterEqual(geometry["text_volume_multiplier"], 1.5)

    def test_tspl_uses_spec_polarity_and_exact_dynamic_bitmap(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = self.render_payload(payload(), Path(temp))
            raw = Path(metadata["tspl"]).read_bytes()
            match = re.search(rb"BITMAP 0,0,(\d+),(\d+),0,", raw)
            self.assertIsNotNone(match)
            width_bytes, height = map(int, match.groups())
            trailer = b"\r\nPRINT 1,1\r\n"
            bitmap = raw[match.end():-len(trailer)]
            self.assertEqual(width_bytes, 80)
            self.assertEqual(height, metadata["geometry"]["height_dots"])
            self.assertEqual(len(bitmap), width_bytes * height)
            self.assertEqual(metadata["tspl_bitmap_mode"], 0)
            self.assertIn("bit 0 prints black", metadata["raster_polarity"])
            self.assertTrue(raw.endswith(trailer))

    def test_tspl_bit_polarity_is_white_one_black_zero(self):
        white = Image.new("L", (8, 1), 255)
        black = Image.new("L", (8, 1), 0)
        mixed = Image.new("L", (8, 1), 255)
        mixed.putpixel((0, 0), 0)
        self.assertEqual(rd.pack_tspl_bits(white), b"\xff")
        self.assertEqual(rd.pack_tspl_bits(black), b"\x00")
        self.assertEqual(rd.pack_tspl_bits(mixed), b"\x7f")

    def test_rejects_missing_or_reordered_sections(self):
        value = payload()
        value["sections"].reverse()
        value["digest_id"] = rd.payload_digest_id(value)
        with self.assertRaisesRegex(ValueError, "expected sections"):
            rd.validate_payload(value)

    def test_rejects_digest_id_mismatch(self):
        value = payload()
        value["sections"][0]["items"][0]["headline"] += " tampered"
        with self.assertRaisesRegex(ValueError, "does not match"):
            rd.validate_payload(value)

    def test_local_section_is_bastrop_and_companies_follow_it(self):
        value = payload()
        names = [section["section"] for section in value["sections"]]
        self.assertEqual(names, ["WEATHER", "GLOBAL", "BASTROP", "SPACEX & EARTH FUNERAL"])
        self.assertNotIn("EVENTS TODAY / SOON", names)
        self.assertNotIn("UK", names)
        self.assertNotIn("FILM/TV", names)

    def test_non_dict_section_item_is_rejected(self):
        value = payload()
        value["sections"][1]["items"] = ["not an item"]
        value["digest_id"] = rd.payload_digest_id(value)
        with self.assertRaisesRegex(ValueError, "not an object"):
            rd.validate_payload(value)

    def test_shows_section_can_be_omitted_and_a_present_one_is_in_order(self):
        omitted = payload()
        rd.validate_payload(omitted)
        with_shows = payload()
        companies = next(index for index, section in enumerate(with_shows["sections"]) if section["section"] == "SPACEX & EARTH FUNERAL")
        with_shows["sections"].insert(
            companies + 1,
            {
                "section": "NEARBY SHOWS",
                "source_feed": "https://app.ticketmaster.com/discovery/v2/events.json",
                "items": [
                    {
                        "title": "FRI DEC 18 · 7:30 PM · Sample Band",
                        "summary": "Mohawk, Austin.",
                        "url": "https://www.ticketmaster.com/event/show-1",
                        "published_at": "2026-12-18",
                    }
                ],
            },
        )
        with_shows["digest_id"] = rd.payload_digest_id(with_shows)
        rd.validate_payload(with_shows)
        with_shows["sections"].reverse()
        with_shows["digest_id"] = rd.payload_digest_id(with_shows)
        with self.assertRaisesRegex(ValueError, "expected sections"):
            rd.validate_payload(with_shows)

    def test_atomic_state_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            expected = {"version": 1, "digests": {"abc": {"status": "completed"}}}
            rd.atomic_json(state_path, expected)
            self.assertEqual(rd.read_state(state_path), expected)

    def test_optional_bot_section_layout_and_rejected_music(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plain = payload()
            without = self.render_payload(plain, root / "without")
            with_bots = payload()
            with_bots["sections"].append(
                {
                    "section": "GROK BOT UPDATES",
                    "source_feed": "bot-updates",
                    "items": [
                        {
                            "title": "Popcorn: Film picks",
                            "summary": "A quiet anime about a night courier opens Friday, and the review is the one worth making time for on a narrow receipt.",
                            "source_label": "Popcorn",
                            "published_at": "2026-11-26T04:40:00-06:00",
                        },
                        {
                            "title": "Listener: Morning queue (reader-a)",
                            "summary": "Three new songs from the usual corner of the shelf, none of them loud, queued before the day starts.",
                            "source_label": "Listener",
                            "url": "https://example.com/listener/morning",
                            "published_at": "2026-11-26T04:40:00-06:00",
                        },
                    ],
                }
            )
            with_bots["digest_id"] = rd.payload_digest_id(with_bots)
            rendered = self.render_payload(with_bots, root / "with")
            self.assertEqual(without["geometry"]["section_count"], len(rd.EXPECTED_SECTIONS))
            self.assertEqual(rendered["geometry"]["section_count"], len(rd.EXPECTED_SECTIONS) + 1)
            self.assertGreater(rendered["geometry"]["height_dots"], without["geometry"]["height_dots"])
            self.assertLessEqual(rendered["geometry"]["height_dots"], rd.MAX_HEIGHT_DOTS)
            bot = rendered["geometry"]["sections"][-1]
            self.assertEqual(bot["name"], "GROK BOT UPDATES")
            self.assertEqual(bot["items"][0]["source"], "Source: Popcorn")
            self.assertIsNone(bot["items"][0]["source_url"])
            self.assertEqual(bot["items"][1]["source"], "Source: Listener")
            self.assertNotIn("MUSIC", [section["name"] for section in rendered["geometry"]["sections"]])

        reordered = payload()
        reordered["sections"] = [reordered["sections"][-1], *reordered["sections"][:-1]]
        reordered["digest_id"] = rd.payload_digest_id(reordered)
        with self.assertRaisesRegex(ValueError, "expected sections"):
            rd.validate_payload(reordered)

        missing = payload()
        missing["sections"] = [section for section in missing["sections"] if section["section"] != "WEATHER"]
        missing["digest_id"] = rd.payload_digest_id(missing)
        with self.assertRaisesRegex(ValueError, "expected sections"):
            rd.validate_payload(missing)

        music = payload()
        music["sections"].append(
            {
                "section": "MUSIC",
                "source_feed": "https://example.com/music",
                "items": [
                    {
                        "title": "A song",
                        "summary": "This placeholder section is no longer part of the receipt.",
                        "url": "https://example.com/music",
                    }
                ],
            }
        )
        music["digest_id"] = rd.payload_digest_id(music)
        with self.assertRaisesRegex(ValueError, "expected sections"):
            rd.validate_payload(music)

        two_items = payload()
        news = next(section for section in two_items["sections"] if section["section"] == "GLOBAL")
        news["items"].append(
            {
                "title": "Second global item",
                "summary": "A second item fills the receipt when the bot section is absent, and it still has to stay inside the printable width.",
                "url": "https://example.com/global-2",
                "published_at": "2026-11-26T08:00:00+00:00",
            }
        )
        two_items["digest_id"] = rd.payload_digest_id(two_items)
        rd.validate_payload(two_items)

        empty = payload()
        empty["sections"][1]["items"] = []
        empty["digest_id"] = rd.payload_digest_id(empty)
        with self.assertRaisesRegex(ValueError, "invalid item count"):
            rd.validate_payload(empty)

    def test_custom_section_title_follows_env(self):
        renamed = payload()
        for section in renamed["sections"]:
            if section["section"] == "SPACEX & EARTH FUNERAL":
                section["section"] = "COMPANIES"
        renamed["digest_id"] = rd.payload_digest_id(renamed)
        with self.assertRaisesRegex(ValueError, "expected sections"):
            rd.validate_payload(renamed)
        with mock.patch.dict(os.environ, {"DIGEST_SECTION_TITLE_COMPANIES": "COMPANIES"}):
            rd.validate_payload(renamed)
            self.assertEqual(rd.settings.section_by_id("companies").title, "COMPANIES")

    def test_weather_icon_is_centered_on_the_forecast(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = self.render_payload(sample_payload(bots=False), Path(temp))
        icon = next(mark for mark in metadata["geometry"]["art"] if mark["type"] == "weather")
        weather = next(section for section in metadata["geometry"]["sections"] if section["name"] == "WEATHER")
        center = rd.weather_text_center(weather["lines"])
        self.assertIsNotNone(center)
        self.assertLessEqual(abs((icon["y"] + icon["size"] / 2) - center), 3)
        source = [line for line in weather["lines"] if line["kind"] == "source"]
        self.assertGreaterEqual(source[0]["y"], icon["y"])


def sample_story(section: str, index: int) -> dict:
    stories = {
        "WEATHER": [
            (
                "Thursday: high 64°F, low 41°F, 20% chance of precipitation. Partly cloudy",
                "Daytime stays dry until late afternoon, then a brief shower is possible. The wind stays light, and the evening cools quickly after sunset.",
            )
        ],
        "GLOBAL": [
            (
                "Harbor talks end with a written ceasefire and a date to reopen the port",
                "Mediators said both sides signed the ceasefire text before dawn and agreed to let aid ships dock on Friday. The port has been closed for nine days.",
            ),
            (
                "Research ship reports the first clear map of the new undersea ridge",
                "The crew finished the survey at dawn and will release the map after the instruments are checked. The ridge was not on last year's charts.",
            ),
        ],
        "BASTROP": [
            (
                "Bastrop brewery opens a taproom on Main Street this Friday",
                "The taproom pours local beer and keeps a small kitchen open until nine. There is no cover before the first set.",
            ),
            (
                "Smithville market adds a Saturday morning produce row",
                "The row opens at eight and closes at noon. Growers from Elgin and Cedar Creek are on the list this week.",
            ),
        ],
        "SPACEX & EARTH FUNERAL": [
            (
                "SpaceX sets a dawn Starship launch window",
                "The window opens at sunrise and lasts ninety minutes. The company said the flight is a fueling test.",
            ),
            (
                "Earth Funeral opens a soil transformation facility",
                "Earth Funeral said families can choose the facility next month. The note is about the building, not a stock price.",
            ),
        ],
    }
    title, summary = stories[section][index]
    if section == "WEATHER":
        return {
            "headline": title,
            "summary": summary,
            "url": "https://api.weather.gov/gridpoints/EXAMPLE/1,1/forecast",
            "published_at": "2026-11-26T12:00:00+00:00",
        }
    slug = {"GLOBAL": "global", "BASTROP": "bastrop", "SPACEX & EARTH FUNERAL": "companies"}[section]
    return {
        "title": title,
        "summary": summary,
        "url": f"https://example.com/{slug}-{index}",
        "published_at": "2026-11-26T12:00:00+00:00",
        "source_label": "Example Desk",
    }


def sample_payload(*, bots: bool) -> dict:
    sections = []
    for name in rd.EXPECTED_SECTIONS:
        count = 1 if bots or name == "WEATHER" else 2
        sections.append(
            {
                "section": name,
                "source_feed": "https://example.com/feed",
                "items": [sample_story(name, index) for index in range(count)],
            }
        )
    if not bots:
        sections.append(
            {
                "section": "NEARBY SHOWS",
                "source_feed": "https://app.ticketmaster.com/discovery/v2/events.json",
                "items": [
                    {
                        "title": "FRI DEC 18 · 7:30 PM · Sample Band",
                        "summary": "Mohawk, Austin.",
                        "url": "https://www.ticketmaster.com/event/example-show",
                        "published_at": "2026-12-18",
                    }
                ],
            }
        )
    if bots:
        sections.append(
            {
                "section": "GROK BOT UPDATES",
                "source_feed": "bot-updates",
                "items": [
                    {
                        "title": "Popcorn: Film picks",
                        "summary": "I found a quiet anime about a night courier. It opens Friday, and I think the review is the one worth your time.",
                        "source_label": "Popcorn",
                        "avatar": "popcorn",
                        "published_at": "2026-11-26T04:40:00-06:00",
                    },
                    {
                        "title": "Listener: Morning queue (reader-a)",
                        "summary": "I queued three new songs from the usual corner of the shelf. None of them are loud.",
                        "source_label": "Listener",
                        "avatar": "listener",
                        "published_at": "2026-11-26T04:40:00-06:00",
                    },
                ],
            }
        )
    result = {
        "schema_version": "1.0",
        "digest_date": "2026-11-26",
        "location": "Bastrop, TX",
        "taste_profile": "fixture",
        "sections": sections,
        "source_errors": [],
        "generated_at": "2026-11-26T11:00:00+00:00",
        "credentials_in_label": False,
    }
    result["digest_id"] = rd.payload_digest_id(result)
    return result


def portrait(path: Path, rgb: tuple[int, int, int]) -> None:
    """Light blob on a clear background, with about 12% padding so the ring shows."""
    image = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    tint = tuple(min(255, 180 + channel // 4) for channel in rgb)
    draw.ellipse((20, 20, 140, 140), fill=tint + (255,))
    draw.ellipse((58, 64, 76, 82), fill=(20, 20, 20, 255))
    draw.ellipse((96, 64, 114, 82), fill=(20, 20, 20, 255))
    draw.arc((66, 90, 106, 122), 20, 160, fill=(30, 20, 20, 255), width=4)
    image.save(path, format="PNG")


def write_sample_pngs(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = {}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        avatars = root / "avatars"
        avatars.mkdir()
        portrait(avatars / "popcorn.png", (220, 140, 60))
        for bots, name in ((False, "without-bot-updates"), (True, "with-bot-updates")):
            source = root / f"{name}.json"
            source.write_text(json.dumps(sample_payload(bots=bots), indent=2), encoding="utf-8")
            with mock.patch.dict(os.environ, {"DIGEST_AVATARS_DIR": str(avatars)}):
                metadata = rd.render(source, root / name)
            target = directory / f"{name}.png"
            target.write_bytes(Path(metadata["png"]).read_bytes())
            written[name] = target
    return written


def showcase_payload(weather_icon: str, headline: str, summary: str) -> dict:
    """Full receipt: four bot notes, one of them with no picture, and one weather icon.

    News sections stay at one item, the same crowding rule a real morning uses
    when bot notes are present.
    """
    value = sample_payload(bots=True)
    weather = value["sections"][0]["items"][0]
    weather["headline"] = headline
    weather["summary"] = summary
    weather["weather_icon"] = weather_icon
    value["sections"] = [section for section in value["sections"] if section["section"] != "GROK BOT UPDATES"]
    value["sections"].append(
        {
            "section": "GROK BOT UPDATES",
            "source_feed": "bot-updates",
            "items": [
                {
                    "title": "Popcorn: Film picks",
                    "summary": "I found a quiet anime about a night courier. It opens Friday, and I think the review is the one worth your time.",
                    "source_label": "Popcorn",
                    "avatar": "popcorn",
                },
                {
                    "title": "Listener: Morning queue (reader-a)",
                    "summary": "I queued three new songs from the usual corner of the shelf. None of them are loud, and I left them ready before the day starts.",
                    "source_label": "Listener",
                    "avatar": "listener",
                },
                {
                    "title": "Scout: Night watch",
                    "summary": "I walked the long way home and the street was empty. I will check the gate again after dark if you want me to.",
                    "source_label": "Scout",
                    "avatar": "scout",
                },
                {
                    "title": "Garden: Seedlings",
                    "summary": (
                        "I started the basil and the tomatoes on the kitchen sill. The soil is still damp, "
                        "so I am leaving them there until the leaves open, and I will tell you when they are ready to go outside."
                    ),
                    "source_label": "Garden",
                    "avatar": "garden",
                },
            ],
        }
    )
    value["digest_id"] = rd.payload_digest_id(value)
    return value


def write_showcase_pngs(directory: Path) -> dict[str, Path]:
    """Two full strips: a sunny day and a night storm, each with four bot notes."""
    directory.mkdir(parents=True, exist_ok=True)
    written = {}
    forecasts = (
        (
            "receipt-sunny-four-bots",
            "sun",
            "Thursday: high 70°F, low 48°F. Sunny",
            "Clear and mild through the afternoon, with a light breeze and no rain.",
        ),
        (
            "receipt-night-storm-four-bots",
            "night-storm",
            "Thursday night: low 55°F. Thunderstorms",
            "Storms move in after dark. Lightning is likely before midnight.",
        ),
    )
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        avatars = root / "avatars"
        avatars.mkdir()
        portrait(avatars / "popcorn.png", (220, 140, 60))
        portrait(avatars / "listener.png", (70, 120, 190))
        portrait(avatars / "garden.png", (70, 150, 80))
        for name, icon, headline, summary in forecasts:
            source = root / f"{name}.json"
            source.write_text(json.dumps(showcase_payload(icon, headline, summary), indent=2), encoding="utf-8")
            for style, suffix in (("bubble", ""), ("plain", "-plain")):
                with mock.patch.dict(
                    os.environ,
                    {"DIGEST_AVATARS_DIR": str(avatars), "DIGEST_BOT_UPDATES_STYLE": style},
                ):
                    metadata = rd.render(source, root / f"{name}{suffix}")
                target = directory / f"{name}{suffix}.png"
                target.write_bytes(Path(metadata["png"]).read_bytes())
                written[f"{name}{suffix}"] = target
    return written


if __name__ == "__main__":
    unittest.main()
