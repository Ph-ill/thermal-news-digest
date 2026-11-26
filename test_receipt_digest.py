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
        "DIGEST_HISTORY_PATH",
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
    }
)

import digest_generator as dg
import receipt_digest as rd
from PIL import Image


def payload(headline_suffix: str = "") -> dict:
    sections = []
    for name in rd.EXPECTED_SECTIONS:
        if name == "EVENTS TODAY / SOON":
            sections.append(
                {
                    "section": name,
                    "source_feed": dg.NEIGHBORHOOD_EVENTS_URL,
                    "source_feeds": [dg.NEIGHBORHOOD_EVENTS_URL, dg.CHAMBER_EVENTS_URL],
                    "window_start": "2026-09-15",
                    "window_end": "2026-10-06",
                    "window_days": 21,
                    "priority_order": ["Maple Grove", "Springfield area"],
                    "items": [
                        {
                            "title": f"TODAY · 5:30 PM-6:30 PM · Maple Grove HOA Meeting{headline_suffix}",
                            "summary": "Community Center, 100 Main Street, Springfield, ST 00000 · Maple Grove",
                            "url": "https://maplegrove.example.org/events/maple-grove-hoa-meeting-2/",
                            "source_id": "maplegrove.example.org:mec:2769",
                            "published_at": "2026-09-15T17:30:00-05:00",
                        },
                        {
                            "title": f"WED SEP 16 · 11:00 AM-1:00 PM · Toddler Time{headline_suffix}",
                            "summary": "15 Library Lane, Springfield, ST 00000 · Springfield area",
                            "url": "https://business.chamber.example.com/eventcalendar/Details/toddler-time-1677079",
                            "source_id": "business.chamber.example.com:event:1677079",
                            "published_at": "2026-09-16T11:00:00-05:00",
                        },
                    ],
                }
            )
            continue
        headline = f"Current item for {name}{headline_suffix}"
        key = "headline" if name == "WEATHER" else "title"
        sections.append({"section": name, "source_feed": "https://example.com/feed", "items": [{key: headline, "summary": "Useful context and implications for this item, preserved in full so the receipt is a real digest rather than a headline list.", "url": "https://example.com/item", "published_at": "2026-09-15T12:00:00+00:00"}]})
    result = {
        "schema_version": "1.0",
        "digest_date": "2026-09-15",
        "location": "near Springfield",
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
                self.assertGreaterEqual(geometry["text_volume_multiplier"], 2.0)

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

    def test_events_section_is_immediately_after_local_and_keeps_public_sources(self):
        value = payload()
        names = [section["section"] for section in value["sections"]]
        local_index = names.index("LOCAL")
        self.assertEqual(names[local_index + 1], "EVENTS TODAY / SOON")
        events = value["sections"][local_index + 1]["items"]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(item["source_id"] for item in events))
        self.assertTrue(all(item["url"].startswith("https://") for item in events))
        self.assertIn("Maple Grove", events[0]["title"] + events[0]["summary"])

    def test_event_source_priority_window_dates_times_and_locations(self):
        neighborhood_url = "https://maplegrove.example.org/events/maple-grove-hoa-meeting-2/"
        civic_url = "https://maplegrove.example.org/events/springfield-city-council-meeting/"
        chamber_url = "https://business.chamber.example.com/eventcalendar/Details/toddler-time-1677079?sourceTypeId=Website"

        def mec_event(name: str, date: str, start: str, end: str, location: str, url: str, event_id: str) -> str:
            value = {
                "@context": "http://schema.org",
                "@type": "Event",
                "startDate": date,
                "location": {"@type": "Place", "name": location, "address": location},
                "organizer": {"name": "Maple Grove" if "Maple Grove" in name else ""},
                "name": name,
                "url": url,
            }
            return (
                f"<script type='application/ld+json'>{json.dumps(value)}</script>"
                f"<article><a data-event-id='{event_id}'></a>"
                f"<span class='mec-start-time'>{start}</span>"
                f"<span class='mec-end-time'>{end}</span></article>"
            )

        neighborhood_html = (
            mec_event(
                "Springfield City Council Meeting",
                "2026-09-22",
                "6:30 pm",
                "7:30 pm",
                "Council Chambers - City Hall, Springfield, ST 00000",
                civic_url,
                "1120",
            )
            + mec_event(
                "Maple Grove HOA Meeting",
                "2026-10-05",
                "5:30 pm",
                "6:30 pm",
                "Community Center, 100 Main Street, Springfield, ST 00000",
                neighborhood_url,
                "2769",
            )
        ).encode()
        chamber_html = (
            f"<a href='{chamber_url}' class='gz-card-title gz-event-card-title'>Toddler Time</a>"
            "<meta itemprop='startDate' content='9/23/2026 11:00:00 AM'>"
            "<meta itemprop='endDate' content='9/23/2026 1:00:00 PM'>"
        ).encode()
        detail_html = (
            "<div class='card-text gz-event-address' itemprop='location'>"
            "<strong>Address</strong><br />15 Library Lane<br />Springfield, ST 00000"
            "</div>"
        ).encode()
        responses = {
            dg.NEIGHBORHOOD_EVENTS_URL: neighborhood_html,
            dg.CHAMBER_EVENTS_URL: chamber_html,
            chamber_url: detail_html,
        }

        section, errors = dg.build_events_section("2026-09-20", responses.__getitem__)
        self.assertEqual(errors, [])
        self.assertEqual(section["window_start"], "2026-09-20")
        self.assertEqual(section["window_end"], "2026-10-11")
        self.assertEqual(
            [item["source_scope"] for item in section["items"]],
            ["Maple Grove", "Springfield area", "Springfield area"],
        )
        self.assertIn("MON OCT 5 · 5:30 PM-6:30 PM", section["items"][0]["title"])
        self.assertIn("100 Main Street", section["items"][0]["location"])
        self.assertEqual(section["items"][0]["source_id"], "maplegrove.example.org:mec:2769")
        self.assertEqual(section["items"][1]["source_id"], "business.chamber.example.com:event:1677079")
        self.assertEqual(section["items"][2]["source_id"], "maplegrove.example.org:mec:1120")

    def test_unavailable_event_sources_are_explicit_and_do_not_invent_events(self):
        def unavailable(_url: str) -> bytes:
            raise OSError("offline fixture")

        section, errors = dg.build_events_section("2026-09-20", unavailable)
        self.assertEqual(len(errors), 2)
        self.assertEqual(len(section["items"]), 1)
        self.assertIn("No verified local events", section["items"][0]["title"])
        self.assertEqual(section["items"][0]["source_id"], "event-window-empty")

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
                    "section": "BOT UPDATES",
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
            self.assertEqual(bot["name"], "BOT UPDATES")
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
            if section["section"] == "SPACEX/SPACE STOCK":
                section["section"] = "MARKETS"
        renamed["digest_id"] = rd.payload_digest_id(renamed)
        with self.assertRaisesRegex(ValueError, "expected sections"):
            rd.validate_payload(renamed)
        with mock.patch.dict(os.environ, {"DIGEST_SECTION_TITLE_MARKETS": "MARKETS"}):
            rd.validate_payload(renamed)
            self.assertEqual(rd.settings.section_by_id("markets").title, "MARKETS")


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
        "LOCAL": [
            (
                "City council adopts the annual budget after cutting the proposed fee increase",
                "The vote funds the library's weekend hours and leaves the park tax unchanged. Crews start the sidewalk work on the south side next month.",
            ),
            (
                "River path stays closed through Friday while crews reset the washed-out railing",
                "Walkers can use the north entrance. The parks office said the railing should be back before the weekend market.",
            ),
        ],
        "SPACEX/SPACE STOCK": [
            (
                "Launch firm wins a civil contract to fly two science missions next spring",
                "The contract covers two launches and the ground support that goes with them. It is an operations story, not a price-target note.",
            ),
            (
                "Range schedule adds a dawn window for the cargo flight",
                "The window opens at sunrise and lasts ninety minutes. Weather officers said the wind, not the hardware, is the thing to watch.",
            ),
        ],
        "UK": [
            (
                "Rail board publishes the weekend map after finishing the bridge repairs",
                "Engineers reopened the main line after replacing the cracked span. The first weekend timetable is shorter than usual while they watch the new joints.",
            ),
            (
                "Museum opens the recovered archive to readers on Saturday morning",
                "Tickets are free and the reading room closes at noon. The letters have been out of public view for a decade.",
            ),
        ],
        "FILM/TV": [
            (
                "A new western series shoots in the desert and keeps the story small",
                "The series follows one sheriff and a traveling photographer. It opens in the spring and skips the usual superhero crossover.",
            ),
            (
                "An anime film about a night courier gets a Friday opening",
                "The story stays with one route and one city. Early reviews say it is quiet, strange, and worth the two hours.",
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
    slug = {"GLOBAL": "global", "LOCAL": "local", "SPACEX/SPACE STOCK": "markets", "UK": "uk", "FILM/TV": "film"}[section]
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
        if name == "EVENTS TODAY / SOON":
            sections.append(
                {
                    "section": name,
                    "source_feed": "https://maplegrove.example.org/events/",
                    "items": [
                        {
                            "title": "FRI NOV 27 · 6:00 PM-7:30 PM · Library reading night",
                            "summary": "Main Library, 15 Library Lane, Springfield · Springfield area",
                            "url": "https://maplegrove.example.org/events/reading-night/",
                            "source_id": "maplegrove.example.org:mec:3001",
                            "published_at": "2026-11-27T18:00:00-06:00",
                        },
                        {
                            "title": "SAT NOV 28 · 9:00 AM-11:00 AM · River path cleanup",
                            "summary": "River Park, Springfield · Springfield area",
                            "url": "https://business.chamber.example.com/eventcalendar/Details/river-cleanup-42",
                            "source_id": "business.chamber.example.com:event:42",
                            "published_at": "2026-11-28T09:00:00-06:00",
                        },
                    ],
                }
            )
            continue
        count = 1 if bots or name == "WEATHER" else 2
        sections.append(
            {
                "section": name,
                "source_feed": "https://example.com/feed",
                "items": [sample_story(name, index) for index in range(count)],
            }
        )
    if bots:
        sections.append(
            {
                "section": "BOT UPDATES",
                "source_feed": "bot-updates",
                "items": [
                    {
                        "title": "Popcorn: Film picks",
                        "summary": "A quiet anime about a night courier opens Friday. The review is the one worth making time for.",
                        "source_label": "Popcorn",
                        "published_at": "2026-11-26T04:40:00-06:00",
                    },
                    {
                        "title": "Listener: Morning queue (reader-a)",
                        "summary": "Three new songs from the usual corner of the shelf, none of them loud.",
                        "source_label": "Listener",
                        "published_at": "2026-11-26T04:40:00-06:00",
                    },
                ],
            }
        )
    result = {
        "schema_version": "1.0",
        "digest_date": "2026-11-26",
        "location": "near Springfield",
        "taste_profile": "fixture",
        "sections": sections,
        "source_errors": [],
        "generated_at": "2026-11-26T11:00:00+00:00",
        "credentials_in_label": False,
    }
    result["digest_id"] = rd.payload_digest_id(result)
    return result


def write_sample_pngs(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = {}
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for bots, name in ((False, "without-bot-updates"), (True, "with-bot-updates")):
            source = root / f"{name}.json"
            source.write_text(json.dumps(sample_payload(bots=bots), indent=2), encoding="utf-8")
            metadata = rd.render(source, root / name)
            target = directory / f"{name}.png"
            target.write_bytes(Path(metadata["png"]).read_bytes())
            written[name] = target
    return written


if __name__ == "__main__":
    unittest.main()
