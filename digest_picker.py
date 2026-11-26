"""Choose digest items, call an OpenAI-compatible chat model, and remember recent picks.

Any failure of the model call falls back to a deterministic picker. The print
must still run when the key is missing, the request times out, or the JSON is bad.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from email.utils import parsedate_to_datetime
import errno
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import digest_settings as settings


PLACEHOLDER_TITLE = "No new update today"
PLACEHOLDER_SUMMARY = (
    "No current item passed the obituary, spam, and duplicate checks for this section."
)
BOT_TEXT_MAX = 360
_ABBREV = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|U\.S|U\.K|a\.m|p\.m|e\.g|i\.e)\.",
    re.IGNORECASE,
)
_OBITUARY = re.compile(
    r"\b(?:obituary|obituaries|obit|in memoriam|passed away|passes away|funeral service|"
    r"celebration of life)\b|\b(?:dies|died|dead) at \d",
    re.IGNORECASE,
)
# Person-death phrasing only. "Senate killed the bill" is legislation, not an obituary.
_DEATH = re.compile(
    r"\b(?:dies|died)\b|\bdeath of\b|\bkilled in\b|\bwas killed\b|\bkilled by\b",
    re.IGNORECASE,
)
_DIES_AT_SEA = re.compile(r"\bdies at sea\b", re.IGNORECASE)
_SPAM = re.compile(
    r"last chance to buy|stock alert|penny stocks?|stocks? to buy|shares? to buy|"
    r"buy alert|hot stock tip|buy now|act now|limited[- ]time|you won'?t believe|"
    r"you wont believe|shocking|click here|sponsored content|advertorial|promo code|"
    r"\d+% off|before it'?s too late|don'?t miss this|"
    r"stocks?\s+i['’]?m\s+buying|what\s+i['’]?m\s+buying|stocks?\s+to\s+watch|"
    r"hot\s+tips?|investing\s+alerts?|price\s+targets?|undervalued\s+stocks?|buy\s+the\s+dip",
    re.IGNORECASE,
)
HISTORY_MAX_BYTES = 65536
HISTORY_URL_MAX = 500
HISTORY_TITLE_MAX = 240
BOT_MAX_BYTES = 65536
RESPONSE_MAX_BYTES = 1_000_000
_LAST_CHANCE = re.compile(r"last chance", re.IGNORECASE)
_INVESTING_WORD = re.compile(r"\b(?:stock|stocks|shares|invest|investing)\b", re.IGNORECASE)
_MARKDOWN = re.compile(r"[`#*_]|\[(.*?)\]\(.*?\)")
_TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid"}


class LLMError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BotUpdatesError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def xml_text(value: str) -> str:
    """Drop characters XML 1.0 cannot carry, so a title cannot break the SVG."""
    kept: list[str] = []
    for char in value or "":
        code = ord(char)
        if code in (0x9, 0xA, 0xD) or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF:
            kept.append(char)
    return "".join(kept)


def fold(text: str) -> str:
    value = unicodedata.normalize("NFKC", xml_text(text or ""))
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    value = value.casefold()
    return re.sub(r"\s+", " ", value).strip()


def clean_spaces(text: str) -> str:
    value = unicodedata.normalize("NFKC", xml_text(text or ""))
    return re.sub(r"\s+", " ", value).strip()


def strip_userinfo(url: str) -> str:
    """Return a URL with any username or password removed."""
    raw = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw
    if not parts.scheme or not parts.hostname:
        return raw
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class _DeadlineWatch:
    """Close the request socket when an absolute monotonic deadline passes.

    ``urlopen``'s timeout is an idle timer: a byte of status line, header, or
    body resets it. This watchdog holds a duplicate of every socket the
    connection creates and shuts that connection down at ``deadline``, covering
    connect, headers, and body. DNS is bounded separately, before a socket exists.
    """

    def __init__(self, deadline: float | None):
        self.deadline = deadline
        self._lock = threading.Lock()
        self._socks: list[socket.socket] = []
        self._stop = threading.Event()
        self._fired = False
        if deadline is not None:
            threading.Thread(target=self._run, name="digest-deadline", daemon=True).start()

    def arm(self, sock: socket.socket) -> None:
        try:
            duped = socket.socket(fileno=os.dup(sock.fileno()))
        except OSError:
            return
        with self._lock:
            expired = self._fired or self._stop.is_set() or (
                self.deadline is not None and time.monotonic() >= self.deadline
            )
            if expired:
                self._fired = True
            else:
                self._socks.append(duped)
        if expired:
            _abort_socket(duped)

    def disarm(self) -> None:
        """The request finished in time. Drop the duplicates without shutting it down."""
        with self._lock:
            self._stop.set()
            if self._fired:
                return
            socks = list(self._socks)
            self._socks.clear()
        for duped in socks:
            try:
                duped.close()
            except OSError:
                pass

    def _run(self) -> None:
        delay = 0.0 if self.deadline is None else self.deadline - time.monotonic()
        if delay > 0 and self._stop.wait(delay):
            return
        with self._lock:
            if self._stop.is_set():
                return
            self._fired = True
            socks = list(self._socks)
            self._socks.clear()
        for duped in socks:
            _abort_socket(duped)


def _abort_socket(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _getaddrinfo(host: str, port: int, deadline: float | None):
    """Resolve ``host`` and give up at ``deadline``. ``getaddrinfo`` has no timeout of its own."""
    if deadline is None:
        return socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if time.monotonic() >= deadline:
        raise TimeoutError("budget")
    box: dict = {}

    def resolve() -> None:
        try:
            box["result"] = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=resolve, name="digest-dns", daemon=True)
    thread.start()
    thread.join(max(0.0, deadline - time.monotonic()))
    if thread.is_alive():
        raise TimeoutError("budget")
    if "error" in box:
        raise box["error"]
    return box.get("result") or []


def _open_deadline_socket(host: str, port: int, timeout, source_address, watch: _DeadlineWatch | None):
    deadline = None if watch is None else watch.deadline
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("budget")
    infos = _getaddrinfo(host, port, deadline)
    last_error: OSError | None = None
    for af, socktype, proto, _canon, sa in infos:
        sock = socket.socket(af, socktype, proto)
        try:
            _apply_idle_timeout(sock, timeout, deadline)
            if source_address:
                sock.bind(source_address)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as exc:
                if exc.errno != errno.ENOPROTOOPT:
                    raise
            if watch is not None:
                watch.arm(sock)
            sock.connect(sa)
            return sock
        except TimeoutError:
            sock.close()
            raise
        except OSError as exc:
            last_error = exc
            try:
                sock.close()
            except OSError:
                pass
    if last_error is not None:
        raise last_error
    raise OSError(f"no address for {host}")


def _apply_idle_timeout(sock: socket.socket, timeout, deadline: float | None) -> None:
    """Idle backstop. A trickle still has to be cut off by the watchdog."""
    if timeout is None or timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        idle = None
    else:
        idle = float(timeout)
    if deadline is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("budget")
        idle = left if idle is None else min(idle, left)
    if idle is not None:
        sock.settimeout(max(0.05, idle))


class _DeadlineHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *, blocksize=8192, watch=None):
        self._watch = watch
        super().__init__(host, port, timeout, source_address, blocksize=blocksize)

    def connect(self):
        sys.audit("http.client.connect", self, self.host, self.port)
        self.sock = _open_deadline_socket(self.host, self.port, self.timeout, self.source_address, self._watch)
        if self._tunnel_host:
            self._tunnel()


class _DeadlineHTTPSConnection(_DeadlineHTTPConnection):
    default_port = http.client.HTTPS_PORT

    def __init__(
        self,
        host,
        port=None,
        timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
        source_address=None,
        *,
        context=None,
        blocksize=8192,
        watch=None,
    ):
        super().__init__(host, port, timeout, source_address, blocksize=blocksize, watch=watch)
        if context is None:
            context = http.client._create_https_context(http.client.HTTPSConnection._http_vsn)
        self._context = context

    def connect(self):
        super().connect()
        wrapped = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host)
        self.sock = wrapped
        if self._watch is not None:
            self._watch.arm(wrapped)


class _BudgetHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, watch: _DeadlineWatch):
        super().__init__()
        self._watch = watch

    def http_open(self, req):
        watch = self._watch

        def factory(host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, **_kwargs):
            return _DeadlineHTTPConnection(host, timeout=timeout, source_address=source_address, watch=watch)

        return self.do_open(factory, req)


class _BudgetHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, watch: _DeadlineWatch):
        super().__init__()
        self._watch = watch

    def https_open(self, req):
        watch = self._watch
        context = self._context

        def factory(host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, context=None, **_kwargs):
            return _DeadlineHTTPSConnection(
                host,
                timeout=timeout,
                source_address=source_address,
                context=context or self._context,
                watch=watch,
            )

        return self.do_open(factory, req, context=context)


class _WatchedResponse:
    def __init__(self, response, watch: _DeadlineWatch):
        self._response = response
        self._watch = watch

    def __enter__(self):
        return self._response

    def __exit__(self, *_exc):
        try:
            self._response.close()
        finally:
            self._watch.disarm()


def open_bounded(request, timeout: float, deadline: float | None):
    """Open ``request``. When ``deadline`` is set, a watchdog closes the socket at that instant."""
    if deadline is None:
        return urllib.request.urlopen(request, timeout=timeout)
    watch = _DeadlineWatch(deadline)
    opener = urllib.request.build_opener(_BudgetHTTPHandler(watch), _BudgetHTTPSHandler(watch))
    try:
        response = opener.open(request, timeout=timeout)
    except BaseException:
        watch.disarm()
        raise
    return _WatchedResponse(response, watch)


def read_bounded(response, limit: int, deadline: float | None) -> bytes:
    """Read at most ``limit`` decoded body bytes, and stop when ``deadline`` passes.

    Chunk framing is ``http.client``'s. The deadline is enforced by the watchdog
    started in ``open_bounded`` (a blocked ``read`` is cut off by closing the
    socket) and by the idle timeout already set on the connection.
    """
    sock = _response_socket(response)
    if deadline is not None and sock is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("budget")
        try:
            current = sock.gettimeout()
            if current is None or current > left:
                sock.settimeout(max(0.05, left))
        except OSError:
            pass
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("budget")
        try:
            piece = response.read(min(8192, limit - total))
        except TimeoutError as exc:
            raise TimeoutError("budget") from exc
        except Exception as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("budget") from exc
            raise
        if isinstance(piece, str):
            piece = piece.encode("utf-8", errors="replace")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("budget")
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
    return b"".join(chunks)


def _response_socket(response):
    seen: list[object] = []
    current = response
    for _ in range(6):
        if current is None or current in seen:
            break
        seen.append(current)
        for attr in ("_sock", "sock"):
            sock = getattr(current, attr, None)
            if sock is not None and hasattr(sock, "settimeout"):
                return sock
        nxt = getattr(current, "raw", None)
        if nxt is None:
            nxt = getattr(current, "fp", None)
        current = nxt
    return None


def host_of(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_url(url: str) -> str:
    raw = (url or "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw
    if not parts.scheme or not parts.netloc:
        return raw
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if parts.port and parts.port not in {80, 443}:
        netloc = f"{host}:{parts.port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query_pairs = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING
    ]
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, path, urllib.parse.urlencode(query_pairs), ""))


def normalize_title(title: str) -> str:
    text = fold(title).replace("'", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def title_key(title: str) -> str | None:
    key = normalize_title(title)
    if len(key) < 12 or len(key.split()) < 3:
        return None
    return key


def is_obituary(text: str) -> bool:
    value = text or ""
    if _OBITUARY.search(value):
        return True
    if not _DEATH.search(value):
        return False
    # "dies at sea" is a wreck headline, not a death notice. Any other death word still drops it.
    remainder = _DIES_AT_SEA.sub(" ", value)
    return bool(_DEATH.search(remainder))


def is_spam(text: str) -> bool:
    if _SPAM.search(text or ""):
        return True
    return bool(_LAST_CHANCE.search(text or "") and _INVESTING_WORD.search(text or ""))


def split_google_outlet(title: str, url: str) -> tuple[str, str | None]:
    if host_of(url) != "news.google.com" or " - " not in title:
        return title, None
    head, tail = title.rsplit(" - ", 1)
    head = head.strip()
    tail = tail.strip()
    if not head or not tail or len(tail) > 48 or len(tail.split()) > 6:
        return title, None
    return head, tail


def echo_stripped(title: str, summary: str) -> str:
    """Return summary text that is not just the headline plus an outlet name."""
    title_norm = fold(title)
    original = clean_spaces(summary)
    summary_norm = fold(original)
    if not summary_norm:
        return ""
    if not title_norm:
        return original
    if summary_norm == title_norm or (title_norm in summary_norm and len(summary_norm) - len(title_norm) < 40):
        return ""
    if summary_norm.startswith(title_norm):
        remainder = summary_norm[len(title_norm) :].strip(" -:|")
        if len(remainder.split()) < 8:
            return ""
        raw_title = clean_spaces(title)
        if original.casefold().startswith(raw_title.casefold()):
            trimmed = original[len(raw_title) :].lstrip(" -:|")
            if len(fold(trimmed).split()) >= 8:
                return trimmed
        return original
    return original


def is_title_echo(title: str, summary: str) -> bool:
    return bool(fold(summary)) and echo_stripped(title, summary) == ""


def _protect_abbreviations(text: str) -> str:
    return _ABBREV.sub(lambda match: match.group(0).replace(".", "\u2024"), text)


def _restore_abbreviations(text: str) -> str:
    return text.replace("\u2024", ".")


def sentence_pieces(text: str) -> list[str]:
    protected = _protect_abbreviations(text)
    parts = re.split(r"(?<=[.!?])\s+", protected)
    return [_restore_abbreviations(part).strip() for part in parts if part.strip()]


def fit_summary(title: str, summary: str, max_chars: int) -> str | None:
    text = echo_stripped(title, summary)
    if not text:
        return None
    pieces = sentence_pieces(text)
    if len(pieces) > 2:
        text = " ".join(pieces[:2]).strip()
    if len(text) > max_chars:
        pieces = sentence_pieces(text)
        if pieces and len(pieces[0]) <= max_chars and len(pieces[0]) >= 30:
            text = pieces[0]
        else:
            window = _protect_abbreviations(text[:max_chars])
            ends = [match.end() for match in re.finditer(r"[.!?]", window)]
            if not ends or ends[-1] < 30:
                return None
            text = _restore_abbreviations(window[: ends[-1]]).strip()
    if text and text[-1] not in ".!?":
        if len(text) + 1 <= max_chars:
            text += "."
        else:
            return None
    if len(text) < 30 or len(text) > max_chars:
        return None
    if is_title_echo(title, text) or _MARKDOWN.search(text):
        return None
    if "\n" in text or "http://" in text.casefold() or "https://" in text.casefold():
        return None
    return text


def short_date(value: str | None) -> str:
    if not value:
        return ""
    parsed: dt.datetime | None = None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return ""
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).strftime("%d %b %Y")


def prepare_candidate(item: dict) -> dict | None:
    raw_title = clean_spaces(str(item.get("title") or item.get("headline") or ""))
    url = str(item.get("url") or "").strip()
    if not raw_title or not url:
        return None
    display, outlet = split_google_outlet(raw_title, url)
    prepared = {
        "title": display,
        "raw_title": raw_title,
        "url": url,
        "published_at": item.get("published_at"),
        "summary": clean_spaces(str(item.get("summary") or "")),
    }
    if outlet:
        prepared["source_label"] = outlet
    elif item.get("source_label"):
        prepared["source_label"] = clean_spaces(str(item["source_label"]))
    return prepared


def fallback_blurb(item: dict) -> str:
    source = item.get("source_label") or host_of(item["url"]) or "the source"
    stamp = short_date(item.get("published_at"))
    if stamp:
        return f"From {source}, {stamp}."
    return f"From {source}."


def _candidate_blob(item: dict) -> str:
    return f"{item.get('raw_title') or item.get('title') or ''} {item.get('summary') or ''}"


class Seen:
    """Recent printed items, plus items already chosen for this edition."""

    def __init__(self, history: list[dict], digest_date: str, keep_days: int = 7):
        try:
            cutoff = (dt.date.fromisoformat(digest_date) - dt.timedelta(days=keep_days)).isoformat()
        except ValueError:
            cutoff = ""
        self.history_urls: set[str] = set()
        self.history_titles: set[str] = set()
        for record in history:
            recorded_date = str(record.get("digest_date") or "")
            # The same calendar day can be rebuilt after a crash. Only earlier days block a pick.
            if not recorded_date or recorded_date == digest_date or recorded_date < cutoff:
                continue
            url = str(record.get("url") or "")
            if url:
                self.history_urls.add(url)
            key = title_key(str(record.get("title") or ""))
            if key:
                self.history_titles.add(key)
        self.chosen_urls: set[str] = set()
        self.chosen_titles: set[str] = set()

    def _match(self, item: dict, urls: set[str], titles: set[str]) -> bool:
        if normalize_url(item["url"]) in urls:
            return True
        key = title_key(item["title"])
        return bool(key and key in titles)

    def in_history(self, item: dict) -> bool:
        return self._match(item, self.history_urls, self.history_titles)

    def already_chosen(self, item: dict) -> bool:
        return self._match(item, self.chosen_urls, self.chosen_titles)

    def repeat(self, item: dict) -> bool:
        return self.in_history(item) or self.already_chosen(item)

    def mark(self, item: dict) -> None:
        self.chosen_urls.add(normalize_url(item["url"]))
        key = title_key(item["title"])
        if key:
            self.chosen_titles.add(key)


def eligible_candidates(raw_items: list[dict], seen: Seen) -> list[dict]:
    chosen: list[dict] = []
    seen_urls: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        prepared = prepare_candidate(raw)
        if prepared is None:
            continue
        if is_obituary(_candidate_blob(prepared)) or is_spam(_candidate_blob(prepared)):
            continue
        url_key = normalize_url(prepared["url"])
        if url_key in seen_urls or seen.in_history(prepared):
            continue
        seen_urls.add(url_key)
        chosen.append(prepared)
    return chosen


def _matches_terms(item: dict, terms: tuple[str, ...]) -> bool:
    if not terms:
        return False
    haystack = f"{item['title']} {item.get('summary') or ''}".casefold()
    return any(term and term in haystack for term in terms)


def _public_item(item: dict) -> dict:
    published = {
        "title": item["title"],
        "summary": item["summary"],
        "url": strip_userinfo(item["url"]),
        "published_at": item.get("published_at"),
        "rationale": item.get("rationale") or "Selected for this section.",
    }
    label = item.get("source_label")
    if label:
        published["source_label"] = label
    return published


def _history_record(section_id: str, digest_date: str, item: dict) -> dict:
    return {
        "digest_date": digest_date,
        "section": section_id,
        "url": normalize_url(item["url"])[:HISTORY_URL_MAX],
        "title": normalize_title(item["title"])[:HISTORY_TITLE_MAX],
    }


def fallback_from_eligible(
    eligible: list[dict],
    count: int,
    seen: Seen,
    prefer_terms: tuple[str, ...],
    max_chars: int,
) -> list[dict]:
    preferred_ids: set[int] = set()
    preferred: list[dict] = []
    if prefer_terms:
        for item in eligible:
            if _matches_terms(item, prefer_terms):
                preferred.append(item)
                preferred_ids.add(id(item))
    ordered = preferred + [item for item in eligible if id(item) not in preferred_ids]
    chosen: list[dict] = []
    for item in ordered:
        if seen.repeat(item):
            continue
        summary = fit_summary(item["title"], item.get("summary") or "", max_chars)
        if summary is None:
            summary = fallback_blurb(item)
        picked = dict(item)
        picked["summary"] = summary
        picked["rationale"] = "Fallback pick: newest item that was not an obituary, ad, or repeat."
        picked["record_history"] = True
        seen.mark(picked)
        chosen.append(picked)
        if len(chosen) >= count:
            break
    return chosen


def placeholder_item(feed_url: str) -> dict:
    return {
        "title": PLACEHOLDER_TITLE,
        "summary": PLACEHOLDER_SUMMARY,
        "url": feed_url,
        "published_at": None,
        "rationale": PLACEHOLDER_SUMMARY,
    }


def fallback_pick(
    candidates: list[dict],
    count: int,
    seen: Seen,
    *,
    prefer_terms: tuple[str, ...] = (),
    max_chars: int = 300,
    feed_url: str = "",
) -> list[dict]:
    eligible = eligible_candidates(candidates, seen)
    chosen = fallback_from_eligible(eligible, count, seen, prefer_terms, max_chars)
    if chosen:
        return [_public_item(item) for item in chosen]
    return [placeholder_item(feed_url or "https://example.invalid/feed")]


def _chat_url() -> str:
    base = settings.llm_base_url()
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LLMError("bad_base_url")
    return base + "/chat/completions"


def _system_prompt(count: int, max_chars: int) -> str:
    return (
        "You choose items for a morning news receipt shared by two adults. "
        "Pick the most newsworthy and interesting candidates. Reject obituaries, "
        "advertisements, clickbait, and investing spam such as last-chance-to-buy alerts. "
        "Prefer concrete events over commentary. Follow the taste note and each section's purpose. "
        f"Use only the candidate urls, copied exactly. Choose at most {count} items per section "
        "and use each url at most once. Do not invent items. "
        f"Write each summary as one or two sentences in plain words, at most {max_chars} characters, "
        "without repeating the headline, and without markdown or emoji. "
        'Return one JSON object and nothing else. It must have one key, "picks". '
        'Each pick has "section" (the section title) and "items". '
        'Each item has only "url" and "summary".'
    )


def _user_payload(groups: list[tuple[str, str, list[dict]]], taste: str, count: int, max_chars: int) -> str:
    sections = []
    for title, purpose, eligible in groups:
        candidates = []
        for item in eligible:
            summary = echo_stripped(item["title"], item.get("summary") or "")
            candidates.append(
                {
                    "title": item["title"],
                    "url": item["url"],
                    "published_at": item.get("published_at"),
                    "summary": summary[:320],
                }
            )
        sections.append({"section": title, "purpose": purpose, "candidates": candidates})
    return json.dumps(
        {
            "taste": taste,
            "items_per_section": count,
            "summary_max_chars": max_chars,
            "sections": sections,
        },
        ensure_ascii=False,
    )


def parse_llm_content(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError("bad_json") from exc
    if not isinstance(value, dict):
        raise LLMError("bad_shape")
    return value


def validate_picks(value: dict, allowed_sections: set[str]) -> list[dict]:
    if set(value) != {"picks"}:
        raise LLMError("bad_shape")
    picks = value["picks"]
    if not isinstance(picks, list):
        raise LLMError("bad_shape")
    seen_sections: set[str] = set()
    normalized: list[dict] = []
    for pick in picks:
        if not isinstance(pick, dict) or set(pick) != {"section", "items"}:
            raise LLMError("bad_shape")
        section = pick["section"]
        items = pick["items"]
        if not isinstance(section, str) or section not in allowed_sections or section in seen_sections:
            raise LLMError("bad_shape")
        if not isinstance(items, list):
            raise LLMError("bad_shape")
        seen_sections.add(section)
        cleaned_items = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"url", "summary"}:
                raise LLMError("bad_shape")
            if not isinstance(item["url"], str) or not isinstance(item["summary"], str):
                raise LLMError("bad_shape")
            cleaned_items.append({"url": item["url"], "summary": item["summary"]})
        normalized.append({"section": section, "items": cleaned_items})
    return normalized


def _message_text(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise LLMError("bad_shape")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMError("bad_shape")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise LLMError("bad_shape")
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        joined = "".join(texts).strip()
        if joined:
            return joined
    raise LLMError("empty_content")


def _call_timeout(deadline: float | None, cap: float) -> float:
    if deadline is None:
        return cap
    left = deadline - time.monotonic()
    if left < 0.5:
        raise LLMError("timeout")
    return min(cap, left)


def _post_chat(url: str, key: str, body: dict, opener, timeout: float, deadline: float | None) -> str:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": "daily-digest/2.0",
        },
        method="POST",
    )
    try:
        opened = (
            open_bounded(request, timeout, deadline)
            if opener is _default_opener
            else opener(request, timeout=timeout)
        )
        with opened as response:
            raw = read_bounded(response, RESPONSE_MAX_BYTES, deadline)
    except LLMError:
        raise
    except TimeoutError as exc:
        raise LLMError("timeout") from exc
    except urllib.error.HTTPError as exc:
        raise LLMError(f"http_{exc.code}") from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            raise LLMError("timeout") from exc
        raise LLMError("network") from exc
    except Exception as exc:
        raise LLMError("network") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise LLMError("bad_json") from exc
    return _message_text(payload)


def llm_pick(
    groups: list[tuple[str, str, list[dict]]],
    *,
    count: int,
    max_chars: int,
    taste: str,
    opener,
    deadline: float | None = None,
) -> list[dict]:
    key = settings.llm_api_key()
    if not key or any(char.isspace() for char in key):
        raise LLMError("no_api_key")
    url = _chat_url()
    messages = [
        {"role": "system", "content": _system_prompt(count, max_chars)},
        {"role": "user", "content": _user_payload(groups, taste, count, max_chars)},
    ]
    full = {
        "model": settings.llm_model(),
        "temperature": 0,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    effort = settings.llm_reasoning_effort()
    if effort:
        full["reasoning_effort"] = effort
    cap = settings.llm_timeout()
    try:
        try:
            content = _post_chat(url, key, full, opener, _call_timeout(deadline, cap), deadline)
        except LLMError as exc:
            # Some compatible servers reject reasoning_effort, temperature, or max_tokens.
            # The retry spends only the time still left on the same deadline.
            if exc.code != "http_400":
                raise
            minimal = {
                "model": settings.llm_model(),
                "response_format": {"type": "json_object"},
                "messages": messages,
            }
            content = _post_chat(url, key, minimal, opener, _call_timeout(deadline, cap), deadline)
        return validate_picks(parse_llm_content(content), {title for title, _purpose, _items in groups})
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError("network") from exc


def _default_opener(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout)


def _picker_error(code: str) -> dict:
    return {
        "section": "PICKER",
        "url": strip_userinfo(settings.llm_base_url()),
        "error": f"llm fallback: {code}",
    }


def pick_news(
    requests: list[dict],
    *,
    digest_date: str,
    history: list[dict],
    has_bots: bool,
    taste: str | None = None,
    opener=None,
    item_count: int | None = None,
    summary_chars: int | None = None,
    deadline: float | None = None,
) -> tuple[dict[str, list[dict]], dict, list[dict], list[dict]]:
    """Pick items for each news section.

    ``requests`` entries need section_id, title, purpose, feed_url, prefer_terms,
    and candidates. Returns items by section id, picker metadata, source errors,
    and history records.
    """
    count = item_count if item_count is not None else settings.news_item_count(has_bots)
    max_chars = summary_chars if summary_chars is not None else settings.summary_limit(has_bots)
    seen = Seen(history, digest_date, settings.history_days())
    prepared: list[tuple[dict, list[dict]]] = []
    for request in requests:
        prepared.append((request, eligible_candidates(request.get("candidates") or [], seen)))

    opener = opener or _default_opener
    llm_picks = None
    reason = "no_api_key" if not settings.llm_api_key() else "no_candidates"
    errors: list[dict] = []
    if settings.llm_api_key() and any(eligible for _request, eligible in prepared):
        groups = [
            (request["title"], request.get("purpose") or "", eligible)
            for request, eligible in prepared
            if eligible
        ]
        try:
            llm_picks = llm_pick(
                groups,
                count=count,
                max_chars=max_chars,
                taste=taste if taste is not None else settings.taste(),
                opener=opener,
                deadline=deadline,
            )
            reason = "ok"
        except LLMError as exc:
            reason = exc.code
            llm_picks = None
            if exc.code != "no_api_key":
                errors.append(_picker_error(exc.code))
        except Exception:
            reason = "network"
            llm_picks = None
            errors.append(_picker_error("network"))

    picks_by_title: dict[str, list[dict]] = {}
    if llm_picks:
        picks_by_title = {pick["section"]: pick["items"] for pick in llm_picks}
        if not any(pick["items"] for pick in llm_picks):
            reason = "empty_picks"

    used_llm = False
    used_fallback = False
    items_by_id: dict[str, list[dict]] = {}
    records: list[dict] = []
    for request, eligible in prepared:
        chosen: list[dict] = []
        if llm_picks is not None:
            by_url = {item["url"]: item for item in eligible}
            for pick in picks_by_title.get(request["title"], []):
                if len(chosen) >= count:
                    break
                candidate = by_url.get(pick["url"])
                if candidate is None or seen.repeat(candidate):
                    continue
                summary = fit_summary(candidate["title"], pick["summary"], max_chars)
                if summary is None:
                    continue
                picked = dict(candidate)
                picked["summary"] = summary
                picked["rationale"] = "LLM pick: newsworthy for this section and the configured taste."
                picked["record_history"] = True
                seen.mark(picked)
                chosen.append(picked)
                used_llm = True
        if len(chosen) < count:
            extra = fallback_from_eligible(
                eligible,
                count - len(chosen),
                seen,
                tuple(request.get("prefer_terms") or ()),
                max_chars,
            )
            if extra:
                if llm_picks is not None:
                    used_fallback = True
                chosen.extend(extra)
        if not chosen:
            public = [placeholder_item(request["feed_url"])]
        else:
            public = []
            for item in chosen:
                if item.get("record_history"):
                    records.append(_history_record(request["section_id"], digest_date, item))
                public.append(_public_item(item))
        items_by_id[request["section_id"]] = public

    if llm_picks is None:
        mode = "fallback"
    elif used_llm and used_fallback:
        mode = "mixed"
        reason = "ok"
    elif used_llm:
        mode = "llm"
        reason = "ok"
    else:
        mode = "fallback"
        if reason == "ok":
            reason = "no_accepted_picks"
            errors.append(_picker_error("no_accepted_picks"))
    meta = {"mode": mode, "model": settings.llm_model() if settings.llm_api_key() else None, "reason": reason}
    return items_by_id, meta, errors, records


def load_history(path: Path) -> tuple[list[dict], str | None]:
    try:
        if not path.exists():
            return [], None
        with path.open("rb") as handle:
            raw = handle.read(HISTORY_MAX_BYTES + 1)
        if len(raw) > HISTORY_MAX_BYTES:
            return [], "history ignored: unreadable"
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        return [], "history ignored: unreadable"
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("items"), list):
        return [], "history ignored: invalid"
    items: list[dict] = []
    for item in value["items"]:
        if not isinstance(item, dict):
            continue
        recorded_date = item.get("digest_date")
        url = item.get("url")
        title = item.get("title")
        if isinstance(recorded_date, str) and isinstance(url, str) and isinstance(title, str):
            items.append(
                {
                    "digest_date": recorded_date[:32],
                    "section": item.get("section") if isinstance(item.get("section"), str) else "",
                    "url": strip_userinfo(url)[:HISTORY_URL_MAX],
                    "title": title[:HISTORY_TITLE_MAX],
                }
            )
    return items, None


def _printer_lock_path() -> Path:
    raw = os.environ.get("DIGEST_LOCK_PATH", "").strip()
    if raw:
        return Path(raw)
    return settings.project_dir() / "print.lock"


@contextlib.contextmanager
def _printer_lock():
    """Hold the same print.lock the shell uses, unless this process is already inside it.

    The shell exports DIGEST_LOCK_HELD=1 while flock is held. Taking the lock again
    from that child would deadlock. A direct second generator blocks here instead.
    """
    if os.environ.get("DIGEST_LOCK_HELD") == "1":
        yield
        return
    path = _printer_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def save_history(path: Path, digest_date: str, records: list[dict], keep_days: int | None = None) -> bool:
    """Merge today's records. Return False and leave the file untouched when it cannot be read."""
    with _printer_lock():
        if path.exists():
            previous, error = load_history(path)
            if error:
                return False
        else:
            previous = []
        days = settings.history_days() if keep_days is None else keep_days
        cutoff = (dt.date.fromisoformat(digest_date) - dt.timedelta(days=days)).isoformat()
        kept = [
            item
            for item in previous
            if item["digest_date"] >= cutoff and item["digest_date"] != digest_date
        ]
        for record in records:
            kept.append(
                {
                    "digest_date": str(record.get("digest_date") or digest_date)[:32],
                    "section": str(record.get("section") or "")[:40],
                    "url": strip_userinfo(str(record.get("url") or ""))[:HISTORY_URL_MAX],
                    "title": str(record.get("title") or "")[:HISTORY_TITLE_MAX],
                }
            )
        _atomic_json(path, {"version": 1, "items": kept})
        return True


def _parse_generated_at(value: object) -> dt.datetime:
    if not isinstance(value, str) or not value.strip():
        raise BotUpdatesError("invalid_schema")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise BotUpdatesError("invalid_schema") from exc
    if parsed.tzinfo is None:
        raise BotUpdatesError("invalid_schema")
    return parsed.astimezone(dt.timezone.utc)


def _xml_char(char: str) -> bool:
    code = ord(char)
    return code in (0x9, 0xA, 0xD) or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF


def _require_line(value: object, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise BotUpdatesError("invalid_schema")
    text = value.strip()
    if not minimum <= len(text) <= maximum:
        raise BotUpdatesError("invalid_schema")
    if any(not _xml_char(char) or ord(char) < 32 for char in text) or "<" in text or ">" in text:
        raise BotUpdatesError("invalid_schema")
    return text


def validate_bot_document(
    value: object,
    *,
    now: dt.datetime,
    audiences: set[str] | frozenset[str] | None = None,
    max_age_hours: int | None = None,
    max_updates: int | None = None,
    future_skew_minutes: int | None = None,
) -> list[dict]:
    if not isinstance(value, dict) or set(value) != {"generated_at", "updates"}:
        raise BotUpdatesError("invalid_schema")
    generated_at = _parse_generated_at(value["generated_at"])
    moment = now if now.tzinfo else now.replace(tzinfo=dt.timezone.utc)
    moment = moment.astimezone(dt.timezone.utc)
    age = moment - generated_at
    max_age = settings.bot_max_age_hours() if max_age_hours is None else max_age_hours
    if age > dt.timedelta(hours=max_age):
        raise BotUpdatesError("stale")
    skew = settings.bot_future_skew_minutes() if future_skew_minutes is None else future_skew_minutes
    if generated_at - moment > dt.timedelta(minutes=skew):
        raise BotUpdatesError("future")
    updates = value["updates"]
    if not isinstance(updates, list):
        raise BotUpdatesError("invalid_schema")
    limit = settings.max_bot_updates() if max_updates is None else max_updates
    if len(updates) > limit:
        raise BotUpdatesError("invalid_schema")
    allowed = set(settings.audiences() if audiences is None else audiences)
    items: list[dict] = []
    for update in updates:
        if not isinstance(update, dict):
            raise BotUpdatesError("invalid_schema")
        keys = set(update)
        if not {"bot", "topic", "text", "for"} <= keys <= {"bot", "topic", "text", "for", "url"}:
            raise BotUpdatesError("invalid_schema")
        bot = _require_line(update["bot"], 1, 40)
        topic = _require_line(update["topic"], 1, 48)
        text = _require_line(update["text"], 8, BOT_TEXT_MAX)
        audience = update["for"]
        if not isinstance(audience, str) or audience not in allowed:
            raise BotUpdatesError("invalid_schema")
        url = update.get("url")
        if url is not None:
            if not isinstance(url, str) or len(url) > 300 or not url.startswith("https://") or any(char.isspace() for char in url):
                raise BotUpdatesError("invalid_schema")
            url = strip_userinfo(url)
            if not url.startswith("https://") or len(url) > 300:
                raise BotUpdatesError("invalid_schema")
        title = f"{bot}: {topic}" if audience == "both" else f"{bot}: {topic} ({audience})"
        item = {
            "title": title,
            "summary": text,
            "source_label": bot,
            "published_at": value["generated_at"].strip(),
            "audience": audience,
            "rationale": "Bot update dropped off for this digest date.",
        }
        if isinstance(url, str):
            item["url"] = url
        items.append(item)
    return items


def _read_regular_nofollow(path: Path, limit: int) -> bytes:
    """Read a regular file, refusing a symlink, and stop after ``limit`` bytes."""
    try:
        info = os.lstat(path)
    except OSError:
        raise
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OSError(errno.ELOOP, "refusing non-regular bot update file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "bot update file is not regular")
        chunks: list[bytes] = []
        remaining = limit
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def load_bot_updates(
    directory: Path,
    digest_date: str,
    now: dt.datetime,
) -> tuple[list[dict] | None, str | None]:
    """Return receipt items, or None when the section should be left out.

    A missing file is quiet. A stale, invalid, linked, or hostile file returns
    an error code and no items, so the other sections can use the space.
    """
    try:
        return _load_bot_updates(directory, digest_date, now)
    except Exception:
        return None, "bot updates ignored: unreadable"


def _load_bot_updates(
    directory: Path,
    digest_date: str,
    now: dt.datetime,
) -> tuple[list[dict] | None, str | None]:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", digest_date):
            raise ValueError
        dt.date.fromisoformat(digest_date)
    except ValueError:
        return None, "bot updates ignored: invalid date"
    path = directory / f"{digest_date}.json"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "bot updates ignored: unreadable"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, "bot updates ignored: unreadable"
    try:
        raw = _read_regular_nofollow(path, BOT_MAX_BYTES + 1)
    except OSError:
        return None, "bot updates ignored: unreadable"
    if len(raw) > BOT_MAX_BYTES:
        return None, "bot updates ignored: file is too large"
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "bot updates ignored: invalid JSON"
    try:
        items = validate_bot_document(value, now=now)
    except BotUpdatesError as exc:
        label = {
            "stale": "bot updates ignored: stale",
            "future": "bot updates ignored: generated_at is in the future",
            "invalid_schema": "bot updates ignored: invalid schema",
        }.get(exc.code, "bot updates ignored: invalid schema")
        return None, label
    except Exception:
        return None, "bot updates ignored: invalid schema"
    if not items:
        return None, None
    return items, None
