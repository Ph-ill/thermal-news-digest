"""Thermal pictures for the receipt: bot avatars and weather icons.

Avatars are contrast-boosted and Floyd-Steinberg dithered to 1-bit so a
203 dpi monochrome head can hold a face. Weather icons are drawn here as
line art. Nothing is fetched from the network.
"""

from __future__ import annotations

import io
import math
import os
import re
import stat
import warnings

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

import digest_picker
import digest_settings as settings


# 144 dots is 18 mm on the 8 dot/mm head. A dithered face needs that much
# before eyes and hair survive; 72 dots (9 mm) printed as noise.
AVATAR_PX = 144
AVATAR_MAX_BYTES = 512 * 1024
AVATAR_MAX_PIXELS = 1024 * 1024
AVATAR_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
TAIL_W = 22
TAIL_GAP = 8
BUBBLE_PAD_X = 16
BUBBLE_PAD_Y = 28
BUBBLE_RADIUS = 48
# Gap under the last baseline so descenders clear the stroke.
BUBBLE_INK_BELOW = 8
# White space between one note's outline and the next.
NOTE_GAP = 28
# Dots. Slightly heavier than the section rules so the balloon reads on thermal paper.
BUBBLE_STROKE = 4
WEATHER_ICON_PX = 64
WEATHER_GAP = 12

_DAY_KINDS = ("sun", "partly", "cloud", "rain", "storm", "snow", "fog", "wind")
_NIGHT_OF = {
    "sun": "night-clear",
    "partly": "night-partly",
    "cloud": "night-cloud",
    "rain": "night-rain",
    "storm": "night-storm",
    "snow": "night-snow",
    "fog": "night-fog",
    "wind": "night-wind",
}
WEATHER_ICONS = frozenset(_DAY_KINDS) | frozenset(_NIGHT_OF.values())

_CODE_KIND = {
    "skc": "sun",
    "few": "partly",
    "sct": "partly",
    "bkn": "cloud",
    "ovc": "cloud",
    "wind": "wind",
    "wind_skc": "wind",
    "wind_few": "wind",
    "wind_sct": "partly",
    "wind_bkn": "cloud",
    "wind_ovc": "cloud",
    "rain": "rain",
    "rain_showers": "rain",
    "rain_showers_hi": "rain",
    "rain_snow": "snow",
    "snow": "snow",
    "snow_showers": "snow",
    "tsra": "storm",
    "tsra_sct": "storm",
    "tsra_hi": "storm",
    "fog": "fog",
    "haze": "fog",
    "smoke": "fog",
    "dust": "fog",
    "hot": "sun",
    "cold": "cloud",
    "blizzard": "snow",
    "fzra": "rain",
    "sleet": "snow",
    "snow_sleet": "snow",
    "rain_sleet": "snow",
}
# Higher wins when the prose and the icon code disagree.
_RANK = {"sun": 0, "partly": 1, "cloud": 1, "wind": 2, "fog": 3, "rain": 4, "snow": 5, "storm": 6}


def _coord(value: float) -> str:
    return str(int(round(value)))


def bubble_radius(width: int, height: int, bulge: int) -> int:
    """Comic corner. Leave a straight left waist so the tail can sit mid-bubble."""
    room = max(16, height // 2 - 18)
    cap = max(12, room - bulge)
    return max(12, min(BUBBLE_RADIUS, width // 2, height // 2, cap))


def bubble_path(
    x: int,
    y: int,
    w: int,
    h: int,
    tip_y: int,
    *,
    radius: int | None = None,
    tail: int = TAIL_W,
) -> str:
    """Comic balloon: arched body, round corners, curved tail pointing left.

    The tail is two inward cubics that meet at ``tip_y``, which the layout
    sets to the avatar's vertical center. The arch and the corners stay
    inside the box; only the tail crosses the left edge.
    """
    x, y, w, h = int(x), int(y), int(w), int(h)
    tail = max(8, int(tail))
    bulge = max(8, min(16, h // 10))
    if radius is None:
        radius = bubble_radius(w, h, bulge)
    else:
        radius = max(8, min(int(radius), w // 2, h // 2))
    if 2 * (bulge + radius) > h - 16:
        bulge = max(0, (h - 2 * radius - 16) // 2)
    straight_top = y + bulge + radius
    straight_bot = y + h - bulge - radius
    if straight_bot - straight_top < 16:
        radius = max(8, (h - 16) // 2 - bulge)
        straight_top = y + bulge + radius
        straight_bot = y + h - bulge - radius
    span = max(16, straight_bot - straight_top)
    half = max(8, min(15, span // 2 - 2))
    tip_y = int(tip_y)
    tip_y = max(straight_top + half, min(tip_y, straight_bot - half))
    y1 = tip_y - half
    y2 = tip_y + half
    tip_x = x - tail
    mid = x + w / 2.0
    pull = tail * 0.55
    # Controls sit toward the tail's center line so each side bows inward
    # and the outline tapers to a point instead of a triangle.
    c1x, c1y = x - pull * 0.45, y2 + (tip_y - y2) * 0.72
    c2x, c2y = tip_x + tail * 0.38, tip_y + (y2 - tip_y) * 0.22
    c3x, c3y = tip_x + tail * 0.38, tip_y + (y1 - tip_y) * 0.22
    c4x, c4y = x - pull * 0.45, y1 + (tip_y - y1) * 0.72
    return (
        f"M{_coord(x + radius)},{_coord(y + bulge)} "
        f"Q{_coord(mid)},{_coord(y - bulge)} {_coord(x + w - radius)},{_coord(y + bulge)} "
        f"A{_coord(radius)},{_coord(radius)} 0 0 1 {_coord(x + w)},{_coord(y + bulge + radius)} "
        f"V{_coord(y + h - bulge - radius)} "
        f"A{_coord(radius)},{_coord(radius)} 0 0 1 {_coord(x + w - radius)},{_coord(y + h - bulge)} "
        f"Q{_coord(mid)},{_coord(y + h + bulge)} {_coord(x + radius)},{_coord(y + h - bulge)} "
        f"A{_coord(radius)},{_coord(radius)} 0 0 1 {_coord(x)},{_coord(straight_bot)} "
        f"V{_coord(y2)} "
        f"C{_coord(c1x)},{_coord(c1y)} {_coord(c2x)},{_coord(c2y)} {_coord(tip_x)},{_coord(tip_y)} "
        f"C{_coord(c3x)},{_coord(c3y)} {_coord(c4x)},{_coord(c4y)} {_coord(x)},{_coord(y1)} "
        f"V{_coord(straight_top)} "
        f"A{_coord(radius)},{_coord(radius)} 0 0 1 {_coord(x + radius)},{_coord(y + bulge)} Z"
    )


def bot_slug(item: dict) -> str:
    """Avatar file stem. A valid ``avatar`` slug wins; otherwise the bot name."""
    raw = item.get("avatar")
    if isinstance(raw, str) and digest_picker.AVATAR_SLUG.fullmatch(raw):
        return raw
    label = item.get("source_label") if isinstance(item.get("source_label"), str) else ""
    if not label:
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        if not title:
            title = item.get("headline") if isinstance(item.get("headline"), str) else ""
        label = title.split(":", 1)[0]
    return digest_picker.slugify_bot(label)


def bot_initial(item: dict) -> str:
    label = item.get("source_label") if isinstance(item.get("source_label"), str) else ""
    if not label.strip():
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        if not title:
            title = item.get("headline") if isinstance(item.get("headline"), str) else ""
        label = title.split(":", 1)[0]
    for char in label.strip():
        if char.isalnum():
            return char.upper()
    return "?"


def _cover_square(image: Image.Image, size: int) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    if side < 1:
        return Image.new("RGB", (size, size), (255, 255, 255))
    left = (width - side) // 2
    top = (height - side) // 2
    cropped = image.crop((left, top, left + side, top + side))
    return cropped.resize((size, size), Image.Resampling.LANCZOS)


def _flatten_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def dither_for_thermal(
    image: Image.Image,
    size: int = AVATAR_PX,
    *,
    threshold: int | None = None,
) -> Image.Image:
    """Square 1-bit portrait. Contrast is boosted, then Floyd-Steinberg dithered.

    ``threshold`` (0–255) is optional. It slides the gray value so that level
    becomes the dither midpoint. A higher threshold prints more black, which
    helps a washed-out photo on a thermal head. Unset leaves the midpoint at 128
    after the contrast boost.
    """
    fitted = _cover_square(_flatten_rgb(image), size)
    gray = ImageOps.autocontrast(ImageOps.grayscale(fitted), cutoff=1)
    gray = ImageEnhance.Contrast(gray).enhance(1.85)
    if threshold is not None:
        shift = 128 - int(threshold)
        gray = gray.point(lambda value, offset=shift: max(0, min(255, value + offset)))
    bits = gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    return bits.convert("L")


def _read_regular(path, limit: int) -> bytes | None:
    """Read a regular file. Symlinks and other non-files are refused."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    if info.st_size <= 0 or info.st_size > limit:
        return None
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return None
        if opened.st_size <= 0 or opened.st_size > limit:
            return None
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            piece = os.read(fd, min(65536, remaining))
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    if len(raw) > limit:
        return None
    return raw


def _decode_limited(raw: bytes) -> Image.Image | None:
    previous = Image.MAX_IMAGE_PIXELS
    # Pillow raises DecompressionBombError only above twice the configured cap.
    Image.MAX_IMAGE_PIXELS = max(1, AVATAR_MAX_PIXELS // 2)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                if image.width < 1 or image.height < 1:
                    return None
                if image.width * image.height > AVATAR_MAX_PIXELS:
                    return None
                return _flatten_rgb(image)
    except Exception:
        return None
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def load_avatar(directory, slug: str, *, threshold: int | None = None) -> Image.Image | None:
    """Load ``directory/<slug>.(png|jpg|webp)`` or return None for the initial fallback.

    The slug is a single path segment. The file must be a regular file, not a
    symlink, at most 512 KiB, and a Pillow image of at most one megapixel.
    """
    if not isinstance(slug, str) or not digest_picker.AVATAR_SLUG.fullmatch(slug):
        return None
    try:
        if not directory.is_dir():
            return None
    except OSError:
        return None
    if threshold is None:
        threshold = settings.avatar_threshold()
    for extension in AVATAR_EXTENSIONS:
        path = directory / f"{slug}{extension}"
        raw = _read_regular(path, AVATAR_MAX_BYTES)
        if raw is None:
            continue
        decoded = _decode_limited(raw)
        if decoded is None:
            continue
        return dither_for_thermal(decoded, AVATAR_PX, threshold=threshold)
    return None


def paste_overlays(image: Image.Image, overlays: list[dict]) -> Image.Image:
    """Paste 1-bit sprites onto the raster. Done after SVG conversion so dither dots stay sharp."""
    canvas = image.convert("L")
    for item in overlays:
        sprite = item["image"].convert("L")
        x = int(item["x"])
        y = int(item["y"])
        if x < 0 or y < 0 or x >= canvas.width or y >= canvas.height:
            continue
        max_w = canvas.width - x
        max_h = canvas.height - y
        if sprite.width > max_w or sprite.height > max_h:
            sprite = sprite.crop((0, 0, max(1, min(sprite.width, max_w)), max(1, min(sprite.height, max_h))))
        canvas.paste(sprite, (x, y))
    return canvas


def _blob(*parts: str) -> str:
    text = " ".join(part for part in parts if part)
    text = text.casefold().replace("_", " ").replace("/", " ").replace(",", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text).strip()


def _at(blob: str, *words: str, prefix: bool = False, not_day: bool = False) -> int | None:
    """Index of the earliest word. ``not_day`` matches ``sun`` and ``sunny`` but not ``sunday``."""
    best: int | None = None
    for word in words:
        if not_day:
            pattern = rf"(?<![a-z0-9]){re.escape(word)}(?!day)"
        elif prefix:
            pattern = rf"(?<![a-z0-9]){re.escape(word)}"
        else:
            pattern = rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])"
        match = re.search(pattern, blob)
        if match is not None and (best is None or match.start() < best):
            best = match.start()
    return best


def _has(blob: str, *words: str, prefix: bool = False) -> bool:
    return _at(blob, *words, prefix=prefix) is not None


def _soonest(*indexes: int | None) -> int | None:
    found = [index for index in indexes if index is not None]
    return min(found) if found else None


def _kind_from_prose(blob: str) -> str | None:
    if not blob:
        return None
    storm = _has(blob, "thunder", "tornado", prefix=True) or _has(blob, "tstm", "tsra")
    snow = _has(blob, "snow", "flurr", "wintry", prefix=True) or _has(blob, "sleet", "blizzard")
    rain = _has(blob, "rain", "shower", "drizzle", prefix=True) or _has(blob, "fzra")
    fog = _has(blob, "fog", prefix=True) or _has(blob, "mist", "haze", "smoke", "dust")
    partly = _has(blob, "partly", "scattered") or _has(blob, "sct", "few")
    wind = _has(blob, "wind", "breez", "gust", prefix=True)
    cloud_at = _soonest(_at(blob, "cloud", prefix=True), _at(blob, "overcast", "bkn", "ovc"))
    # ``sun(?!day)`` matches sunny and a lone sun, and leaves the weekday alone.
    sun_at = _soonest(_at(blob, "sun", not_day=True), _at(blob, "clear", "fair", "skc", "hot"))
    if storm:
        return "storm"
    if snow:
        return "snow"
    if rain:
        return "rain"
    if fog:
        return "fog"
    if partly:
        return "partly"
    if cloud_at is not None or sun_at is not None:
        # NWS names the sky you have now first: "Mostly Cloudy then Sunny" stays cloudy.
        if sun_at is None or (cloud_at is not None and cloud_at <= sun_at):
            return "cloud"
        return "sun"
    if wind:
        return "wind"
    return None


def _kind_from_code(code: str) -> str | None:
    code = code.split(",")[0].casefold()
    if code in _CODE_KIND:
        return _CODE_KIND[code]
    if code.startswith("tsra") or "tornado" in code:
        return "storm"
    if code.startswith("snow") or "sleet" in code or code.startswith("blizzard"):
        return "snow"
    if code.startswith("rain") or code.startswith("shower") or code.startswith("fzra"):
        return "rain"
    if code.startswith("fog") or code in {"haze", "smoke", "dust"}:
        return "fog"
    if code.startswith("wind_"):
        base = _kind_from_code(code[5:])
        if base in {None, "sun"}:
            return "wind"
        return base
    if code.startswith("wind"):
        return "wind"
    return None


def _codes(icon: str, *, nighttime: bool) -> list[str]:
    text = icon.casefold()
    day_codes = re.findall(r"day/([a-z0-9_]+)", text)
    night_codes = re.findall(r"night/([a-z0-9_]+)", text)
    if nighttime:
        return night_codes or day_codes
    return day_codes or night_codes


def _night_from_icon(icon: str) -> bool | None:
    text = icon.casefold()
    has_night = "/night/" in text or text.startswith("night/")
    has_day = "/day/" in text or text.startswith("day/")
    if has_night and not has_day:
        return True
    if has_day and not has_night:
        return False
    return None


def _prefer(prose: str | None, code: str | None) -> str | None:
    if prose is None:
        return code
    if code is None:
        return prose
    if _RANK[code] > _RANK[prose]:
        return code
    return prose


def classify_weather(short_forecast: str = "", icon: str = "", *, nighttime: bool | None = None) -> str | None:
    """Map NWS ``shortForecast`` text and an icon URL or code to a receipt icon.

    Precipitation and storms win over sky cover. Wind wins over a clear icon
    code (``wind_skc``) and loses to an explicit sunny or cloudy sentence.
    When both cloud and sun are named, the earlier word wins, and "Sunday"
    is not a sunny forecast. An empty description returns None so a missing
    forecast is not given a cloud.
    """
    if nighttime is None:
        nighttime = _night_from_icon(icon)
    if nighttime is None:
        nighttime = False
    prose = _kind_from_prose(_blob(short_forecast))
    code_kind = None
    for code in _codes(icon, nighttime=bool(nighttime)):
        found = _kind_from_code(code)
        if found is None:
            continue
        code_kind = found if code_kind is None or _RANK[found] > _RANK[code_kind] else code_kind
    # Icon text with no URL still counts, for a bare code such as "tsra".
    if code_kind is None and icon and "/" not in icon:
        code_kind = _kind_from_code(icon.replace(" ", "_"))
        if code_kind is None:
            code_kind = _kind_from_prose(_blob(icon))
    kind = _prefer(prose, code_kind)
    if kind is None:
        return None
    if nighttime:
        return _NIGHT_OF[kind]
    return kind


def icon_for_item(item: dict) -> str | None:
    """Icon for a weather item. An explicit ``weather_icon`` wins.

    The detailed summary is not consulted: it often mentions a later shower
    that should not replace the short forecast drawn in the headline.
    """
    explicit = item.get("weather_icon")
    if isinstance(explicit, str) and explicit in WEATHER_ICONS:
        return explicit
    short = item.get("short_forecast") if isinstance(item.get("short_forecast"), str) else ""
    icon = item.get("forecast_icon") if isinstance(item.get("forecast_icon"), str) else ""
    night = item.get("is_daytime")
    nighttime = None if not isinstance(night, bool) else not night
    if short or icon:
        found = classify_weather(short, icon, nighttime=nighttime)
        if found:
            return found
    headline = item.get("headline") if isinstance(item.get("headline"), str) else ""
    if not headline:
        headline = item.get("title") if isinstance(item.get("title"), str) else ""
    if not headline:
        return None
    return classify_weather(headline, "", nighttime=nighttime)


def _scale(size: int, *values: float) -> tuple[int, ...]:
    return tuple(int(round(value * size / 64)) for value in values)


def _mask(size: int) -> Image.Image:
    return Image.new("L", (size, size), 0)


def _outline(mask: Image.Image, width: int = 2) -> Image.Image:
    kernel = width * 2 + 1
    dilated = mask.filter(ImageFilter.MaxFilter(kernel))
    return ImageChops.subtract(dilated, mask)


def _stamp(image: Image.Image, mask: Image.Image, color: int = 0) -> None:
    if mask.getbbox() is None:
        return
    ink = Image.new("L", image.size, color)
    image.paste(ink, mask=mask)


def _cloud_mask(size: int, box: tuple[int, int, int, int]) -> Image.Image:
    mask = _mask(size)
    draw = ImageDraw.Draw(mask)
    x0, y0, x1, y1 = box
    width = max(1, x1 - x0)
    height = max(1, y1 - y0)
    draw.rounded_rectangle((x0 + width * 0.08, y0 + height * 0.45, x0 + width * 0.92, y0 + height * 0.92), radius=max(2, size // 10), fill=255)
    draw.ellipse((x0 + width * 0.02, y0 + height * 0.38, x0 + width * 0.46, y0 + height * 0.88), fill=255)
    draw.ellipse((x0 + width * 0.24, y0 + height * 0.08, x0 + width * 0.70, y0 + height * 0.78), fill=255)
    draw.ellipse((x0 + width * 0.52, y0 + height * 0.32, x0 + width * 0.98, y0 + height * 0.90), fill=255)
    return mask


def _paint_cloud(image: Image.Image, box: tuple[int, int, int, int]) -> None:
    _stamp(image, _outline(_cloud_mask(image.size[0], box), 2))


def _moon_mask(size: int, box: tuple[int, int, int, int]) -> Image.Image:
    mask = _mask(size)
    draw = ImageDraw.Draw(mask)
    x0, y0, x1, y1 = box
    draw.ellipse(box, fill=255)
    shift = int((x1 - x0) * 0.45)
    grow = int((y1 - y0) * 0.06)
    draw.ellipse((x0 + shift, y0 - grow, x1 + shift, y1 + grow), fill=0)
    return mask


def _paint_moon(image: Image.Image, box: tuple[int, int, int, int]) -> None:
    _stamp(image, _moon_mask(image.size[0], box))


def _sun_rays(draw: ImageDraw.ImageDraw, cx: int, cy: int, inner: int, outer: int, width: int = 3) -> None:
    for step in range(8):
        angle = math.radians(step * 45)
        draw.line(
            (
                cx + math.cos(angle) * inner,
                cy + math.sin(angle) * inner,
                cx + math.cos(angle) * outer,
                cy + math.sin(angle) * outer,
            ),
            fill=0,
            width=width,
        )


def _draw_sun(image: Image.Image, *, cx: int | None = None, cy: int | None = None, radius: int | None = None) -> None:
    size = image.size[0]
    cx = size // 2 if cx is None else cx
    cy = size // 2 if cy is None else cy
    radius = size // 5 if radius is None else radius
    draw = ImageDraw.Draw(image)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=0, width=max(2, size // 20))
    _sun_rays(draw, cx, cy, radius + size // 16, radius + size // 5, max(2, size // 22))


def _draw_moon(image: Image.Image) -> None:
    size = image.size[0]
    _paint_moon(image, _scale(size, 16, 10, 50, 50))


def _draw_partly(image: Image.Image, *, night: bool) -> None:
    size = image.size[0]
    if night:
        _paint_moon(image, _scale(size, 6, 4, 30, 30))
    else:
        _draw_sun(image, cx=int(size * 0.30), cy=int(size * 0.30), radius=int(size * 0.12))
    _paint_cloud(image, _scale(size, 16, 22, 60, 58))


def _draw_cloud(image: Image.Image, *, night: bool) -> None:
    size = image.size[0]
    if night:
        _paint_moon(image, _scale(size, 4, 4, 24, 26))
    _paint_cloud(image, _scale(size, 8, 16, 58, 52))


def _pairs(size: int, *coords: float) -> tuple[tuple[int, int], ...]:
    scaled = _scale(size, *coords)
    return tuple(zip(scaled[0::2], scaled[1::2]))


def _drops(image: Image.Image, points: tuple[tuple[int, int], ...]) -> None:
    draw = ImageDraw.Draw(image)
    for x, y in points:
        draw.line((x, y, x - 3, y + 8), fill=0, width=3)


def _draw_rain(image: Image.Image, *, night: bool) -> None:
    size = image.size[0]
    if night:
        _paint_moon(image, _scale(size, 4, 2, 20, 20))
    _paint_cloud(image, _scale(size, 8, 6, 56, 40))
    _drops(image, _pairs(size, 22, 44, 34, 46, 46, 44))


def _draw_storm(image: Image.Image, *, night: bool) -> None:
    size = image.size[0]
    if night:
        _paint_moon(image, _scale(size, 4, 2, 18, 18))
    _paint_cloud(image, _scale(size, 6, 4, 58, 36))
    draw = ImageDraw.Draw(image)
    bolt = _scale(size, 34, 30, 24, 44, 32, 44, 22, 58, 40, 42, 32, 42)
    draw.polygon(list(zip(bolt[0::2], bolt[1::2])), fill=0)


def _flake(draw: ImageDraw.ImageDraw, x: int, y: int, radius: int = 4) -> None:
    draw.line((x - radius, y, x + radius, y), fill=0, width=2)
    draw.line((x, y - radius, x, y + radius), fill=0, width=2)
    draw.line((x - radius + 1, y - radius + 1, x + radius - 1, y + radius - 1), fill=0, width=2)
    draw.line((x - radius + 1, y + radius - 1, x + radius - 1, y - radius + 1), fill=0, width=2)


def _draw_snow(image: Image.Image, *, night: bool) -> None:
    size = image.size[0]
    if night:
        _paint_moon(image, _scale(size, 4, 2, 18, 18))
    _paint_cloud(image, _scale(size, 8, 4, 56, 36))
    draw = ImageDraw.Draw(image)
    for point in (_scale(size, 22, 48), _scale(size, 34, 56), _scale(size, 46, 48)):
        _flake(draw, point[0], point[1], max(3, size // 16))


def _draw_fog(image: Image.Image, *, night: bool) -> None:
    size = image.size[0]
    draw = ImageDraw.Draw(image)
    if night:
        _paint_moon(image, _scale(size, 40, 4, 58, 22))
    for index, inset in enumerate((8, 16, 10, 18)):
        y = int(size * (0.28 + index * 0.16))
        left, right = _scale(size, inset, 64 - inset)
        draw.line((left, y, right, y), fill=0, width=max(3, size // 18))


def _draw_wind(image: Image.Image, *, night: bool) -> None:
    size = image.size[0]
    draw = ImageDraw.Draw(image)
    if night:
        _paint_moon(image, _scale(size, 42, 4, 60, 22))
    width = max(3, size // 18)
    spans = ((8, 18, 46), (6, 32, 52), (10, 46, 40))
    for left, y, right in spans:
        x0, yy, x1 = _scale(size, left, y, right)
        draw.line((x0, yy, x1, yy), fill=0, width=width)
        hook = max(6, size // 8)
        draw.arc((x1 - hook, yy - hook, x1 + hook, yy + hook), 270, 45, fill=0, width=width)


def draw_weather_icon(kind: str, size: int = WEATHER_ICON_PX) -> Image.Image:
    """Line-art icon on white. Unknown kinds become a cloud."""
    if kind not in WEATHER_ICONS:
        kind = "cloud"
    image = Image.new("L", (size, size), 255)
    if kind == "sun":
        _draw_sun(image)
    elif kind == "night-clear":
        _draw_moon(image)
    elif kind == "partly":
        _draw_partly(image, night=False)
    elif kind == "night-partly":
        _draw_partly(image, night=True)
    elif kind == "cloud":
        _draw_cloud(image, night=False)
    elif kind == "night-cloud":
        _draw_cloud(image, night=True)
    elif kind == "rain":
        _draw_rain(image, night=False)
    elif kind == "night-rain":
        _draw_rain(image, night=True)
    elif kind == "storm":
        _draw_storm(image, night=False)
    elif kind == "night-storm":
        _draw_storm(image, night=True)
    elif kind == "snow":
        _draw_snow(image, night=False)
    elif kind == "night-snow":
        _draw_snow(image, night=True)
    elif kind == "fog":
        _draw_fog(image, night=False)
    elif kind == "night-fog":
        _draw_fog(image, night=True)
    elif kind == "wind":
        _draw_wind(image, night=False)
    else:
        _draw_wind(image, night=True)
    return image
