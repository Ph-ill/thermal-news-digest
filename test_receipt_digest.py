#!/usr/bin/env python3
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

# Fictional fixture location; set before importing the generator.
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


if __name__ == "__main__":
    unittest.main()
