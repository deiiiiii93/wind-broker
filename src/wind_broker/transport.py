"""HTTP transport to windd over the Unix socket (preferred) or loopback TCP,
plus detached auto-spawn of the daemon.

Remote mode (a workstation key from ``api_key=`` or ``WIND_BROKER_KEY``) talks
to another Mac's LAN router at ``WIND_BROKER_URL`` instead; it never touches
the local socket, token or daemon, and imports nothing macOS-only.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .config import LOOPBACK_HOSTS, BrokerConfig, broker_home, load_config, read_token
from .errors import DaemonUnavailable, ProviderError


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class Transport:
    """Minimal JSON-over-HTTP client.

    Env overrides: WIND_BROKER_URL, WIND_BROKER_SOCKET, and WIND_BROKER_KEY
    (a workstation key: switches to remote mode against a hub's LAN router).
    """

    def __init__(self, *, base_url: str | None = None, socket_path: str | None = None,
                 client_name: str = "python", config: BrokerConfig | None = None, api_key: str | None = None):
        self.config = config or _safe_config()
        base_url = base_url or os.environ.get("WIND_BROKER_URL")
        self.api_key = api_key or os.environ.get("WIND_BROKER_KEY") or None
        if self.api_key:
            if not base_url:
                raise ValueError("remote mode needs a hub URL: pass base_url= or set WIND_BROKER_URL "
                                 "(e.g. http://hub-mac.local:18882)")
            self.base_url = base_url.rstrip("/")
            self.socket_path = None  # a workstation key is only for the hub's router
        elif base_url:
            # An explicit URL means TCP only, unless a socket is also given.
            self.base_url = base_url.rstrip("/")
            self.socket_path = socket_path or os.environ.get("WIND_BROKER_SOCKET")
        else:
            self.base_url = f"http://{self.config.host}:{self.config.port}"
            socket_path = socket_path or os.environ.get("WIND_BROKER_SOCKET")
            self.socket_path = socket_path or (str(self.config.socket_path) if self.config.socket_path else None)
        self.client_name = client_name

    @property
    def remote(self) -> bool:
        return bool(self.api_key)

    def _auth_token(self, *, unix: bool) -> str | None:
        """Credential for one connection: the workstation key, or the local token
        only for the Unix socket and loopback TCP (never over the network)."""

        if self.api_key:
            return self.api_key
        if unix or (urlsplit(self.base_url).hostname or "") in LOOPBACK_HOSTS:
            return read_token()  # re-read: a freshly spawned daemon may have just created it
        return None

    def _connections(self, timeout: float):
        if self.socket_path and os.path.exists(self.socket_path):
            yield UnixHTTPConnection(self.socket_path, timeout)
        parts = urlsplit(self.base_url)
        yield http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=timeout)

    def request(self, verb: str, path: str, body: dict[str, Any] | None = None,
                timeout: float = 15.0) -> tuple[int, dict[str, Any]]:
        payload = json.dumps(body or {}, ensure_ascii=False).encode() if verb == "POST" else None
        headers = {"X-Wind-Client": self.client_name, "User-Agent": f"wind-broker-client/{__version__}",
                   "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        last_error: Exception | None = None
        for connection in self._connections(timeout):
            token = self._auth_token(unix=isinstance(connection, UnixHTTPConnection))
            if token:
                headers["Authorization"] = f"Bearer {token}"
            else:
                headers.pop("Authorization", None)
            try:
                try:
                    connection.connect()
                except (ConnectionRefusedError, FileNotFoundError) as exc:
                    last_error = exc  # nothing listening: next transport / unavailable
                    continue
                except OSError as exc:
                    # Listening but overloaded (reset/timeout): not "down", so never autospawn.
                    raise ProviderError(f"windd refused the connection: {exc}", kind="busy") from exc
                try:
                    connection.request(verb, path, body=payload, headers=headers)
                    response = connection.getresponse()
                    raw = response.read()
                except (TimeoutError, socket.timeout) as exc:
                    raise ProviderError(f"windd did not answer {path} within {timeout:g}s") from exc
                except (ConnectionResetError, BrokenPipeError, http.client.RemoteDisconnected) as exc:
                    raise ProviderError(f"windd dropped the connection during {path}: {exc}") from exc
                try:
                    data = json.loads(raw or b"{}")
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"windd returned non-JSON ({response.status}) for {path}") from exc
                return response.status, data
            finally:
                connection.close()
        raise DaemonUnavailable(f"windd is not running at {self.base_url} ({last_error})")

    def healthy(self, timeout: float = 2.0) -> bool:
        try:
            status, body = self.request("GET", "/v1/status", timeout=timeout)
        except ProviderError:
            return False
        return status == 200 and bool(body.get("ok"))


def _safe_config() -> BrokerConfig:
    try:
        return load_config()
    except (OSError, ValueError):
        return BrokerConfig()


def daemon_command(config: BrokerConfig | None = None) -> tuple[list[str], dict[str, str]]:
    """Interpreter + env that can import this very copy of wind_broker."""

    config = config or _safe_config()
    package_parent = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    if package_parent.name in {"site-packages", "dist-packages"}:
        # Installed into an environment: that environment's interpreter owns it.
        python = sys.executable
    else:
        # Source/app-bundle layout: the parent directory only holds our
        # packages, so it is safe to put on any interpreter's path.
        configured = config.python_executable
        python = configured if configured and os.access(configured, os.X_OK) else sys.executable
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(package_parent), env.get("PYTHONPATH", "")) if p)
        # The package may live inside a signed .app bundle: never write
        # __pycache__ next to it (that invalidates the code signature).
        env.setdefault("PYTHONPYCACHEPREFIX", str(broker_home() / "pycache"))
    env.pop("WIND_BROKER_URL", None)
    env.pop("WIND_BROKER_KEY", None)
    return [python, "-m", "wind_broker.daemon"], env


def spawn_daemon(*, transport: Transport | None = None, wait_seconds: float = 30.0) -> bool:
    """Start windd detached (own session, survives the caller). Returns True when healthy.

    A file lock prevents a stampede when several processes discover the daemon
    is down at the same moment.
    """

    import fcntl  # POSIX-only; imported here so remote clients work on Windows

    transport = transport or Transport()
    home = broker_home()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "spawn.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if transport.healthy():
            return True
        command, env = daemon_command(transport.config)
        with open(home / "windd.log", "ab") as log_file:
            process = subprocess.Popen(  # noqa: S603 - fixed argv
                command, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                env=env, cwd=str(home), start_new_session=True, close_fds=True,
            )
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if transport.healthy(timeout=1.0):
                return True
            if process.poll() is not None:
                # Lost a bind race to another windd? Then it is healthy now.
                return transport.healthy()
            time.sleep(0.15)
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        return False
