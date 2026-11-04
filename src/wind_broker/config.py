"""Persistent configuration stored in ``~/.wind-broker/config.json``."""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_WIND_MODULE = "/Applications/Wind API.app/Contents/python/WindPy.py"

# Methods that are pure data reads. Subscription-style calls (wsq with a
# callback, wsi push) cannot cross a process boundary and are rejected.
DEFAULT_METHODS = (
    "wsd", "wss", "wsq", "wsi", "wset", "tdays",
    "tdaysoffset", "tdayscount", "edb", "wst", "wses", "wsee",
)


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", ""})


def broker_home() -> Path:
    """Directory holding config, socket, pid file, and logs."""

    return Path(os.environ.get("WIND_BROKER_HOME", "~/.wind-broker")).expanduser()


def config_path() -> Path:
    return broker_home() / "config.json"


def token_path() -> Path:
    return broker_home() / "token"


def ensure_home() -> Path:
    """Create the state directory, private to this user when we own it."""

    home = broker_home()
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass  # e.g. a shared directory we do not own; the token file is still 0600
    return home


def read_token() -> str | None:
    try:
        token = token_path().read_text().strip()
    except OSError:
        return None
    return token or None


def ensure_token() -> str:
    """Return the per-user API token, creating it (mode 0600) on first use.

    Loopback TCP is reachable by every account on the Mac; this secret, readable
    only by the owner, is what limits the broker to the user who runs it.
    """

    ensure_home()
    existing = read_token()
    if existing:
        os.chmod(token_path(), 0o600)
        return existing
    token = secrets.token_urlsafe(32)
    fd, temporary = tempfile.mkstemp(dir=broker_home(), prefix=".token.")
    with os.fdopen(fd, "w") as handle:  # mkstemp files are created 0600
        handle.write(token)
    os.replace(temporary, token_path())
    return token


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _default_socket() -> str:
    return str(broker_home() / "wind.sock")


@dataclass
class BrokerConfig:
    host: str = "127.0.0.1"
    port: int = 18881
    unix_socket_path: str = field(default_factory=_default_socket)
    wind_module_path: str = DEFAULT_WIND_MODULE
    python_executable: str = field(default_factory=lambda: sys.executable)
    startup_seconds: int = 300
    request_seconds: float = 90.0
    auto_connect_on_start: bool = False
    history_cache_dir: str | None = None
    # LAN router (hub mode): an opt-in second listener for other workstations.
    # Applied live by the daemon, so these are not restart keys.
    router_enabled: bool = False
    router_host: str = "0.0.0.0"
    router_port: int = 18882
    router_max_inflight: int = 2

    # Keys that only take effect after the daemon restarts (listeners/worker).
    RESTART_KEYS = frozenset({"host", "port", "unix_socket_path", "wind_module_path", "python_executable"})

    def validate(self) -> None:
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("host must be a non-empty string")
        if not _is_int(self.port) or not 0 <= self.port <= 65535:
            raise ValueError("port must be an integer in 0..65535")
        if not isinstance(self.startup_seconds, (int, float)) or self.startup_seconds <= 0:
            raise ValueError("startup_seconds must be positive")
        if not isinstance(self.request_seconds, (int, float)) or self.request_seconds <= 0:
            raise ValueError("request_seconds must be positive")
        if not isinstance(self.auto_connect_on_start, bool):
            raise ValueError("auto_connect_on_start must be a boolean")
        for name in ("unix_socket_path", "wind_module_path", "python_executable"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        if self.history_cache_dir is not None and not isinstance(self.history_cache_dir, str):
            raise ValueError("history_cache_dir must be a string or null")
        if not isinstance(self.router_enabled, bool):
            raise ValueError("router_enabled must be a boolean")
        if not isinstance(self.router_host, str) or not self.router_host:
            raise ValueError("router_host must be a non-empty string")
        if not _is_int(self.router_port) or not 0 <= self.router_port <= 65535:
            raise ValueError("router_port must be an integer in 0..65535")
        if self.router_port and self.router_port == self.port:
            raise ValueError("router_port must differ from port")
        if not _is_int(self.router_max_inflight) or self.router_max_inflight < 1:
            raise ValueError("router_max_inflight must be an integer >= 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def socket_path(self) -> Path | None:
        return Path(self.unix_socket_path).expanduser() if self.unix_socket_path else None

    @property
    def cache_dir(self) -> Path | None:
        return Path(self.history_cache_dir).expanduser() if self.history_cache_dir else None

    def updated(self, changes: dict[str, Any]) -> "BrokerConfig":
        known = {f.name for f in fields(self)}
        unknown = set(changes) - known
        if unknown:
            raise ValueError(f"unknown config keys: {', '.join(sorted(unknown))}")
        merged = {**self.to_dict(), **changes}
        if isinstance(merged.get("startup_seconds"), float) and merged["startup_seconds"].is_integer():
            merged["startup_seconds"] = int(merged["startup_seconds"])
        if merged.get("history_cache_dir") == "":
            merged["history_cache_dir"] = None
        candidate = BrokerConfig(**merged)
        candidate.validate()
        return candidate


def load_config(path: Path | None = None) -> BrokerConfig:
    """Read config, filling defaults; unknown keys are ignored for forward compatibility."""

    path = path or config_path()
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text() or "{}")
    known = {f.name for f in fields(BrokerConfig)}
    config = BrokerConfig(**{k: v for k, v in data.items() if k in known})
    config.validate()
    return config


def save_config(config: BrokerConfig, path: Path | None = None) -> None:
    """Write atomically so a crash never leaves a half-written config."""

    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config.", suffix=".json")
    with os.fdopen(fd, "w") as handle:
        json.dump(config.to_dict(), handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
