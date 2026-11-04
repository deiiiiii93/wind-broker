"""LAN router (hub mode): lets other workstations share this Mac's Wind session.

The router is a second, opt-in TCP listener inside windd. It accepts only data
calls and a trimmed status, authenticated by per-workstation keys that the hub
owner issues and revokes through the local API. Keys are shown once and stored
only as SHA-256 digests in ``~/.wind-broker/router_clients.json`` (mode 0600).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .broker import Broker, BrokerError
from .config import BrokerConfig, broker_home, ensure_home
from .http_common import JSONHandler, run_call

log = logging.getLogger("windd")

CLIENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
KEY_PREFIX = "wbk_"
# Pre-auth limits: any LAN host can connect before proving it holds a key.
ROUTER_SOCKET_TIMEOUT = 30.0
ROUTER_MAX_CONNECTIONS = 64
STATUS_PATHS = frozenset({"/v1/status", "/v1/health", "/health"})
PUBLIC_STATUS_KEYS = ("version", "state", "connected", "connected_since", "last_active_at")


def registry_path() -> Path:
    return broker_home() / "router_clients.json"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class ClientRegistry:
    """Workstation keys (persisted as digests) plus runtime per-client stats.

    An unreadable file counts as zero clients and sets ``error``; it is left
    untouched until the owner adds or revokes a client, so a typo in a manual
    edit never silently destroys the registry.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or registry_path()
        self._lock = threading.Lock()
        self._entries: list[dict[str, str]] = []
        self._stats: dict[str, dict[str, Any]] = {}
        self.error: str | None = None
        self._load()

    # ------------------------------------------------------------ storage
    def _load(self) -> None:
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return
        except OSError as exc:
            self.error = f"cannot read {self.path}: {exc}"
            return
        try:
            data = json.loads(raw or "{}")
            entries = data.get("clients", []) if isinstance(data, dict) else None
            if not isinstance(entries, list) or not all(
                    isinstance(e, dict) and isinstance(e.get("name"), str) and isinstance(e.get("key_sha256"), str)
                    for e in entries):
                raise ValueError("expected {\"clients\": [{\"name\", \"key_sha256\", ...}]}")
        except ValueError as exc:
            self.error = f"ignoring unreadable {self.path}: {exc}"
            return
        self._entries = [{"name": e["name"], "key_sha256": e["key_sha256"],
                          "created_at": str(e.get("created_at") or "")} for e in entries]

    def _save(self) -> None:
        ensure_home()
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".router_clients.")
        with os.fdopen(fd, "w") as handle:  # mkstemp files are created 0600
            json.dump({"clients": self._entries}, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)
        self.error = None

    # ---------------------------------------------------------- management
    def add(self, name: str) -> str:
        """Issue a key for a new workstation. The plaintext key is returned once."""

        if not isinstance(name, str) or not CLIENT_NAME.match(name):
            raise ValueError("client name must be 1-40 characters: letters, digits, '.', '_' or '-', "
                             "starting with a letter or digit")
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            if any(e["name"] == name for e in self._entries):
                raise ValueError(f"client {name!r} already exists; revoke it first to issue a new key")
            self._entries.append({"name": name, "key_sha256": hash_key(key), "created_at": _now()})
            self._save()
        return key

    def revoke(self, name: str) -> None:
        with self._lock:
            remaining = [e for e in self._entries if e["name"] != name]
            if len(remaining) == len(self._entries):
                raise KeyError(name)
            self._entries = remaining
            self._stats.pop(name, None)
            self._save()

    def verify(self, key: str | None) -> str | None:
        """Return the workstation name for a valid key, else None."""

        if not key:
            return None
        digest = hash_key(key).encode()
        match = None
        with self._lock:
            for entry in self._entries:  # no early exit: timing does not reveal the position
                if hmac.compare_digest(digest, entry["key_sha256"].encode()):
                    match = entry["name"]
        return match

    # ------------------------------------------------------------- runtime
    def _stat(self, name: str) -> dict[str, Any]:
        return self._stats.setdefault(name, self._stat_template())

    def acquire(self, name: str, cap: int) -> bool:
        """Reserve one in-flight slot for ``name``; False when it is at ``cap``."""

        with self._lock:
            stat = self._stat(name)
            if stat["inflight"] >= cap:
                return False
            stat["inflight"] += 1
            return True

    def release(self, name: str) -> None:
        with self._lock:
            stat = self._stats.get(name)
            if stat is not None and stat["inflight"] > 0:
                stat["inflight"] -= 1

    def touch(self, name: str, addr: str | None) -> None:
        with self._lock:
            stat = self._stat(name)
            stat["requests"] += 1
            stat["last_seen_at"] = _now()
            stat["last_addr"] = addr

    def clients(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"name": e["name"], "created_at": e["created_at"],
                     **self._stats.get(e["name"], self._stat_template())}
                    for e in self._entries]

    @staticmethod
    def _stat_template() -> dict[str, Any]:
        return {"inflight": 0, "requests": 0, "last_seen_at": None, "last_addr": None}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ------------------------------------------------------------------ listener
class RouterHandler(JSONHandler):
    """LAN-facing handler: workstation key first, then data calls and status only."""

    broker: Broker
    router: "Router"

    def setup(self) -> None:
        # Read at connection time so the limit is the module's current value.
        self.timeout = ROUTER_SOCKET_TIMEOUT
        super().setup()

    def _authenticate(self) -> str | None:
        name = self.router.registry.verify(self._supplied_token())
        if name is None:
            log.warning("router: rejected request from %s (missing or invalid workstation key)",
                        self.address_string())
            self._fail(401, "unauthorized", "missing or invalid workstation key; send "
                                            "'Authorization: Bearer <key issued by the hub>'")
        return name

    def _forbidden(self, path: str) -> None:
        self._fail(403, "forbidden", f"{path} is not available over the LAN router (data calls and status only)")

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        name = self._authenticate()
        if name is None:
            return
        path = urlsplit(self.path).path
        if path not in STATUS_PATHS:
            self._forbidden(path)
            return
        self.router.registry.touch(name, self.address_string())
        full = self.broker.status()
        self._send(200, {"ok": True, **{k: full.get(k) for k in PUBLIC_STATUS_KEYS}, "client": name, "router": True})

    def do_POST(self) -> None:  # noqa: N802
        name = self._authenticate()
        if name is None:
            return
        path = urlsplit(self.path).path
        if path != "/v1/call":
            self._forbidden(path)
            return
        if not self._json_post_ok():
            return
        try:
            body = self._body()
            app = str(body.get("client") or self.headers.get("X-Wind-Client") or "remote")
            client = f"{name}/{app}"[:80]
            seconds = self._clamped_seconds(body.get("request_seconds"))
            cap = self.broker.config.router_max_inflight
            if not self.router.registry.acquire(name, cap):
                self._fail(429, "busy", f"workstation {name!r} already has {cap} request(s) in flight; "
                                        "wait for them to finish")
                return
            try:
                self.router.registry.touch(name, self.address_string())
                self._send(200, run_call(self.broker, body, client=client, request_seconds=seconds,
                                         allow_login=False))
            finally:
                self.router.registry.release(name)
        except BrokerError as exc:
            self._error(exc)
        except Exception as exc:  # noqa: BLE001 - the daemon must never die on a request
            log.exception("router: unhandled error on %s", path)
            self._error(BrokerError(f"{type(exc).__name__}: {exc}", kind="internal"))

    def _clamped_seconds(self, requested: Any) -> float:
        """A workstation may shorten, never extend, the hub's request deadline."""

        ceiling = float(self.broker.config.request_seconds)
        if requested is None:
            return ceiling
        try:
            seconds = float(requested)
        except (TypeError, ValueError) as exc:
            raise BrokerError("request_seconds must be a number", kind="bad_request") from exc
        return min(seconds, ceiling)


class _RouterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], handler: type[RouterHandler]):
        self._slots = threading.BoundedSemaphore(ROUTER_MAX_CONNECTIONS)
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # too many open LAN connections: drop this one
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class _RouterHTTPServerV6(_RouterHTTPServer):
    address_family = socket.AF_INET6


def lan_addresses() -> list[str]:
    """Best-effort IPv4 addresses a workstation could use to reach this Mac."""

    found: set[str] = set()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))  # UDP connect sends nothing; it picks the default route
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(str(info[4][0]))
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127.") and a != "0.0.0.0")


class Router:
    """Owns the LAN listener and applies ``router_*`` config changes live."""

    def __init__(self, config: BrokerConfig, broker: Broker, registry: ClientRegistry | None = None):
        self.config = config
        self.broker = broker
        self.registry = registry or ClientRegistry()
        self._lock = threading.Lock()
        self._server: _RouterHTTPServer | None = None
        self._applied: tuple[bool, str, int] | None = None
        self._bind_error: str | None = None
        if self.registry.error:
            log.warning("router: %s", self.registry.error)

    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        server = self._server
        return int(server.server_address[1]) if server is not None else int(self.config.router_port)

    @property
    def error(self) -> str | None:
        return self._bind_error or self.registry.error

    def apply(self) -> None:
        """Start, stop or rebind to match the config. Never raises on bind errors."""

        desired = (bool(self.config.router_enabled), str(self.config.router_host), int(self.config.router_port))
        with self._lock:
            if desired == self._applied:
                return
            self._stop_locked()
            self._bind_error = None
            enabled, host, port = desired
            if not enabled:
                self._applied = desired
                return
            handler = type("BoundRouterHandler", (RouterHandler,), {"broker": self.broker, "router": self})
            server_class = _RouterHTTPServerV6 if ":" in host else _RouterHTTPServer
            try:
                server = server_class((host.strip("[]"), port), handler)
            except OSError as exc:
                # Leave _applied unset so the next config save retries the bind.
                self._bind_error = f"cannot listen on {host}:{port}: {exc}"
                log.warning("router: %s", self._bind_error)
                return
            self._server = server
            self._applied = desired
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25},
                             name="windd-router", daemon=True).start()
            log.warning("router: LAN listener on http://%s:%s shares this Mac's Wind session with %d "
                        "workstation key(s); make sure your Wind agreement allows it",
                        host, self.port, len(self.registry))

    def _stop_locked(self) -> None:
        server, self._server = self._server, None
        self._applied = None
        if server is not None:
            server.shutdown()
            server.server_close()
            log.info("router: LAN listener stopped")

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def summary(self) -> dict[str, Any]:
        """Compact form for the local ``/v1/status``."""

        return {"enabled": bool(self.config.router_enabled), "listening": self.listening,
                "port": self.port, "clients": len(self.registry)}

    def status(self) -> dict[str, Any]:
        """Full form for the local ``GET /v1/router``."""

        return {"ok": True, "enabled": bool(self.config.router_enabled), "host": self.config.router_host,
                "port": self.port, "listening": self.listening, "error": self.error,
                "max_inflight": self.config.router_max_inflight, "addresses": lan_addresses(),
                "hostname": socket.gethostname(), "clients": self.registry.clients()}
