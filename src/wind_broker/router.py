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
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any

from .config import broker_home, ensure_home

CLIENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
KEY_PREFIX = "wbk_"


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
