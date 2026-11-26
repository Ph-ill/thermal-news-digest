#!/usr/bin/env python3
"""Render and submit a true dynamic-height 80 mm TSPL2 news receipt."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from PIL import Image, ImageFont

import digest_picker
import digest_settings as settings
import receipt_art

WIDTH_DOTS = 640
DOTS_PER_MM = 8
WIDTH_MM = 80
LEFT = 28
RIGHT = 28
TOP = 24
BOTTOM = 36
BORDER = 12
TITLE_FONT_SIZE = 30
SUBTITLE_FONT_SIZE = 18
SECTION_FONT_SIZE = 17
BODY_FONT_SIZE = 22
DETAIL_FONT_SIZE = 17
SOURCE_FONT_SIZE = 15
SMALL_FONT_SIZE = 17
BODY_LINE_HEIGHT = 30
DETAIL_LINE_HEIGHT = 23
SOURCE_LINE_HEIGHT = 21
SECTION_BAR_HEIGHT = 30
MAX_HEIGHT_DOTS = 6000
# Informational baseline from an early short receipt. A short morning still prints.
PRIOR_RECEIPT_BODY_CHARS = 435
HEADLINE_CHAR_CAP = 320
DETAIL_CHAR_CAP = 480
SOURCE_MAX_BYTES = 2_000_000
FONT_REGULAR = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
FONT_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")
PROJECT_DIR = Path(os.environ.get("DIGEST_HOME") or Path(__file__).resolve().parent)
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"
DEFAULT_STATE = PROJECT_DIR / "state.json"
DEFAULT_QUEUE = os.environ.get("DIGEST_PRINTER_QUEUE", "receipt")
STATE_MAX_BYTES = 1_048_576
# Required titles in the default order, captured at import for existing callers.
# validate_payload reads the live ordered list so optional sections can be absent
# and a deployment can rename headers without editing this file.
EXPECTED_SECTIONS = settings.required_titles()


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def normalize_text(text: str) -> str:
    text = digest_picker.xml_text(text or "")
    substitutions = {
        "☀️": "",
        "☀": "",
        "⚠️": "!",
        "⚠": "!",
        "—": "-",
        "–": "-",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "…": "...",
        "·": "-",
        "&nbsp;": " ",
    }
    for old, new in substitutions.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def split_long_token(token: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for char in token:
        candidate = current + char
        if current and font.getlength(candidate) > max_width:
            pieces.append(current)
            current = char
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def wrap_pixels(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words: list[str] = []
    for token in normalize_text(text).split():
        if font.getlength(token) <= max_width:
            words.append(token)
        else:
            words.extend(split_long_token(token, font, max_width))
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if current and font.getlength(candidate) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or ["No update"]


def payload_digest_id(payload: dict) -> str:
    stable = {key: value for key, value in payload.items() if key not in {"generated_at", "digest_id"}}
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _order_error(expected: tuple[str, ...], names: tuple[str, ...]) -> ValueError:
    return ValueError(
        f"expected sections {expected!r} in order (optional may be omitted), got {names!r}"
    )


def check_section_order(names: list[object]) -> None:
    specs = settings.sections()
    expected = tuple(spec.title for spec in specs)
    optional = {spec.title for spec in specs if spec.optional}
    if len(expected) != len(set(expected)):
        raise ValueError("expected sections are not unique")
    position = 0
    for name in names:
        if not isinstance(name, str) or name not in expected:
            raise _order_error(expected, tuple(names))
        while position < len(expected) and expected[position] != name:
            if expected[position] not in optional:
                raise _order_error(expected, tuple(names))
            position += 1
        if position >= len(expected):
            raise _order_error(expected, tuple(names))
        position += 1
    while position < len(expected):
        if expected[position] not in optional:
            raise _order_error(expected, tuple(names))
        position += 1


def validate_payload(payload: dict) -> None:
    if payload.get("schema_version") != "1.0":
        raise ValueError("unsupported news digest schema")
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("news digest sections are missing")
    names = [section.get("section") if isinstance(section, dict) else None for section in sections]
    check_section_order(names)
    specs = {spec.title: spec for spec in settings.sections()}
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("news digest section is not an object")
        spec = specs[section["section"]]
        items = section.get("items")
        if spec.kind == "shows":
            allowed = range(1, 4)
        elif spec.kind == "bot_updates":
            allowed = range(1, settings.max_bot_updates() + 1)
        else:
            allowed = range(1, settings.max_news_items() + 1)
        if not isinstance(items, list) or len(items) not in allowed:
            raise ValueError(
                f"section {section.get('section')} has invalid item count {len(items) if isinstance(items, list) else None}"
            )
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"section {section.get('section')} item is not an object")
            if not normalize_text(str(item.get("title") or item.get("headline") or "")):
                raise ValueError(f"section {section.get('section')} has no printable headline")
            if not normalize_text(str(item.get("summary") or item.get("rationale") or "")):
                raise ValueError(f"section {section.get('section')} has no printable detail")
            if spec.kind == "bot_updates":
                if not normalize_text(str(item.get("source_label") or "")) and not item.get("url"):
                    raise ValueError(f"section {section.get('section')} has no source label")
            elif not item.get("url"):
                raise ValueError(f"section {section.get('section')} has no source URL")
            if spec.kind == "shows" and not item.get("url"):
                raise ValueError("show item has no public event URL")
    expected = payload_digest_id(payload)
    if payload.get("digest_id") != expected:
        raise ValueError("news digest ID does not match its stable payload")


def source_line(item: dict) -> str:
    label = normalize_text(str(item.get("source_label") or ""))
    if label:
        return f"Source: {label}"
    host = (urlparse(str(item.get("url") or "")).hostname or "source").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"Source: {host}"


def detail_text(item: dict, headline: str) -> str:
    summary = normalize_text(str(item.get("summary") or ""))
    if summary and summary.casefold() != headline.casefold():
        return summary
    rationale = normalize_text(str(item.get("rationale") or ""))
    if rationale and rationale.casefold() != headline.casefold():
        return rationale
    return "Latest available item from this section's configured source."


def clip_text(text: str, limit: int) -> str:
    value = normalize_text(text)
    if len(value) <= limit:
        return value
    window = value[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    if len(window) < 24:
        window = value[:limit].rstrip()
    return window or value[:limit]


def _fit_label(name: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    label = normalize_text(name) or "SECTION"
    if font.getlength(label) <= max_width:
        return label
    while label and font.getlength(label) > max_width:
        label = label[:-1]
    return label or "S"


def _fit_line(line: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    fitted = line
    while fitted and font.getlength(fitted) > max_width:
        fitted = fitted[:-1]
    return fitted or "."


def _public_source_url(url: object) -> str | None:
    if not url:
        return None
    cleaned = digest_picker.strip_userinfo(str(url))
    return cleaned or None


def _section_kind(name: str) -> str:
    for spec in settings.sections():
        if spec.title == name:
            return spec.kind
    return "news"


def _standard_text_width() -> int:
    return WIDTH_DOTS - LEFT - RIGHT - 16


def _weather_text_width() -> int:
    text_x = LEFT + receipt_art.WEATHER_ICON_PX + receipt_art.WEATHER_GAP
    return WIDTH_DOTS - RIGHT - 8 - text_x


def _bubble_origin() -> tuple[int, int, int]:
    bubble_x = LEFT + receipt_art.AVATAR_PX + receipt_art.TAIL_GAP + receipt_art.TAIL_W
    bubble_w = WIDTH_DOTS - RIGHT - bubble_x
    return bubble_x, bubble_w, bubble_w - 2 * receipt_art.BUBBLE_PAD_X


def _prepare_sections(payload: dict) -> tuple[list[dict], bool]:
    regular = ImageFont.truetype(str(FONT_REGULAR), BODY_FONT_SIZE)
    detail_font = ImageFont.truetype(str(FONT_REGULAR), DETAIL_FONT_SIZE)
    source_font = ImageFont.truetype(str(FONT_BOLD), SOURCE_FONT_SIZE)
    section_font = ImageFont.truetype(str(FONT_BOLD), SECTION_FONT_SIZE)
    bar_width = WIDTH_DOTS - LEFT - RIGHT
    clipped = False
    prepared: list[dict] = []
    for section in payload["sections"]:
        raw_name = normalize_text(str(section.get("section") or ""))
        name = _fit_label(str(section.get("section") or ""), section_font, bar_width - 16)
        if len(name) < len(raw_name):
            clipped = True
        kind = _section_kind(raw_name)
        weather_icon = None
        if kind == "weather" and section.get("items"):
            first = section["items"][0]
            if isinstance(first, dict):
                weather_icon = receipt_art.icon_for_item(first)
        if kind == "bot_updates":
            text_width = _bubble_origin()[2]
        elif weather_icon:
            text_width = _weather_text_width()
        else:
            text_width = _standard_text_width()
        text_width = max(48, text_width)
        items = []
        for item in section["items"]:
            raw_headline = normalize_text(str(item.get("title") or item.get("headline") or ""))
            headline = clip_text(str(item.get("title") or item.get("headline") or ""), HEADLINE_CHAR_CAP) or "No update"
            raw_detail = detail_text(item, headline)
            detail = clip_text(raw_detail, DETAIL_CHAR_CAP) or "Latest available item from this section's configured source."
            source = clip_text(source_line(item), 160) or "Source: source"
            if "\x01" in str(item.get("title") or "") or "\x01" in str(item.get("headline") or "") or "\x01" in str(item.get("summary") or ""):
                clipped = True
            if len(headline) < len(raw_headline) or len(detail) < len(normalize_text(raw_detail)):
                clipped = True
            prepared_item = {
                "headline": headline,
                "detail": detail,
                "source": source,
                "headline_lines": [_fit_line(line, regular, text_width) for line in wrap_pixels(headline, regular, text_width)],
                "detail_lines": [_fit_line(line, detail_font, text_width) for line in wrap_pixels(detail, detail_font, text_width)],
                "source_lines": [_fit_line(line, source_font, text_width) for line in wrap_pixels(source, source_font, text_width)],
                "source_url": _public_source_url(item.get("url")),
                "source_id": item.get("source_id"),
            }
            if kind == "bot_updates":
                prepared_item["initial"] = receipt_art.bot_initial(item)
                prepared_item["avatar"] = receipt_art.load_avatar(
                    settings.avatars_dir(),
                    receipt_art.bot_slug(item),
                )
            items.append(prepared_item)
        prepared.append(
            {
                "section": name,
                "section_width": int(section_font.getlength(name) + 0.999),
                "kind": kind,
                "weather_icon": weather_icon,
                "items": items,
            }
        )
    return prepared, clipped


def _shrink_prepared(prepared: list[dict]) -> bool:
    """Drop one wrapped line from the fullest item. Keep a headline and a source line."""
    for key, floor in (("detail_lines", 1), ("headline_lines", 1), ("source_lines", 1)):
        best: tuple[int, int, int] | None = None
        for section_index, section in enumerate(prepared):
            for item_index, item in enumerate(section["items"]):
                extra = len(item[key])
                if extra > floor and (best is None or extra > best[0]):
                    best = (extra, section_index, item_index)
        if best is not None:
            _extra, section_index, item_index = best
            prepared[section_index]["items"][item_index][key].pop()
            return True
    return False


def _public_item(item: dict) -> dict:
    return {
        "headline": item["headline"],
        "detail": item["detail"],
        "source": item["source"],
        "source_url": item["source_url"],
        "source_id": item["source_id"],
    }


def _append_lines(
    block: dict,
    item_index: int,
    fonts: dict,
    grouped: tuple,
    text_x: int,
    baseline: int,
    max_width: int,
) -> tuple[int, int, int]:
    count = 0
    last = baseline
    for kind, lines, line_height, gap in grouped:
        baseline += gap
        for line in lines:
            width = min(max_width, int(fonts[kind].getlength(line) + 0.999))
            block["lines"].append(
                {
                    "text": line,
                    "x": text_x,
                    "y": baseline,
                    "width": width,
                    "kind": kind,
                    "item_index": item_index,
                }
            )
            last = baseline
            baseline += line_height
            count += 1
    return baseline, count, last


def _place_bots(block: dict, section: dict, y: int, fonts: dict) -> tuple[int, int, int, int]:
    """Avatar on the left, speech bubble on the right. Returns the y under the last bubble."""
    bubble_x, bubble_w, text_width = _bubble_origin()
    text_x = bubble_x + receipt_art.BUBBLE_PAD_X
    y += 6
    total_lines = 0
    headline_chars = 0
    printable_chars = 0
    for item_index, item in enumerate(section["items"]):
        if item_index:
            y += 8
        grouped = (
            ("headline", item["headline_lines"], BODY_LINE_HEIGHT, BODY_FONT_SIZE),
            ("detail", item["detail_lines"], DETAIL_LINE_HEIGHT, 4),
        )
        _baseline, count, last = _append_lines(
            block, item_index, fonts, grouped, text_x, y + receipt_art.BUBBLE_PAD_Y, text_width
        )
        total_lines += count
        bubble_h = max(receipt_art.AVATAR_PX, last + 8 - y + receipt_art.BUBBLE_PAD_Y)
        if bubble_h <= receipt_art.AVATAR_PX + 4:
            avatar_y = y + max(0, (bubble_h - receipt_art.AVATAR_PX) // 2)
        else:
            avatar_y = y
        block["bubbles"].append(
            {
                "x": bubble_x,
                "y": y,
                "w": bubble_w,
                "h": bubble_h,
                "tip_y": avatar_y + receipt_art.AVATAR_PX // 2,
            }
        )
        if item.get("avatar") is not None:
            block["sprites"].append(
                {
                    "image": item["avatar"],
                    "x": LEFT,
                    "y": avatar_y,
                    "mark": {"type": "avatar", "x": LEFT, "y": avatar_y, "size": receipt_art.AVATAR_PX},
                }
            )
        else:
            block["badges"].append(
                {
                    "cx": LEFT + receipt_art.AVATAR_PX // 2,
                    "cy": avatar_y + receipt_art.AVATAR_PX // 2,
                    "letter": item.get("initial") or "?",
                    "x": LEFT,
                    "y": avatar_y,
                }
            )
        block["items"].append(_public_item(item))
        headline_chars += len(item["headline"])
        printable_chars += len(item["headline"]) + len(item["detail"]) + len(item["source"])
        y += bubble_h
    return y, total_lines, headline_chars, printable_chars


def _place(prepared: list[dict]) -> tuple[list[dict], int, int, int, int]:
    regular = ImageFont.truetype(str(FONT_REGULAR), BODY_FONT_SIZE)
    detail_font = ImageFont.truetype(str(FONT_REGULAR), DETAIL_FONT_SIZE)
    source_font = ImageFont.truetype(str(FONT_BOLD), SOURCE_FONT_SIZE)
    fonts = {"headline": regular, "detail": detail_font, "source": source_font}
    y = 112
    blocks: list[dict] = []
    total_lines = 0
    headline_chars = 0
    printable_chars = 0
    for section in prepared:
        block = {
            "section": section["section"],
            "bar_y": y,
            "section_width": section["section_width"],
            "lines": [],
            "items": [],
            "bubbles": [],
            "badges": [],
            "sprites": [],
        }
        y += SECTION_BAR_HEIGHT
        if section.get("kind") == "bot_updates":
            y, added_lines, added_head, added_chars = _place_bots(block, section, y, fonts)
            total_lines += added_lines
            headline_chars += added_head
            printable_chars += added_chars
            y += 12
            blocks.append(block)
            continue
        weather_icon = section.get("weather_icon") if section.get("kind") == "weather" else None
        if weather_icon:
            icon_y = y + 6
            block["sprites"].append(
                {
                    "image": receipt_art.draw_weather_icon(weather_icon),
                    "x": LEFT,
                    "y": icon_y,
                    "mark": {
                        "type": "weather",
                        "icon": weather_icon,
                        "x": LEFT,
                        "y": icon_y,
                        "size": receipt_art.WEATHER_ICON_PX,
                    },
                }
            )
            text_x = LEFT + receipt_art.WEATHER_ICON_PX + receipt_art.WEATHER_GAP
            text_width = _weather_text_width()
        else:
            icon_y = None
            text_x = LEFT + 8
            text_width = _standard_text_width()
        baseline = y
        for item_index, item in enumerate(section["items"]):
            grouped = (
                ("headline", item["headline_lines"], BODY_LINE_HEIGHT, BODY_FONT_SIZE + 8),
                ("detail", item["detail_lines"], DETAIL_LINE_HEIGHT, 2),
                ("source", item["source_lines"], SOURCE_LINE_HEIGHT, 2),
            )
            if item_index:
                baseline += 10
            baseline, count, _last = _append_lines(block, item_index, fonts, grouped, text_x, baseline, text_width)
            total_lines += count
            block["items"].append(_public_item(item))
            headline_chars += len(item["headline"])
            printable_chars += len(item["headline"]) + len(item["detail"]) + len(item["source"])
        if icon_y is not None and baseline < icon_y + receipt_art.WEATHER_ICON_PX + 4:
            baseline = icon_y + receipt_art.WEATHER_ICON_PX + 4
        y = baseline + 12
        blocks.append(block)
    footer_y = y + 24
    height = footer_y + BOTTOM
    height = ((height + DOTS_PER_MM - 1) // DOTS_PER_MM) * DOTS_PER_MM
    return blocks, height, total_lines, headline_chars, printable_chars


def layout_payload(payload: dict) -> tuple[list[dict], int, int, int, int, bool]:
    validate_payload(payload)
    prepared, clipped = _prepare_sections(payload)
    blocks, height, total_lines, headline_chars, printable_chars = _place(prepared)
    guard = 0
    while height > MAX_HEIGHT_DOTS and guard < 5000:
        if not _shrink_prepared(prepared):
            break
        clipped = True
        blocks, height, total_lines, headline_chars, printable_chars = _place(prepared)
        guard += 1
    if height > MAX_HEIGHT_DOTS:
        height = MAX_HEIGHT_DOTS - (MAX_HEIGHT_DOTS % DOTS_PER_MM)
        clipped = True
    return blocks, height, total_lines, headline_chars, printable_chars, clipped


def build_svg(payload: dict) -> tuple[str, dict]:
    blocks, height, total_lines, headline_chars, printable_chars, clipped = layout_payload(payload)
    digest_id = payload["digest_id"]
    footer_y = height - BOTTOM
    date = normalize_text(payload.get("digest_date", ""))
    location = normalize_text(payload.get("location", ""))
    title = f"DAILY DIGEST {date}"
    svg: list[str] = [
        f"<!-- digest-id: {digest_id} -->",
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH_DOTS}" height="{height}" viewBox="0 0 {WIDTH_DOTS} {height}">',
        f'<rect width="{WIDTH_DOTS}" height="{height}" fill="white"/>',
        f'<rect x="{BORDER}" y="{BORDER}" width="{WIDTH_DOTS - 2 * BORDER}" height="{height - 2 * BORDER}" fill="none" stroke="black" stroke-width="3"/>',
        f'<text x="{LEFT}" y="{TOP + TITLE_FONT_SIZE}" font-family="DejaVu Sans Mono" font-size="{TITLE_FONT_SIZE}" font-weight="bold">{html.escape(title)}</text>',
        f'<text x="{LEFT}" y="{TOP + TITLE_FONT_SIZE + 28}" font-family="DejaVu Sans Mono" font-size="{SUBTITLE_FONT_SIZE}">{html.escape(location)}</text>',
        f'<path d="M{LEFT} 96H{WIDTH_DOTS - RIGHT}" stroke="black" stroke-width="2"/>',
    ]
    overlays: list[dict] = []
    art: list[dict] = []
    for block in blocks:
        svg.append(
            f'<rect x="{LEFT}" y="{block["bar_y"]}" width="{WIDTH_DOTS - LEFT - RIGHT}" height="{SECTION_BAR_HEIGHT}" fill="white" stroke="black" stroke-width="2"/>'
        )
        svg.append(
            f'<text x="{LEFT + 8}" y="{block["bar_y"] + 22}" font-family="DejaVu Sans Mono" font-size="{SECTION_FONT_SIZE}" font-weight="bold" fill="black">{html.escape(block["section"])}</text>'
        )
        for bubble in block["bubbles"]:
            path = receipt_art.bubble_path(bubble["x"], bubble["y"], bubble["w"], bubble["h"], bubble["tip_y"])
            svg.append(f'<path d="{path}" fill="white" stroke="black" stroke-width="3"/>')
            art.append({"type": "bubble", "x": bubble["x"], "y": bubble["y"], "w": bubble["w"], "h": bubble["h"]})
        for badge in block["badges"]:
            radius = receipt_art.AVATAR_PX // 2 - 2
            svg.append(
                f'<circle cx="{badge["cx"]}" cy="{badge["cy"]}" r="{radius}" fill="white" stroke="black" stroke-width="3"/>'
            )
            svg.append(
                f'<text x="{badge["cx"]}" y="{badge["cy"] + 11}" text-anchor="middle" '
                f'font-family="DejaVu Sans Mono" font-size="32" font-weight="bold" fill="black">{html.escape(badge["letter"])}</text>'
            )
            art.append(
                {
                    "type": "fallback",
                    "letter": badge["letter"],
                    "x": badge["x"],
                    "y": badge["y"],
                    "size": receipt_art.AVATAR_PX,
                }
            )
        for sprite in block["sprites"]:
            overlays.append({"image": sprite["image"], "x": sprite["x"], "y": sprite["y"]})
            art.append(sprite["mark"])
        for line in block["lines"]:
            size = {"headline": BODY_FONT_SIZE, "detail": DETAIL_FONT_SIZE, "source": SOURCE_FONT_SIZE}[line["kind"]]
            weight = "bold" if line["kind"] in {"headline", "source"} else "normal"
            svg.append(
                f'<text x="{line["x"]}" y="{line["y"]}" font-family="DejaVu Sans Mono" font-size="{size}" font-weight="{weight}" fill="black">{html.escape(line["text"])}</text>'
            )
    svg.extend(
        [
            f'<path d="M{LEFT} {footer_y - 24}H{WIDTH_DOTS - RIGHT}" stroke="black" stroke-width="2"/>',
            f'<text x="{LEFT}" y="{footer_y}" font-family="DejaVu Sans Mono" font-size="{SMALL_FONT_SIZE}">ID {digest_id[:12]} | {len(blocks)} SECTIONS</text>',
            "</svg>",
        ]
    )
    result = "".join(svg)
    ET.fromstring(re.sub(r"^<!--.*?-->", "", result, count=1))
    max_width = max(line["width"] for block in blocks for line in block["lines"])
    last_content_y = max(line["y"] for block in blocks for line in block["lines"])
    return result, {
        "width_dots": WIDTH_DOTS,
        "height_dots": height,
        "width_mm": WIDTH_MM,
        "height_mm": height / DOTS_PER_MM,
        "dynamic_height": True,
        "section_count": len(blocks),
        "body_line_count": total_lines,
        "headline_char_count": headline_chars,
        "prior_receipt_body_char_count": PRIOR_RECEIPT_BODY_CHARS,
        "printable_body_char_count": printable_chars,
        "text_volume_multiplier": round(printable_chars / PRIOR_RECEIPT_BODY_CHARS, 3),
        "max_line_width_dots": max_width,
        "printable_text_width_dots": WIDTH_DOTS - LEFT - RIGHT - 16,
        "content_bottom_dots": last_content_y,
        "footer_y_dots": footer_y,
        "clipping": clipped,
        "art": art,
        "overlays": overlays,
        "sections": [
            {
                "name": block["section"],
                "items": block["items"],
                "line_count": len(block["lines"]),
            }
            for block in blocks
        ],
    }


def rasterize(svg_path: Path, png_path: Path, height: int) -> Image.Image:
    subprocess.run(
        ["rsvg-convert", "-w", str(WIDTH_DOTS), "-h", str(height), "-o", str(png_path), str(svg_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    image = Image.open(png_path).convert("L")
    if image.size != (WIDTH_DOTS, height):
        raise ValueError(f"unexpected raster size: {image.size}")
    return image


def pack_tspl_bits(image: Image.Image, threshold: int = 185) -> bytes:
    """Pack TSPL BITMAP bytes per spec: 0 prints a dark dot; 1 leaves paper white."""
    width, height = image.size
    if width % 8:
        raise ValueError("TSPL2 bitmap width must be byte aligned")
    pixels = image.load()
    packed = bytearray()
    for y in range(height):
        for x0 in range(0, width, 8):
            byte = 0xFF
            for bit in range(8):
                if pixels[x0 + bit, y] < threshold:
                    byte &= ~(1 << (7 - bit))
            packed.append(byte)
    return bytes(packed)


def build_tspl(image: Image.Image) -> bytes:
    width, height = image.size
    width_bytes = width // 8
    bitmap = pack_tspl_bits(image)
    height_mm = height / DOTS_PER_MM
    header = (
        f"SIZE {WIDTH_MM} mm,{height_mm:.1f} mm\r\n"
        "GAP 0 mm,0 mm\r\n"
        "DIRECTION 1\r\n"
        "REFERENCE 0,0\r\n"
        "SET TEAR ON\r\n"
        "CLS\r\n"
        # Mode 0 overwrites a cleared buffer. BITMAP polarity is carried in the
        # data itself: per TSPL spec, 0=printed/dark and 1=unprinted/white.
        f"BITMAP 0,0,{width_bytes},{height},0,"
    ).encode("ascii")
    return header + bitmap + b"\r\nPRINT 1,1\r\n"


def load_source_text(source: Path) -> str:
    with source.open("rb") as handle:
        raw_bytes = handle.read(SOURCE_MAX_BYTES + 1)
    if len(raw_bytes) > SOURCE_MAX_BYTES:
        raise ValueError("digest source is too large")
    return raw_bytes.decode("utf-8")


def render(source: Path, output_dir: Path) -> dict:
    raw = load_source_text(source)
    payload = json.loads(raw)
    validate_payload(payload)
    digest_id = payload["digest_id"]
    svg_text, geometry = build_svg(payload)
    overlays = geometry.pop("overlays", [])
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"daily-digest-{payload['digest_date']}-{digest_id[:12]}"
    svg_path = output_dir / f"{stem}.svg"
    png_path = output_dir / f"{stem}.png"
    tspl_path = output_dir / f"{stem}.tspl"
    metadata_path = output_dir / f"{stem}.json"
    svg_path.write_text(svg_text, encoding="utf-8")
    image = rasterize(svg_path, png_path, geometry["height_dots"])
    if overlays:
        image = receipt_art.paste_overlays(image, overlays)
        image.save(png_path)
    dark = image.point(lambda value: 255 if value < 185 else 0)
    bbox = dark.getbbox()
    if not bbox or bbox[0] <= 0 or bbox[1] <= 0 or bbox[2] >= WIDTH_DOTS or bbox[3] >= geometry["height_dots"]:
        raise ValueError(f"raster content touches page edge: {bbox}")
    black_pixels = sum(image.histogram()[:185])
    black_ratio = black_pixels / (WIDTH_DOTS * geometry["height_dots"])
    if not 0.01 < black_ratio < 0.45:
        raise ValueError(f"unsafe raster black ratio: {black_ratio:.4f}")
    tspl = build_tspl(image)
    tspl_path.write_bytes(tspl)
    expected_bitmap_bytes = (WIDTH_DOTS // 8) * geometry["height_dots"]
    metadata = {
        "status": "rendered",
        "created_at": now_iso(),
        "source": str(source.resolve()),
        "source_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "source_errors": payload.get("source_errors", []),
        "digest_id": digest_id,
        "profile": "receipt-80mm-news-v7",
        "geometry": geometry,
        "raster_black_bbox": list(bbox),
        "raster_black_ratio": round(black_ratio, 6),
        "raster_polarity": "TSPL spec: bit 0 prints black; bit 1 leaves white",
        "tspl_bitmap_mode": 0,
        "tspl_direction": 1,
        "svg": str(svg_path.resolve()),
        "png": str(png_path.resolve()),
        "tspl": str(tspl_path.resolve()),
        "svg_sha256": hashlib.sha256(svg_path.read_bytes()).hexdigest(),
        "png_sha256": hashlib.sha256(png_path.read_bytes()).hexdigest(),
        "tspl_sha256": hashlib.sha256(tspl).hexdigest(),
        "bitmap_bytes": expected_bitmap_bytes,
        "tspl_bytes": len(tspl),
        "transport": "CUPS raw queue on digest host -> IPP raw queue on print server -> USB TSPL2",
    }
    atomic_json(metadata_path, metadata)
    metadata["metadata"] = str(metadata_path.resolve())
    return metadata


def _blank_state() -> dict:
    return {"version": 1, "digests": {}}


CORRUPT_KEEP = 8
CORRUPT_KEEP_SECONDS = 14 * 24 * 60 * 60


def _purge_corrupt_state(path: Path) -> None:
    """Drop old quarantined copies. Keep a few recent ones so the latest failure stays inspectable."""
    prefix = f"{path.name}.corrupt-"
    found: list[Path] = []
    try:
        children = list(path.parent.iterdir())
    except OSError:
        return
    for child in children:
        if not child.name.startswith(prefix):
            continue
        try:
            if child.is_file():
                found.append(child)
        except OSError:
            continue
    try:
        found.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return
    cutoff = time.time() - CORRUPT_KEEP_SECONDS
    for index, child in enumerate(found):
        try:
            recent = child.stat().st_mtime >= cutoff
        except OSError:
            continue
        if index < CORRUPT_KEEP and recent:
            continue
        try:
            child.unlink()
        except OSError:
            pass


def _quarantine_state(path: Path, identity: tuple[int, int] | None) -> bool:
    """Move a state file aside only when it is still the file that was read."""
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(current.st_mode):
        return False
    if identity is not None and (current.st_dev, current.st_ino) != identity:
        return False
    stamp = time.time_ns()
    dest = path.with_name(f"{path.name}.corrupt-{stamp}")
    counter = 0
    while dest.exists():
        counter += 1
        dest = path.with_name(f"{path.name}.corrupt-{stamp}-{counter}")
    os.replace(path, dest)
    return True


def _read_state_locked(path: Path) -> dict:
    if not path.exists():
        _purge_corrupt_state(path)
        return _blank_state()
    identity: tuple[int, int] | None = None
    try:
        # O_NONBLOCK so a fifo returns at once. A non-regular file is rejected
        # before any read, which is what would block on a pipe.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return _blank_state()
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("state is not a regular file")
        identity = (info.st_dev, info.st_ino)
        raw = os.read(fd, STATE_MAX_BYTES + 1)
        if len(raw) > STATE_MAX_BYTES:
            raise ValueError("state too large")
        value = json.loads(raw.decode("utf-8"))
        digests = value.get("digests") if isinstance(value, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(digests, dict)
            or any(not isinstance(entry, dict) for entry in digests.values())
        ):
            raise ValueError("invalid state")
        _purge_corrupt_state(path)
        return value
    except Exception:
        try:
            _quarantine_state(path, identity)
        except OSError:
            pass
        _purge_corrupt_state(path)
        return _blank_state()
    finally:
        os.close(fd)


def read_state(path: Path) -> dict:
    """Load print state. An unreadable or wrongly shaped file is set aside and replaced with a fresh one."""
    with digest_picker._printer_lock():
        return _read_state_locked(path)


def submit(metadata: dict, queue: str, state_path: Path, poll_seconds: int, *, force: bool = False) -> dict:
    state = read_state(state_path)
    digest_id = metadata["digest_id"]
    prior = state["digests"].get(digest_id)
    # A completed or still-queued job is not sent again. failed stays retryable.
    # force is the explicit reprint of this same source file.
    if prior and prior.get("status") in {"submitted", "completed"} and not force:
        result = dict(metadata)
        result.update({"status": "already_submitted", "prior": prior})
        return result
    cmd = ["lp", "-d", queue, "-o", "raw", "-t", f"daily-digest-{digest_id[:12]}", metadata["tspl"]]
    completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
    match = re.search(r"request id is (\S+)", completed.stdout)
    if not match:
        raise RuntimeError(f"lp did not return a request id: {completed.stdout!r}")
    job_id = match.group(1)
    record = {
        "status": "submitted",
        "submitted_at": now_iso(),
        "queue": queue,
        "job_id": job_id,
        "tspl_sha256": metadata["tspl_sha256"],
        "source": metadata["source"],
        "profile": metadata["profile"],
    }
    state["digests"][digest_id] = record
    atomic_json(state_path, state)
    deadline = time.monotonic() + poll_seconds
    while time.monotonic() < deadline:
        active = subprocess.run(["lpstat", "-W", "not-completed", "-o", queue], capture_output=True, text=True)
        if job_id not in active.stdout:
            record["status"] = "completed"
            record["completed_at"] = now_iso()
            atomic_json(state_path, state)
            break
        time.sleep(1)
    result = dict(metadata)
    result.update({"status": record["status"], "queue": queue, "job_id": job_id, "lp_output": completed.stdout.strip(), "state": str(state_path.resolve())})
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--submit", action="store_true")
    mode.add_argument("--reprint", action="store_true")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--queue", default=DEFAULT_QUEUE)
    parser.add_argument("--poll-seconds", type=int, default=30)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        metadata = render(args.source, args.output_dir)
    except (OSError, UnicodeError, ValueError):
        return 2
    if args.dry_run:
        metadata["status"] = "dry_run"
        metadata["queue"] = args.queue
        metadata["would_submit"] = metadata["tspl"]
        print(json.dumps(metadata, sort_keys=True))
        return 0
    try:
        result = submit(metadata, args.queue, args.state, args.poll_seconds, force=args.reprint)
    except Exception:
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
