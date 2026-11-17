"""Minimal JSON HTTP framework on top of the Python standard library.

Routes are (method, regex) -> handler(conn, body, params). Every service owns
its own server process; they communicate only through the shared SQLite file,
which is what makes them independently startable/stoppable.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .db import connect
from .util import dumps


class App:
    def __init__(self, db_path: str, name: str):
        self.db_path = db_path
        self.name = name
        self.routes = []

    def route(self, method, pattern):
        rx = re.compile("^" + pattern + "$")

        def deco(fn):
            self.routes.append((method, rx, fn))
            return fn

        return deco

    def make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # quiet by default
                pass

            def _send(self, status, payload):
                data = dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                body = None
                if method in ("POST", "PUT", "PATCH"):
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        body = json.loads(raw or b"{}")
                    except json.JSONDecodeError:
                        return self._send(400, {"error": "invalid json"})
                for m, rx, fn in app.routes:
                    if m != method:
                        continue
                    parsed = urlparse(self.path)
                    match = rx.match(parsed.path)
                    if not match:
                        continue
                    query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                    conn = connect(app.db_path)
                    try:
                        result = fn(conn, body or {}, match.groupdict(), query)
                        if isinstance(result, tuple):
                            status, payload = result
                        else:
                            status, payload = 200, result
                        return self._send(status, payload)
                    except Exception as exc:  # pragma: no cover - service safety
                        conn.rollback()
                        return self._send(500, {"error": str(exc)})
                    finally:
                        conn.close()
                self._send(404, {"error": f"no route {method} {self.path}"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        return Handler

    def serve(self, port: str | int):
        httpd = ThreadingHTTPServer(("0.0.0.0", int(port)), self.make_handler())
        print(f"[{self.name}] listening on :{port} (db={self.db_path})", flush=True)
        httpd.serve_forever()
