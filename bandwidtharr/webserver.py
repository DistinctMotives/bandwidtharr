import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from bandwidtharr.state import SharedState

INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_bytes()


def _make_handler(state: SharedState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, INDEX_HTML, "text/html; charset=utf-8")
            elif self.path == "/api/state":
                snapshot = state.snapshot()
                # Non-2xx whenever either app is currently unreachable, so
                # the Dockerfile's HEALTHCHECK (which fails on HTTPError)
                # reflects real health, not just "the web server is alive."
                status = 200 if snapshot["qbit_ok"] and snapshot["sab_ok"] else 503
                body = json.dumps(snapshot).encode()
                self._send(status, body, "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def start(state: SharedState, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(state))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
