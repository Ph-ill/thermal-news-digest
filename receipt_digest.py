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
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from PIL import Image, ImageFont

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
# Measured from the last underfilled seven-section receipt (ID 8c5c5ff56bd7).
# V3 must carry at least twice that 435-character body on every real render.
PRIOR_RECEIPT_BODY_CHARS = 435
FONT_REGULAR = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
FONT_BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")
PROJECT_DIR = Path(os.environ.get("DIGEST_HOME") or Path(__file__).resolve().parent)
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "output"
DEFAULT_STATE = PROJECT_DIR / "state.json"
DEFAULT_QUEUE = os.environ.get("DIGEST_PRINTER_QUEUE", "receipt")
EXPECTED_SECTIONS = (
    "WEATHER",
    "GLOBAL",
    "LOCAL",
    "EVENTS TODAY / SOON",
    "SPACEX/SPACE STOCK",
    "UK",
    "FILM/TV",
    "MUSIC",
)


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


def validate_payload(payload: dict) -> None:
    if payload.get("schema_version") != "1.0":
        raise ValueError("unsupported news digest schema")
    sections = payload.get("sections")
    if not isinstance(sections, list):
        raise ValueError("news digest sections are missing")
    names = tuple(section.get("section") for section in sections)
    if names != EXPECTED_SECTIONS:
        raise ValueError(f"expected sections {EXPECTED_SECTIONS!r}, got {names!r}")
    for section in sections:
        items = section.get("items")
        expected_count = range(1, 4) if section.get("section") == "EVENTS TODAY / SOON" else (1,)
        if not isinstance(items, list) or len(items) not in expected_count:
            raise ValueError(
                f"section {section.get('section')} has invalid item count {len(items) if isinstance(items, list) else None}"
            )
        for item in items:
            if not normalize_text(str(item.get("title") or item.get("headline") or "")):
                raise ValueError(f"section {section.get('section')} has no printable headline")
            if not normalize_text(str(item.get("summary") or item.get("rationale") or "")):
                raise ValueError(f"section {section.get('section')} has no printable detail")
            if not item.get("url"):
                raise ValueError(f"section {section.get('section')} has no source URL")
            if section.get("section") == "EVENTS TODAY / SOON" and not item.get("source_id"):
                raise ValueError("event item has no public source identifier")
    expected = payload_digest_id(payload)
    if payload.get("digest_id") != expected:
        raise ValueError("news digest ID does not match its stable payload")


def source_line(item: dict) -> str:
    host = (urlparse(str(item["url"])).hostname or "source").lower()
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


def layout_payload(payload: dict) -> tuple[list[dict], int, int, int, int]:
    validate_payload(payload)
    regular = ImageFont.truetype(str(FONT_REGULAR), BODY_FONT_SIZE)
    detail_font = ImageFont.truetype(str(FONT_REGULAR), DETAIL_FONT_SIZE)
    source_font = ImageFont.truetype(str(FONT_BOLD), SOURCE_FONT_SIZE)
    section_font = ImageFont.truetype(str(FONT_BOLD), SECTION_FONT_SIZE)
    text_width = WIDTH_DOTS - LEFT - RIGHT - 16
    bar_width = WIDTH_DOTS - LEFT - RIGHT
    y = 112
    blocks: list[dict] = []
    total_lines = 0
    headline_chars = 0
    printable_chars = 0
    for section in payload["sections"]:
        name = normalize_text(section["section"])
        name_width = int(section_font.getlength(name) + 0.999)
        if name_width > bar_width - 16:
            raise ValueError(f"section label exceeds printable width: {name!r}")
        block = {
            "section": name,
            "bar_y": y,
            "section_width": name_width,
            "lines": [],
            "items": [],
        }
        y += SECTION_BAR_HEIGHT
        baseline = y
        for item_index, item in enumerate(section["items"]):
            headline = normalize_text(str(item.get("title") or item.get("headline")))
            detail = detail_text(item, headline)
            source = source_line(item)
            headline_lines = wrap_pixels(headline, regular, text_width)
            detail_lines = wrap_pixels(detail, detail_font, text_width)
            source_lines = wrap_pixels(source, source_font, text_width)
            measured_headline = [int(regular.getlength(line) + 0.999) for line in headline_lines]
            measured_detail = [int(detail_font.getlength(line) + 0.999) for line in detail_lines]
            measured_source = [int(source_font.getlength(line) + 0.999) for line in source_lines]
            if any(width > text_width for width in measured_headline + measured_detail + measured_source):
                raise ValueError(f"text exceeds printable width in {name}")
            if item_index:
                baseline += 10
            baseline += BODY_FONT_SIZE + 8
            for line, width in zip(headline_lines, measured_headline):
                block["lines"].append({"text": line, "y": baseline, "width": width, "kind": "headline", "item_index": item_index})
                baseline += BODY_LINE_HEIGHT
                total_lines += 1
            baseline += 2
            for line, width in zip(detail_lines, measured_detail):
                block["lines"].append({"text": line, "y": baseline, "width": width, "kind": "detail", "item_index": item_index})
                baseline += DETAIL_LINE_HEIGHT
                total_lines += 1
            baseline += 2
            for line, width in zip(source_lines, measured_source):
                block["lines"].append({"text": line, "y": baseline, "width": width, "kind": "source", "item_index": item_index})
                baseline += SOURCE_LINE_HEIGHT
                total_lines += 1
            block["items"].append(
                {
                    "headline": headline,
                    "detail": detail,
                    "source": source,
                    "source_url": item["url"],
                    "source_id": item.get("source_id"),
                }
            )
            headline_chars += len(headline)
            printable_chars += len(headline) + len(detail) + len(source)
        y = baseline + 12
        blocks.append(block)
    if printable_chars < PRIOR_RECEIPT_BODY_CHARS * 2:
        raise ValueError(
            f"digest body is underfilled ({printable_chars} chars; requires at least {PRIOR_RECEIPT_BODY_CHARS * 2})"
        )
    footer_y = y + 24
    height = footer_y + BOTTOM
    height = ((height + DOTS_PER_MM - 1) // DOTS_PER_MM) * DOTS_PER_MM
    if height > MAX_HEIGHT_DOTS:
        raise ValueError(f"digest is too long ({height} dots); refusing unsafe print")
    return blocks, height, total_lines, headline_chars, printable_chars


def build_svg(payload: dict) -> tuple[str, dict]:
    blocks, height, total_lines, headline_chars, printable_chars = layout_payload(payload)
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
    for block in blocks:
        svg.append(
            f'<rect x="{LEFT}" y="{block["bar_y"]}" width="{WIDTH_DOTS - LEFT - RIGHT}" height="{SECTION_BAR_HEIGHT}" fill="white" stroke="black" stroke-width="2"/>'
        )
        svg.append(
            f'<text x="{LEFT + 8}" y="{block["bar_y"] + 22}" font-family="DejaVu Sans Mono" font-size="{SECTION_FONT_SIZE}" font-weight="bold" fill="black">{html.escape(block["section"])}</text>'
        )
        for line in block["lines"]:
            size = {"headline": BODY_FONT_SIZE, "detail": DETAIL_FONT_SIZE, "source": SOURCE_FONT_SIZE}[line["kind"]]
            weight = "bold" if line["kind"] in {"headline", "source"} else "normal"
            svg.append(
                f'<text x="{LEFT + 8}" y="{line["y"]}" font-family="DejaVu Sans Mono" font-size="{size}" font-weight="{weight}" fill="black">{html.escape(line["text"])}</text>'
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
        "clipping": False,
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


def render(source: Path, output_dir: Path) -> dict:
    raw = source.read_text(encoding="utf-8")
    payload = json.loads(raw)
    validate_payload(payload)
    digest_id = payload["digest_id"]
    svg_text, geometry = build_svg(payload)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"daily-digest-{payload['digest_date']}-{digest_id[:12]}"
    svg_path = output_dir / f"{stem}.svg"
    png_path = output_dir / f"{stem}.png"
    tspl_path = output_dir / f"{stem}.tspl"
    metadata_path = output_dir / f"{stem}.json"
    svg_path.write_text(svg_text, encoding="utf-8")
    image = rasterize(svg_path, png_path, geometry["height_dots"])
    dark = image.point(lambda value: 255 if value < 185 else 0)
    bbox = dark.getbbox()
    if not bbox or bbox[0] <= 0 or bbox[1] <= 0 or bbox[2] >= WIDTH_DOTS or bbox[3] >= geometry["height_dots"]:
        raise ValueError(f"raster content touches page edge: {bbox}")
    black_pixels = sum(1 for value in image.getdata() if value < 185)
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
        "profile": "receipt-80mm-news-events-v4",
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


def read_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "digests": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("digests"), dict):
        raise ValueError(f"invalid state file: {path}")
    return value


def submit(metadata: dict, queue: str, state_path: Path, poll_seconds: int) -> dict:
    state = read_state(state_path)
    digest_id = metadata["digest_id"]
    prior = state["digests"].get(digest_id)
    if prior and prior.get("status") in {"submitted", "completed"}:
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
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--queue", default=DEFAULT_QUEUE)
    parser.add_argument("--poll-seconds", type=int, default=30)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    metadata = render(args.source, args.output_dir)
    if args.dry_run:
        metadata["status"] = "dry_run"
        metadata["queue"] = args.queue
        metadata["would_submit"] = metadata["tspl"]
        print(json.dumps(metadata, sort_keys=True))
        return 0
    result = submit(metadata, args.queue, args.state, args.poll_seconds)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
