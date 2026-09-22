#!/usr/bin/env python3
"""Talk to your agent from a browser tab.

    python deployment/browser/server.py

The API key stays in this process; the page only gets 60-second tokens.
The same request handler is the Vercel WSGI/API entry in index.py.
"""

import copy
import json
import os
import re
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from hotel import booking_filename, booking_voucher_html, get_booking, run_tool, snapshot  # noqa: E402
from lib import (ApiError, aai, load_env, publish_agent, read_agent,  # noqa: E402
                 stored_agent_id)


def resolve_agent() -> dict:
    """A published id means the agent is managed elsewhere, so use it as it is."""
    name = os.environ.get("AGENT", "night-desk")
    known = stored_agent_id(name)
    if known:
        try:
            agent = aai(f"/agents/{known}")
        except ApiError as err:
            raise RuntimeError(f"Could not load agent {known}: {err}") from err
        return {"id": known, "name": agent.get("name") or "Your agent"}
    agent = read_agent(name)
    try:
        result = publish_agent(agent, name=name, reuse_by_name=True)
    except ApiError as err:
        raise RuntimeError(f"Could not publish agents/{name}.jsonc: {err}") from err
    verb = "Created" if result["created"] else "Updated"
    print(f'{verb} "{agent["name"]}" from agents/{name}.jsonc')
    return {"id": result["id"], "name": agent["name"]}


def public_agent(agent: dict) -> dict:
    """Read-only view of the stored agent. The API keeps header values and llm
    keys write-only; these deletes hold even if that changes. The system prompt
    is in here, so a public deployment shows it to anyone who opens the page."""
    copied = copy.deepcopy(agent)
    for tool in copied.get("tools", []):
        http = tool.get("http") or {}
        for header in http.get("headers", []):
            header["value"] = "<hidden>"
    for llm in copied.get("llm", []):
        llm.pop("api_key", None)
    return copied


AGENT = None
PAGE = ""


def ensure_ready() -> Optional[bytes]:
    """Load env and the published agent. Returns an error body if setup failed."""
    global AGENT, PAGE
    load_env()
    if not os.environ.get("ASSEMBLYAI_API_KEY"):
        return b'{"error":"missing ASSEMBLYAI_API_KEY"}'
    if AGENT and PAGE:
        return None
    try:
        AGENT = resolve_agent()
    except RuntimeError as err:
        print(err)
        return json.dumps({"error": str(err)}).encode()
    print(f"Agent: {AGENT['id']}")
    PAGE = ((HERE / "index.html").read_text()
            .replace("{{AGENT_NAME}}", AGENT["name"])
            .replace("{{AGENT_JSON}}", json.dumps(AGENT).replace("<", "\\u003c")))
    return None


def original_path(raw_path: str, headers: Optional[dict] = None) -> str:
    """Vercel rewrites land on /api; recover the public URL from ?__p= or headers."""
    parsed = urlparse(raw_path or "/")
    qs = parse_qs(parsed.query)
    if qs.get("__p"):
        return qs["__p"][0] or "/"
    headers = {str(k).lower(): v for k, v in (headers or {}).items()}
    for key in ("x-invoke-path", "x-forwarded-uri", "x-vercel-original-path"):
        value = headers.get(key)
        if value and value not in {"/api", "/api/"}:
            return urlparse(value).path or "/"
    if parsed.path in {"/api", "/api/"}:
        return "/"
    return parsed.path or "/"


def dispatch(method: str, raw_path: str, body: bytes = b"",
             headers: Optional[dict] = None) -> tuple[int, bytes, str, Optional[dict]]:
    """Shared by the local HTTP server and the Vercel WSGI/API adapters."""
    setup_error = ensure_ready()
    if setup_error:
        return 500, setup_error, "application/json", None

    path = original_path(raw_path, headers)
    query = urlparse(raw_path or "/").query
    method = method.upper()

    if method == "GET" and path == "/token":
        try:
            token = aai("/token?product=voice_agent&expires_in_seconds=60")
            return 200, json.dumps(token).encode(), "application/json", None
        except ApiError as err:
            print(err)
            return 502, b'{"error":"token request failed"}', "application/json", None

    if method == "GET" and path == "/agent":
        try:
            agent = aai(f"/agents/{AGENT['id']}")
            return 200, json.dumps(public_agent(agent)).encode(), "application/json", None
        except ApiError as err:
            print(err)
            return 502, b'{"error":"could not load the agent"}', "application/json", None

    if method == "GET" and path == "/app.js":
        return 200, (HERE / "app.js").read_bytes(), "text/javascript", None

    if method == "GET" and path == "/api/hotel":
        return 200, json.dumps(snapshot()).encode(), "application/json", None

    booking_match = re.fullmatch(r"/booking/(ND-\d{4})(/download)?", path)
    if method == "GET" and booking_match:
        booking = get_booking(booking_match.group(1))
        if not booking:
            return 404, b'{"error":"booking not found"}', "application/json", None
        auto_print = parse_qs(query).get("print", [""])[0] == "1"
        page = booking_voucher_html(booking, auto_print=auto_print and not booking_match.group(2))
        extra = None
        if booking_match.group(2):
            extra = {
                "Content-Disposition": f'attachment; filename="{booking_filename(booking)}"',
            }
        return 200, page.encode(), "text/html; charset=utf-8", extra

    if method == "POST" and path.startswith("/api/tools/"):
        try:
            payload = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            return 400, b'{"error":"invalid json"}', "application/json", None
        name = path.rsplit("/", 1)[-1]
        result = run_tool(name, payload if isinstance(payload, dict) else {})
        return 200, json.dumps(result).encode(), "application/json", None

    if method == "POST":
        return 404, b'{"error":"not found"}', "application/json", None

    return 200, PAGE.encode(), "text/html", None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._send(*dispatch("GET", self.path, headers=dict(self.headers)))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        self._send(*dispatch("POST", self.path, raw, headers=dict(self.headers)))

    def log_message(self, *args) -> None:  # quiet; errors are printed above
        pass


def app(environ, start_response):
    """WSGI entry for Vercel. PATH_INFO is the public route."""
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO") or "/"
    query = environ.get("QUERY_STRING") or ""
    raw_path = f"{path}?{query}" if query else path
    size = int(environ.get("CONTENT_LENGTH") or 0)
    body = environ["wsgi.input"].read(size) if size else b""
    headers = {
        key[5:].replace("_", "-"): value
        for key, value in environ.items()
        if key.startswith("HTTP_")
    }
    status, payload, content_type, extra = dispatch(method, raw_path, body, headers)
    header_list = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(payload))),
    ]
    for key, value in (extra or {}).items():
        header_list.append((key, value))
    phrase = HTTPStatus(status).phrase
    start_response(f"{status} {phrase}", header_list)
    return [payload]


def main() -> None:
    error = ensure_ready()
    if error:
        sys.exit(error.decode())

    # PORT when set, otherwise 3000 and up until one is free.
    fixed = os.environ.get("PORT")
    port = int(fixed) if fixed else 3000
    while True:
        try:
            server = ThreadingHTTPServer(("", port), Handler)
            break
        except OSError:
            if fixed or port >= 3010:
                raise
            port += 1

    print(f"Talk to it: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
