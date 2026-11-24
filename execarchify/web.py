"""Serve the offline explorer and its four workspace-scoped queries."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
import ipaddress
import json
import socket
from urllib.parse import urlsplit

from .model import Workspace


ASSETS = {
    "/": ("index.html", "text/html"),
    "/index.html": ("index.html", "text/html"),
    "/static/app.css": ("app.css", "text/css"),
    "/static/app.js": ("app.js", "text/javascript"),
    "/static/graph.js": ("graph.js", "text/javascript"),
    "/static/mark.svg": ("mark.svg", "image/svg+xml"),
}
OPERATIONS = {"/api/" + name: name for name in ("context", "resolve", "trace", "impact")}
MAX_BODY = 32 * 1024


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    server_version = "ExecArchify"
    sys_version = ""
    timeout = 20

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The browser can leave a view while its query is still running.
            pass

    def _send(self, status, data, media="application/json", **headers):
        self.send_response(status)
        self.send_header("Content-Type", media + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; "
                         "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, payload, **headers):
        data = json.dumps(payload, separators=(",", ":")).encode("ascii")
        self._send(status, data, **headers)

    def _path(self):
        try:
            host = self.headers.get("Host", "")
            name = urlsplit("//" + host).hostname
            if _loopback(self.server.server_address[0]) and not _loopback(name):
                self._json(403, {"error": "Use the local server address"})
                return None
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + host:
                self._json(403, {"error": "Origin does not match this server"})
                return None
            return urlsplit(self.path).path
        except ValueError:
            self._json(400, {"error": "Invalid request address"})
            return None

    def do_GET(self):
        path = self._path()
        if path is None:
            return
        if path in OPERATIONS:
            self._json(405, {"error": "Send a JSON object using POST"}, Allow="POST")
        elif path in ASSETS:
            filename, media = ASSETS[path]
            data = resources.files("execarchify").joinpath("static", filename).read_bytes()
            self._send(200, data, media)
        else:
            self._json(404, {"error": "Unknown explorer route"})

    do_HEAD = do_GET

    def do_POST(self):
        path = self._path()
        if path is None:
            return
        if path not in OPERATIONS:
            if path in ASSETS:
                self._json(405, {"error": "Use GET for explorer assets"}, Allow="GET, HEAD")
            else:
                self._json(404, {"error": "Unknown query"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Expected application/json"})
            return
        sizes = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") or len(sizes) != 1
                or not sizes[0].isascii() or not sizes[0].isdecimal()):
            self._json(400, {"error": "Expected one Content-Length header"})
            return
        if len(sizes[0]) > 10 or int(sizes[0]) > MAX_BODY:
            self._json(413, {"error": "Query body exceeds 32 KiB"})
            return
        try:
            size = int(sizes[0])
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("Incomplete request body")
            arguments = json.loads(body.decode("utf-8"))
            if not isinstance(arguments, dict):
                raise ValueError("Expected a JSON object")
        except (ValueError, RecursionError) as error:
            self._json(400, {"error": str(error)})
            return
        except TimeoutError:
            self._json(408, {"error": "Incomplete request body"})
            return
        try:
            from .api import call

            result = call(self.server.workspace, OPERATIONS[path], arguments)
        except (ValueError, KeyError, TypeError) as error:
            self._json(400, {"error": str(error)})
            return
        except Exception as error:
            self.log_error("Workspace query failed (%s)", type(error).__name__)
            self._json(500, {"error": "Workspace query could not be completed"})
            return
        self._json(200, result)


def _server(workspace, host, port):
    server_type = ThreadingHTTPServer
    if ":" in host:
        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_type = IPv6Server
    server = server_type((host, port), _Handler)
    server.workspace = workspace
    return server


def serve(workspace: Workspace, host: str = "127.0.0.1", port: int = 8765):
    """Run until interrupted. A zero port asks the OS for an available port."""
    with _server(workspace, host, port) as server:
        address = f"[{host}]" if ":" in host else host
        print(f"ExecArchify http://{address}:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
