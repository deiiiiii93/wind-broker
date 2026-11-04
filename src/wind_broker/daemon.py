"""``windd``: the machine-wide WindPy broker daemon (HTTP over TCP + Unix socket)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .broker import Broker, BrokerError
from .codec import encode_frame, tag_tree, untag_tree
from .config import broker_home, config_path, load_config

log = logging.getLogger("windd")
MAX_BODY = 8 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", ""}


class Handler(BaseHTTPRequestHandler):
    server_version = f"windd/{__version__}"
    protocol_version = "HTTP/1.1"
    broker: Broker
    daemon: "Daemon"

    # Unix-socket peers have no (host, port) tuple.
    def address_string(self) -> str:  # noqa: D401
        return self.client_address[0] if isinstance(self.client_address, tuple) else "unix"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        log.debug("%s %s", self.address_string(), format % args)

    # ------------------------------------------------------------- plumbing
    def _send(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, exc: BrokerError) -> None:
        self._send(exc.http_status, {"ok": False, "error": exc.to_dict()})

    def _guard(self) -> bool:
        """Reject browser-originated requests (CSRF / DNS rebinding).

        A web page can make a browser POST to 127.0.0.1, but it cannot send
        ``Content-Type: application/json`` cross-origin without a CORS preflight,
        which this server never approves, and a rebinding attack shows up as a
        foreign Host header.
        """

        if isinstance(self.client_address, tuple):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if "]" not in (self.headers.get("Host") or "") \
                else (self.headers.get("Host") or "").split("]")[0] + "]"
            allowed = LOOPBACK_HOSTS | {self.daemon.config.host}
            if host not in allowed:
                self._send(403, {"ok": False, "error": {"kind": "forbidden", "code": None,
                                                        "message": f"Host {host!r} not allowed"}})
                return False
        if self.command == "POST":
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._send(415, {"ok": False, "error": {"kind": "bad_request", "code": None,
                                                        "message": "POST requires Content-Type: application/json"}})
                return False
        return True

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

    def _client_name(self, body: dict[str, Any]) -> str:
        return str(body.get("client") or self.headers.get("X-Wind-Client") or self.headers.get("User-Agent")
                   or "unknown")[:80]

    # --------------------------------------------------------------- routes
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if not self._guard():
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path in {"/v1/status", "/v1/health", "/health"}:
                self._send(200, self.broker.status())
            elif url.path == "/v1/logs":
                limit = int(query.get("limit", ["100"])[0])
                self._send(200, {"ok": True, "logs": self.broker.recent_logs(limit)})
            elif url.path == "/v1/config":
                self._send(200, {"ok": True, "config": self.daemon.saved_config(),
                                 "active": self.broker.config.to_dict(), "path": str(self.daemon.config_file)})
            else:
                self._send(404, {"ok": False, "error": {"kind": "not_found", "code": None, "message": url.path}})
        except ValueError as exc:
            self._error(BrokerError(str(exc), kind="bad_request"))

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = urlsplit(self.path).path
        try:
            body = self._body()
            if path == "/v1/call":
                self._call(body)
            elif path == "/v1/login":
                wait = bool(body.get("wait", True))
                status = self.broker.login(wait=wait, wait_seconds=body.get("wait_seconds"))
                self._send(200 if status["connected"] or not wait else 503, status)
            elif path == "/v1/logout":
                self._send(200, self.broker.logout())
            elif path == "/v1/test":
                result = self.broker.run_test(body.get("preset"), body.get("method"), body.get("args"),
                                              body.get("kwargs"), client=self._client_name({**body,
                                              "client": body.get("client") or "windbroker-test"}))
                self._send(200, result)
            elif path == "/v1/config":
                self._send(200, self.broker.update_config(body))
            elif path in {"/v1/logs/clear", "/v1/logs/reset"}:
                self.broker.clear_logs()
                self._send(200, {"ok": True})
            elif path == "/v1/shutdown":
                self._send(200, {"ok": True, "message": "shutting down"})
                threading.Thread(target=self.daemon.stop, name="windd-shutdown", daemon=True).start()
            else:
                self._send(404, {"ok": False, "error": {"kind": "not_found", "code": None, "message": path}})
        except BrokerError as exc:
            self._error(exc)
        except Exception as exc:  # noqa: BLE001 - the daemon must never die on a request
            log.exception("unhandled error on %s", path)
            self._error(BrokerError(f"{type(exc).__name__}: {exc}", kind="internal"))

    def _call(self, body: dict[str, Any]) -> None:
        method = str(body.get("method") or "")
        usedf = bool(body.get("usedf", True))
        result, source = self.broker.call(
            method, untag_tree(body.get("args") or []), untag_tree(body.get("kwargs") or {}),
            client=self._client_name(body), request_seconds=body.get("request_seconds"), usedf=usedf,
        )
        response: dict[str, Any] = {"ok": True, "method": method, "source": source}
        if usedf:
            response.update(encode_frame(result, str(body.get("frame_format") or "table")))
        else:
            response["windata"] = tag_tree(result)
        self._send(200, response)


# socketserver's default listen backlog is 5: a burst of concurrent clients
# would get ECONNRESET. A machine-wide broker needs a deeper accept queue.
LISTEN_BACKLOG = 128


class _TCPHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG


class _UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG
    allow_reuse_address = False


class Daemon:
    def __init__(self, config_file: Path | None = None):
        self.config_file = config_file or config_path()
        self.config = load_config(self.config_file)
        self.broker = Broker(self.config, config_file=self.config_file)
        self._servers: list[socketserver.BaseServer] = []
        self._stopped = threading.Event()
        self.pid_file = broker_home() / "windd.pid"

    def saved_config(self) -> dict[str, Any]:
        try:
            return load_config(self.config_file).to_dict()
        except (OSError, ValueError):
            return self.config.to_dict()

    def _handler(self) -> type[Handler]:
        return type("BoundHandler", (Handler,), {"broker": self.broker, "daemon": self})

    def bind(self) -> None:
        if self.config.host not in LOOPBACK_HOSTS:
            log.warning("binding to non-loopback host %s exposes licensed Wind data to the network", self.config.host)
        tcp = _TCPHTTPServer((self.config.host, self.config.port), self._handler())
        self._servers.append(tcp)
        self.config.port = tcp.server_address[1]  # resolves port 0 in tests
        sock = self.config.socket_path
        if sock is not None:
            sock.parent.mkdir(parents=True, exist_ok=True)
            if sock.exists():
                # Only remove a stale socket; a live one means another windd.
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    probe.connect(str(sock))
                    raise OSError(f"another windd is listening on {sock}")
                except (ConnectionRefusedError, FileNotFoundError):
                    sock.unlink(missing_ok=True)
                finally:
                    probe.close()
            unix = _UnixHTTPServer(str(sock), self._handler())
            os.chmod(sock, 0o600)
            self._servers.append(unix)

    def serve(self) -> None:
        self.bind()
        home = broker_home()
        home.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(os.getpid()))
        for server in self._servers:
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25},
                             name=f"windd-{type(server).__name__}", daemon=True).start()
        log.info("windd %s pid %s listening on http://%s:%s and %s", __version__, os.getpid(),
                 self.config.host, self.config.port, self.config.socket_path)
        self.broker.start_background()
        self._stopped.wait()

    def stop(self, *_: Any) -> None:
        if self._stopped.is_set():
            return
        log.info("windd shutting down")
        try:
            self.broker.shutdown()
        finally:
            for server in self._servers:
                server.shutdown()
                server.server_close()
            sock = self.config.socket_path
            if sock is not None:
                sock.unlink(missing_ok=True)
            try:
                if self.pid_file.read_text().strip() == str(os.getpid()):
                    self.pid_file.unlink()
            except OSError:
                pass
            self._stopped.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="windd", description="Persistent WindPy broker daemon")
    parser.add_argument("--config", type=Path, help="config file (default ~/.wind-broker/config.json)")
    parser.add_argument("--verbose", "-v", action="store_true")
    options = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if options.verbose else logging.INFO,
                        format="%(asctime)s %(name)s[%(process)d] %(levelname)s %(message)s")
    try:
        daemon = Daemon(options.config)
    except (OSError, ValueError) as exc:
        log.error("invalid configuration: %s", exc)
        return 2
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: threading.Thread(target=daemon.stop, daemon=True).start())
    try:
        daemon.serve()
    except OSError as exc:
        if _another_daemon_healthy(daemon.config):
            # Exit 0 so launchd (KeepAlive.SuccessfulExit=false) does not loop.
            log.info("another windd is already serving %s:%s; exiting", daemon.config.host, daemon.config.port)
            return 0
        log.error("cannot start windd: %s", exc)
        return 1
    return 0


def _another_daemon_healthy(config) -> bool:
    from .transport import Transport

    return Transport(config=config, client_name="windd-probe").healthy()


if __name__ == "__main__":
    sys.exit(main())
