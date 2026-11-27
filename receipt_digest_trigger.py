#!/usr/bin/env python3
"""Loopback-only fixed-action HTTP bridge for Home Assistant."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import urllib.parse

BRIDGE = str(Path(__file__).resolve().parent / "print_digest_safe.sh")
BIND = "127.0.0.1"
PORT = int(os.environ.get("DIGEST_TRIGGER_PORT", "8793"))
TRIGGER_TIMEOUT = float(os.environ.get("DIGEST_TRIGGER_TIMEOUT", "120") or "120")
LOCK = threading.Lock()
# Keep the generator's closing JSON (it is redirected onto stderr) and the start of a traceback.
JOURNAL_MAX_CHARS = 8000
_SECRET_ENV = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION)", re.IGNORECASE)
_BEARER = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+")
_ENV_ASSIGNMENT = re.compile(
    r"(?im)^([A-Za-z_][A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION)[A-Za-z0-9_]*)=.*$"
)
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_URL_SECRET_QUERY = frozenset(
    {"apikey", "api_key", "key", "token", "secret", "password", "access_token", "sig"}
)


def kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the bridge and every child. Killing only flock leaves the print holding the lock."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def _secret_forms(value: str) -> list[str]:
    """The secret and the percent-encoded copies a URL or a log line might carry."""
    forms: list[str] = []
    for form in (
        value,
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value),
    ):
        if form and len(form) >= 8 and form not in forms:
            forms.append(form)
    return forms


def redact_logged_url(raw: str) -> str:
    """Drop userinfo and the values of credential query keys. Leave the rest of the URL."""
    trimmed = raw.rstrip(").,;")
    suffix = raw[len(trimmed) :]
    try:
        parts = urllib.parse.urlsplit(trimmed)
    except ValueError:
        return raw
    if not parts.scheme or not parts.hostname:
        return raw
    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    rebuilt = []
    for key, value in pairs:
        shown = "[redacted]" if key.casefold() in _URL_SECRET_QUERY else urllib.parse.quote(value, safe="")
        rebuilt.append(f"{urllib.parse.quote(key, safe='')}={shown}")
    query = "&".join(rebuilt)
    cleaned = urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    return cleaned + suffix


def redact_secrets(text: str) -> str:
    """Drop secret env values, URL credentials, and credential assignments.

    Feed URLs often carry ``apikey`` (or a percent-encoded copy of a known secret)
    without the variable name itself matching KEY or TOKEN. Userinfo is removed
    from every URL. Never log the environment itself.
    """
    redacted = _URL_IN_TEXT.sub(lambda match: redact_logged_url(match.group(0)), text or "")
    forms: list[str] = []
    for name, value in os.environ.items():
        if not value or len(value) < 8 or not _SECRET_ENV.search(name):
            continue
        forms.extend(_secret_forms(value))
    for form in sorted(set(forms), key=len, reverse=True):
        redacted = redacted.replace(form, "[redacted]")
    redacted = _BEARER.sub(r"\1[redacted]", redacted)
    redacted = _ENV_ASSIGNMENT.sub(r"\1=[redacted]", redacted)
    return redacted


def prepare_journal(text: str) -> str:
    """Redact secrets, then keep the tail when the log is long.

    The shell script sends the generator's stdout to stderr, and that JSON is the
    decision line at the end.
    """
    cleaned = redact_secrets(text or "").strip()
    if len(cleaned) <= JOURNAL_MAX_CHARS:
        return cleaned
    note = "\n… truncated …\n"
    tail = JOURNAL_MAX_CHARS - len(note)
    return note + cleaned[-tail:]


def log_subprocess_stderr(text: str) -> None:
    """Write bridge stderr to this process's stderr, which the journal captures."""
    cleaned = prepare_journal(text)
    if not cleaned:
        return
    for line in cleaned.splitlines():
        print(f"receipt-digest-trigger: {line}", file=sys.stderr, flush=True)


def _exception_stderr(exc: subprocess.TimeoutExpired) -> str:
    value = getattr(exc, "stderr", None)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return ""


def run_bridge(mode: str, timeout: float | None = None, bridge: str | None = None) -> subprocess.CompletedProcess:
    limit = TRIGGER_TIMEOUT if timeout is None else timeout
    proc = subprocess.Popen(
        [bridge or BRIDGE, mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired as exc:
        kill_process_group(proc)
        try:
            _stdout, stderr = proc.communicate(timeout=2)
        except Exception:
            stderr = ""
        exc.stderr = stderr if isinstance(stderr, str) else ""
        raise
    return subprocess.CompletedProcess([bridge or BRIDGE, mode], proc.returncode or 0, stdout, stderr)


def bridge_body(returncode: int, stdout: str) -> dict:
    """Status for Home Assistant. Stderr stays off this response."""
    value: dict = {"status": "ok" if returncode == 0 else "error", "returncode": returncode}
    text = (stdout or "").strip()
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            value["result"] = parsed
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "ReceiptDigestTrigger/1"

    def send_json(self, status: int, value: dict) -> None:
        payload = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_json(404, {"error": "not_found"})
            return
        self.send_json(200, {"status": "ok", "bind": BIND, "port": PORT})

    def do_POST(self) -> None:
        modes = {
            "/v1/digest/dry-run": "--dry-run",
            "/v1/digest/submit": "--submit",
            "/v1/digest/reprint": "--reprint",
            "/v1/digest/rebuild": "--rebuild",
        }
        mode = modes.get(self.path)
        if not mode:
            self.send_json(404, {"error": "not_found"})
            return
        if self.headers.get("Content-Length") not in {None, "0"}:
            self.send_json(400, {"error": "request_body_not_allowed"})
            return
        if not LOCK.acquire(blocking=False):
            self.send_json(409, {"error": "print_action_in_progress"})
            return
        try:
            result = run_bridge(mode)
        except subprocess.TimeoutExpired as exc:
            log_subprocess_stderr(_exception_stderr(exc))
            self.send_json(504, {"error": "bridge_timeout"})
            return
        finally:
            LOCK.release()
        log_subprocess_stderr(result.stderr)
        value = bridge_body(result.returncode, result.stdout)
        self.send_json(200 if result.returncode == 0 else 500, value)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"receipt digest trigger listening on http://{BIND}:{PORT}", flush=True)
    server.serve_forever()
