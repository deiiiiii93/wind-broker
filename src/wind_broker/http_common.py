"""JSON-over-HTTP plumbing shared by windd's local API and its LAN router."""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler
from typing import Any

from . import __version__
from .broker import Broker, BrokerError
from .codec import encode_frame, tag_tree, untag_tree

log = logging.getLogger("windd")
MAX_BODY = 8 * 1024 * 1024


class JSONHandler(BaseHTTPRequestHandler):
    server_version = f"windd/{__version__}"
    protocol_version = "HTTP/1.1"

    # Unix-socket peers have no (host, port) tuple.
    def address_string(self) -> str:  # noqa: D401
        return self.client_address[0] if isinstance(self.client_address, tuple) else "unix"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("%s %s", self.address_string(), format % args)

    def _send(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _fail(self, status: int, kind: str, message: str) -> None:
        # The request body may be unread: hang up rather than parse it as the next request.
        self.close_connection = True
        self._send(status, {"ok": False, "error": {"kind": kind, "code": None, "message": message}})

    def _error(self, exc: BrokerError) -> None:
        self._send(exc.http_status, {"ok": False, "error": exc.to_dict()})

    def _json_post_ok(self) -> bool:
        """POSTs must be JSON: a browser cannot send that cross-origin without a preflight."""

        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._fail(415, "bad_request", "POST requires Content-Type: application/json")
            return False
        return True

    def _supplied_token(self) -> str:
        header = self.headers.get("Authorization") or ""
        return header[7:].strip() if header[:7].lower() == "bearer " else (self.headers.get("X-Wind-Token") or "")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise BrokerError("request body too large", kind="bad_request")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrokerError(f"invalid JSON body: {exc}", kind="bad_request") from exc
        if not isinstance(body, dict):
            raise BrokerError("JSON body must be an object", kind="bad_request")
        return body


def run_call(broker: Broker, body: dict[str, Any], *, client: str, request_seconds: Any = None,
             allow_login: bool = True) -> dict[str, Any]:
    """Execute a ``/v1/call`` body and build its JSON response."""

    method = str(body.get("method") or "")
    usedf = bool(body.get("usedf", True))
    result, source = broker.call(
        method, untag_tree(body.get("args") or []), untag_tree(body.get("kwargs") or {}),
        client=client, request_seconds=request_seconds, usedf=usedf, allow_login=allow_login,
    )
    response: dict[str, Any] = {"ok": True, "method": method, "source": source}
    if usedf:
        response.update(encode_frame(result, str(body.get("frame_format") or "table")))
    else:
        response["windata"] = tag_tree(result)
    return response
