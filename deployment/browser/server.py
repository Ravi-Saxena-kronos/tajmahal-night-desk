#!/usr/bin/env python3
"""Talk to your agent from a browser tab.

    python deployment/browser/server.py

The API key stays in this process; the page only gets 60-second tokens.
The same request handler is the Vercel WSGI/API entry in index.py.
"""

import base64
import copy
import json
import os
import re
import socket
import ssl
import struct
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

# REST can still describe an id that the voice websocket rejects. This is the
# Night Desk that starts a session with a Vercel-minted token.
VOICE_FALLBACK_ID = "agent_3b41300cda834e87918ff9d0f2fd4ffb"

NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


def with_no_store(extra: Optional[dict] = None) -> dict:
    headers = dict(NO_STORE)
    if extra:
        headers.update(extra)
    return headers


def render_page(agent: dict) -> str:
    return ((HERE / "index.html").read_text()
            .replace("{{AGENT_NAME}}", agent["name"])
            .replace("{{AGENT_JSON}}", json.dumps(agent).replace("<", "\\u003c")))


def _agent_by_name(want: str) -> list[dict]:
    listing = aai("/agents")
    return [row for row in listing.get("agents") or [] if row.get("name") == want]


def voice_session_ok(agent_id: str) -> Optional[bool]:
    """True if session.update is accepted, False if the socket rejects the id.

    None means the probe could not run, so the caller should keep the REST id.
    """
    try:
        token_body = aai("/token?product=voice_agent&expires_in_seconds=60")
        token = token_body.get("token")
        if not token:
            return None
    except ApiError as err:
        print(f"session probe token failed: {err}")
        return None

    host = "agents.assemblyai.com"
    path = f"/v1/ws?token={token}"
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    ).encode()
    ctx = ssl.create_default_context()
    try:
        sock = socket.create_connection((host, 443), timeout=6)
        ssock = ctx.wrap_socket(sock, server_hostname=host)
    except OSError as err:
        print(f"session probe connect failed: {err}")
        return None
    try:
        ssock.sendall(req)
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = ssock.recv(1)
            if not chunk:
                return None
            header += chunk
        if b"101" not in header.split(b"\r\n", 1)[0]:
            return None

        def mask_send(payload: bytes) -> None:
            mask = os.urandom(4)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            n = len(payload)
            head = bytes([0x81])
            if n < 126:
                head += bytes([0x80 | n])
            else:
                head += bytes([0x80 | 126]) + struct.pack("!H", n)
            ssock.sendall(head + mask + masked)

        def recv_text() -> Optional[str]:
            hdr = b""
            while len(hdr) < 2:
                chunk = ssock.recv(2 - len(hdr))
                if not chunk:
                    return None
                hdr += chunk
            opcode = hdr[0] & 0x0F
            ln = hdr[1] & 0x7F
            if ln == 126:
                ext = ssock.recv(2)
                ln = struct.unpack("!H", ext)[0]
            elif ln == 127:
                ext = ssock.recv(8)
                ln = struct.unpack("!Q", ext)[0]
            data = b""
            while len(data) < ln:
                chunk = ssock.recv(ln - len(data))
                if not chunk:
                    break
                data += chunk
            if opcode == 8:
                return None
            if opcode == 1:
                return data.decode()
            return ""

        mask_send(json.dumps({
            "type": "session.update",
            "session": {"agent_id": agent_id},
        }).encode())
        ssock.settimeout(6)
        raw = recv_text()
        if raw is None:
            return None
        try:
            mask_send(json.dumps({"type": "session.end"}).encode())
        except OSError:
            pass
        if '"type":"session.error"' in raw or '"code":"agent_not_found"' in raw:
            return False
        if '"config"' in raw or "session.updated" in raw or "session.ready" in raw:
            return True
        return None
    except OSError as err:
        print(f"session probe {agent_id}: {err}")
        return None
    finally:
        try:
            ssock.close()
        except OSError:
            pass


def resolve_agent() -> dict:
    """Pick an id the voice websocket will accept.

    Vercel can keep a stale AGENT_ID whose REST GET still 200s while
    session.update returns agent_not_found. Prefer a live same-name agent,
    then env ids, then the Night Desk that already starts on this account.
    """
    name = os.environ.get("AGENT", "night-desk")
    file_agent = read_agent(name)
    want = file_agent.get("name") or "Tajmahal Night Desk"
    known = stored_agent_id(name)
    # Probe the id that already starts a session first so a stale Vercel
    # AGENT_ID does not burn the serverless time budget.
    candidates: list[str] = []
    for aid in (os.environ.get("AGENT_ID_NIGHT_DESK") or "", VOICE_FALLBACK_ID, known):
        if aid and aid not in candidates:
            candidates.append(aid)
    try:
        for row in _agent_by_name(want):
            aid = row.get("id")
            if aid and aid not in candidates:
                candidates.append(aid)
    except ApiError as err:
        print(f"Could not list agents: {err}")

    rest_names: dict[str, str] = {}
    for aid in list(candidates):
        try:
            agent = aai(f"/agents/{aid}")
            rest_names[aid] = agent.get("name") or want
        except ApiError as err:
            if err.status != 404:
                print(f"GET {aid}: {err}")

    chosen = None
    voice_confirmed = False
    for aid in candidates:
        ok = voice_session_ok(aid)
        if ok is True:
            chosen = aid
            voice_confirmed = True
            break
        if ok is False:
            print(f"Voice session rejected {aid}")
            continue
        if aid in rest_names and chosen is None:
            chosen = aid

    if not rest_names:
        try:
            result = publish_agent(file_agent, name=name, reuse_by_name=True)
        except ApiError as err:
            if not voice_confirmed:
                raise RuntimeError(f"Could not publish agents/{name}.jsonc: {err}") from err
            result = None
        if result:
            verb = "Created" if result["created"] else "Updated"
            print(f'{verb} "{file_agent["name"]}" from agents/{name}.jsonc')
            rest_names[result["id"]] = file_agent["name"]
            if not voice_confirmed:
                chosen = result["id"]

    if not voice_confirmed:
        chosen = chosen or rest_names and next(iter(rest_names)) or VOICE_FALLBACK_ID

    label = rest_names.get(chosen, want)
    print(f"Agent: {chosen}" + (" (voice ok)" if voice_confirmed else ""))
    return {"id": chosen, "name": label}


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
    print(f"Serving {AGENT['id']}")
    PAGE = render_page(AGENT)
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
        return 500, setup_error, "application/json", with_no_store()

    path = original_path(raw_path, headers)
    query = urlparse(raw_path or "/").query
    method = method.upper()

    if method == "GET" and path == "/token":
        try:
            token = aai("/token?product=voice_agent&expires_in_seconds=60")
            return 200, json.dumps(token).encode(), "application/json", with_no_store()
        except ApiError as err:
            print(err)
            return 502, b'{"error":"token request failed"}', "application/json", with_no_store()

    if method == "GET" and path == "/agent":
        payload = {"id": AGENT["id"], "name": AGENT["name"]}
        try:
            agent = aai(f"/agents/{AGENT['id']}")
            payload = public_agent(agent)
            payload["id"] = AGENT["id"]
        except ApiError as err:
            print(err)
        return 200, json.dumps(payload).encode(), "application/json", with_no_store()

    if method == "GET" and path == "/app.js":
        return 200, (HERE / "app.js").read_bytes(), "text/javascript", with_no_store()

    if method == "GET" and path == "/api/hotel":
        return 200, json.dumps(snapshot()).encode(), "application/json", with_no_store()

    booking_match = re.fullmatch(r"/booking/(ND-\d{4})(/download)?", path)
    if method == "GET" and booking_match:
        booking = get_booking(booking_match.group(1))
        if not booking:
            return 404, b'{"error":"booking not found"}', "application/json", with_no_store()
        auto_print = parse_qs(query).get("print", [""])[0] == "1"
        page = booking_voucher_html(booking, auto_print=auto_print and not booking_match.group(2))
        extra = None
        if booking_match.group(2):
            extra = {
                "Content-Disposition": f'attachment; filename="{booking_filename(booking)}"',
            }
        return 200, page.encode(), "text/html; charset=utf-8", with_no_store(extra)

    if method == "POST" and path.startswith("/api/tools/"):
        try:
            payload = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            return 400, b'{"error":"invalid json"}', "application/json", with_no_store()
        name = path.rsplit("/", 1)[-1]
        result = run_tool(name, payload if isinstance(payload, dict) else {})
        return 200, json.dumps(result).encode(), "application/json", with_no_store()

    if method == "POST":
        return 404, b'{"error":"not found"}', "application/json", with_no_store()

    return 200, PAGE.encode(), "text/html; charset=utf-8", with_no_store()


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
