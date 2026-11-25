#!/usr/bin/env python3
# shellcheck shell=python
"""Management web UI of multica-agent-runtime: workspace data that is not in use can be deleted.

Serves one page (static/) and a small JSON API:

    GET  /api/workspaces          workspaces, sizes, activity, in-use state
    POST /api/workspaces/delete   {"items":[{"dir":..., "confirm":...}], "dry_run":bool}
    GET  /api/health              liveness plus effective configuration

Runs as an unprivileged agent user and needs nothing but Python's standard library. It never
talks to the Multica server; the optional daemon status comes from the daemon's local socket.

    WEBUI_BIND=0.0.0.0 WEBUI_PORT=8090 WEBUI_AUTH=user:secret multica-webui
"""

from __future__ import annotations

import argparse
import base64
import hmac
import ipaddress
import json
import os
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import workspace_store as store  # noqa: E402

VERSION = "1.0.0"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 64 * 1024
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return "" if value == "CHANGEME" else value


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def credentials() -> tuple[str, str] | None:
    """`WEBUI_AUTH=user:password`, or `WEBUI_AUTH_FILE` with one `user:password` line."""
    raw = _env("WEBUI_AUTH")
    path = _env("WEBUI_AUTH_FILE")
    if not raw and path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
            raw = lines[0] if lines else ""
        except (OSError, IndexError):
            raw = ""
    if not raw or ":" not in raw:
        return None
    user, password = raw.split(":", 1)
    return user, password


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


class Handler(BaseHTTPRequestHandler):
    server_version = f"multica-webui/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- plumbing ------------------------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: ANN401 - base class signature
        sys.stderr.write("%s [webui] %s %s\n" % (time.strftime("%H:%M:%S"), self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        headers = dict(extra or {})
        if not getattr(self, "_body_read", True):
            # an unread request body would be parsed as the next request line on a kept-alive socket
            headers["Connection"] = "close"
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            {"Cache-Control": "no-store"},
        )

    def _error(self, status: int, message: str, **extra: Any) -> None:
        payload: dict[str, Any] = {"error": message}
        payload.update(extra)
        self._json(status, payload)

    # --- authentication ------------------------------------------------------------------------

    def _authorized(self) -> bool:
        expected = self.server.auth  # type: ignore[attr-defined]
        if expected is None:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return False
        user, _, password = decoded.partition(":")
        return hmac.compare_digest(user, expected[0]) and hmac.compare_digest(password, expected[1])

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return origin.split("://", 1)[-1] == host

    # --- routing -------------------------------------------------------------------------------

    def _unauthorized(self) -> None:
        self._send(
            HTTPStatus.UNAUTHORIZED,
            json.dumps({"error": "authentication required"}).encode(),
            "application/json; charset=utf-8",
            {"WWW-Authenticate": 'Basic realm="multica webui", charset="UTF-8"', "Cache-Control": "no-store"},
        )

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._body_read = True
        if not self._authorized():
            self._unauthorized()
            return
        path = self.path.split("?", 1)[0]
        if path in STATIC_FILES:
            self._serve_static(*STATIC_FILES[path])
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"status": "ok", "version": VERSION, "config": self._public_config()})
            return
        if path == "/api/workspaces":
            try:
                payload = store.snapshot()
            except Exception as exc:  # noqa: BLE001 - report scan failures instead of dying
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"scan failed: {exc}")
                return
            payload["config"] = self._public_config()
            self._json(HTTPStatus.OK, payload)
            return
        self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self._body_read = False
        if not self._authorized():
            self._unauthorized()
            return
        path = self.path.split("?", 1)[0]
        if path != "/api/workspaces/delete":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if self.server.read_only:  # type: ignore[attr-defined]
            self._error(HTTPStatus.FORBIDDEN, "read-only mode: deletion is disabled")
            return
        # a cross-site form/fetch cannot set this header without a preflight, and the preflight fails
        # for a foreign origin: this is the CSRF guard, Basic credentials alone are not one
        if self.headers.get("X-WebUI") != "1" or not self._same_origin():
            self._error(HTTPStatus.FORBIDDEN, "missing or foreign request header")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._error(HTTPStatus.BAD_REQUEST, "invalid request body")
            return
        try:
            raw = self.rfile.read(length)
            self._body_read = True
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, OSError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return
        items = payload.get("items") if isinstance(payload, dict) else None
        dry_run = bool(payload.get("dry_run")) if isinstance(payload, dict) else False
        try:
            result = store.delete_many(items, dry_run=dry_run)
        except store.DeleteError as exc:
            self._error(exc.status, str(exc), reasons=exc.reasons)
            return
        except Exception as exc:  # noqa: BLE001 - a failed delete must not take the server down
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"delete failed: {exc}")
            return
        if isinstance(items, list) and len(items) == 1 and not result["deleted"]:
            # one request, one verdict: pass the real status instead of a generic 207
            entry = (result["skipped"] or result["failed"])[0]
            self._error(int(entry.get("status") or 400), entry.get("error") or "delete failed", reasons=entry.get("reasons") or [])
            return
        status = HTTPStatus.OK if not result["failed"] else HTTPStatus.MULTI_STATUS
        self._json(status, result)

    # --- helpers -------------------------------------------------------------------------------

    def _public_config(self) -> dict[str, Any]:
        cfg = store.config()
        return {
            "root": cfg["root"],
            "activity_grace_seconds": cfg["activity_grace_seconds"],
            "protect": cfg["protect"],
            "trash": cfg["trash"],
            "keep_trash_hours": cfg["keep_trash_hours"],
            "resolve_names": cfg["resolve_names"],
            "refresh_seconds": _env_int("WEBUI_REFRESH_SECONDS", 15),
            "auth": self.server.auth is not None,  # type: ignore[attr-defined]
            "version": VERSION,
            "read_only": _env("WEBUI_READ_ONLY").strip().lower() in ("1", "true", "yes", "on"),
        }

    def _serve_static(self, name: str, content_type: str) -> None:
        path = os.path.join(STATIC_DIR, name)
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._error(HTTPStatus.NOT_FOUND, "missing static file")
            return
        self._send(HTTPStatus.OK, body, content_type, {"Cache-Control": "no-store"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], auth: tuple[str, str] | None, read_only: bool):
        self.auth = auth
        self.read_only = read_only
        super().__init__(address, Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="multica-agent-runtime management web UI")
    parser.add_argument("--bind", default=_env("WEBUI_BIND", "0.0.0.0"), help="listen address")
    parser.add_argument("--port", type=int, default=_env_int("WEBUI_PORT", 8090), help="listen port")
    parser.add_argument("--root", default="", help="workspaces root (else WEBUI_WORKSPACES_ROOT)")
    parser.add_argument("--version", action="version", version=f"multica-webui {VERSION}")
    args = parser.parse_args(argv)

    if args.root:
        os.environ["WEBUI_WORKSPACES_ROOT"] = os.path.abspath(args.root)

    auth = credentials()
    if auth is None and not is_loopback(args.bind) and _env("WEBUI_INSECURE") != "1":
        sys.stderr.write(
            "[webui] refusing to listen on %s without authentication.\n"
            "        set WEBUI_AUTH=user:password (or WEBUI_AUTH_FILE), bind WEBUI_BIND=127.0.0.1,\n"
            "        or set WEBUI_INSECURE=1 when an authenticating reverse proxy is in front.\n" % args.bind
        )
        return 2

    read_only = _env("WEBUI_READ_ONLY").strip().lower() in ("1", "true", "yes", "on")
    if read_only:
        sys.stderr.write("[webui] read-only mode: deletion is disabled\n")

    root = store.workspaces_root()
    if not os.path.isdir(root):
        sys.stderr.write(f"[webui] warning: workspaces root {root} does not exist yet\n")

    server = Server((args.bind, args.port), auth, read_only)

    def stop(signum, _frame):  # noqa: ANN001 - signal handler signature
        sys.stderr.write(f"[webui] signal {signum}, stopping\n")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    sys.stderr.write(
        f"[webui] multica-agent-runtime web UI {VERSION} on http://{args.bind}:{server.server_address[1]}/ "
        f"(root {root}, auth {'on' if auth else 'off'}"
        f"{', read-only' if read_only else ''})\n"
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
