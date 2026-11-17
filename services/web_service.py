"""Web console (port 8100): static SPA + reverse proxy.

The browser only ever talks to this process. ``/api/<service>/...`` calls are
proxied over HTTP to the three independently-deployed services, so the UI keeps
working (with degraded sections) if one of them is stopped.
"""
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

UPSTREAMS = {
    "adj": os.environ.get("ADJ_URL", "http://127.0.0.1:8101"),
    "rb": os.environ.get("RB_URL", "http://127.0.0.1:8102"),
    "rpt": os.environ.get("RPT_URL", "http://127.0.0.1:8103"),
}
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


class WebHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method):
        parts = self.path.split("/", 3)  # ['', 'api', 'adj', 'entities...']
        if len(parts) < 4 or parts[2] not in UPSTREAMS:
            return self._send(404, b'{"error":"unknown upstream"}')
        url = f"{UPSTREAMS[parts[2]]}/{parts[3]}"
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self._send(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            self._send(exc.code, exc.read())
        except (urllib.error.URLError, ConnectionError) as exc:
            self._send(503,
                       f'{{"error":"upstream unavailable: {exc}"}}'.encode())

    def _static(self):
        path = self.path.split("?")[0]
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            full = os.path.join(WEB_DIR, "index.html")
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as fh:
            self._send(200, fh.read(), CONTENT_TYPES.get(ext, "text/plain"))

    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._proxy("GET")
        self._static()

    def do_POST(self):
        if self.path.startswith("/api/"):
            return self._proxy("POST")
        self._send(404, b'{"error":"not found"}')


if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", "8100"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), WebHandler)
    print(f"[web] console on http://127.0.0.1:{port}", flush=True)
    httpd.serve_forever()
