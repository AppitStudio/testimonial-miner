"""Small, dependency-free local web server for exploring testimonial results."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ASSET_DIR = Path(__file__).with_name("web_assets")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


class DashboardServer(ThreadingHTTPServer):
    data_dir: Path


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def _send(self, status: int, body: bytes, content_type: str,
              *, cache_control: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; "
                         "script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: object, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json({"ok": True})
            return
        if path == "/api/testimonials":
            db_path = self.server.data_dir / "testimonials.json"
            try:
                with db_path.open() as f:
                    payload = json.load(f)
            except FileNotFoundError:
                self._json({"version": 1, "updated_at": None, "items": []})
                return
            except (OSError, json.JSONDecodeError) as exc:
                self._json({"error": f"Could not read testimonials.json: {exc}"}, status=500)
                return
            self._json(payload)
            return
        asset = ASSETS.get(path)
        if asset:
            filename, content_type = asset
            try:
                body = (ASSET_DIR / filename).read_bytes()
            except OSError:
                self._send(500, b"Dashboard asset missing", "text/plain; charset=utf-8")
                return
            self._send(200, body, content_type, cache_control="no-cache")
            return
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args) -> None:
        if self.path != "/api/health":
            super().log_message(format, *args)


def serve_dashboard(data_dir: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = DashboardServer((host, port), DashboardHandler)
    server.data_dir = Path(data_dir)
    actual_host, actual_port = server.server_address[:2]
    display_host = "127.0.0.1" if actual_host in {"0.0.0.0", "::"} else actual_host
    print(f"Testimonial dashboard: http://{display_host}:{actual_port}")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
