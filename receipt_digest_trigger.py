#!/usr/bin/env python3
"""Loopback-only fixed-action HTTP bridge for Home Assistant."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import threading

BRIDGE = str(Path(__file__).resolve().parent / "print_digest_safe.sh")
BIND = "127.0.0.1"
PORT = int(os.environ.get("DIGEST_TRIGGER_PORT", "8793"))
TRIGGER_TIMEOUT = float(os.environ.get("DIGEST_TRIGGER_TIMEOUT", "120") or "120")
LOCK = threading.Lock()


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
    except subprocess.TimeoutExpired:
        kill_process_group(proc)
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
        except subprocess.TimeoutExpired:
            self.send_json(504, {"error": "bridge_timeout"})
            return
        finally:
            LOCK.release()
        value = bridge_body(result.returncode, result.stdout)
        self.send_json(200 if result.returncode == 0 else 500, value)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"receipt digest trigger listening on http://{BIND}:{PORT}", flush=True)
    server.serve_forever()
