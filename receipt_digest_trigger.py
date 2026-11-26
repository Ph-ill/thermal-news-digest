#!/usr/bin/env python3
"""Loopback-only fixed-action HTTP bridge for Home Assistant."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading

BRIDGE = str(Path(__file__).resolve().parent / "print_digest_safe.sh")
BIND = "127.0.0.1"
PORT = int(os.environ.get("DIGEST_TRIGGER_PORT", "8793"))
LOCK = threading.Lock()


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
            result = subprocess.run(
                [BRIDGE, mode], capture_output=True, text=True, timeout=120
            )
        except subprocess.TimeoutExpired:
            self.send_json(504, {"error": "bridge_timeout"})
            return
        finally:
            LOCK.release()
        stdout = result.stdout.strip()
        try:
            bridge_result = json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            bridge_result = {"raw_stdout": stdout}
        value = {
            "status": "ok" if result.returncode == 0 else "error",
            "returncode": result.returncode,
            "result": bridge_result,
        }
        if result.stderr.strip():
            value["stderr"] = result.stderr.strip()
        self.send_json(200 if result.returncode == 0 else 500, value)

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"receipt digest trigger listening on http://{BIND}:{PORT}", flush=True)
    server.serve_forever()
