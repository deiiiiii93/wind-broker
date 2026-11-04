"""``WindClient``: drop-in replacement for an in-process WindPy transport.

Signature-compatible with ``index-dashboard-codex``'s
``app.providers.wind_client.WindClient`` (``call(method, *args, **kwargs)``
returning a ``pandas.DataFrame``, ``close()``), but every call goes to the
shared ``windd`` session, so no consumer ever triggers another QR login.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, NoReturn

import pandas as pd

from .codec import decode_frame, plain_args, tag_tree, untag_tree
from .config import DEFAULT_METHODS, DEFAULT_WIND_MODULE
from .errors import DaemonUnavailable, ProviderError
from .transport import Transport, spawn_daemon


def default_client_name() -> str:
    name = os.environ.get("WIND_BROKER_CLIENT")
    if name:
        return name
    argv0 = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    if argv0 in {"", "-c", "ipykernel_launcher.py"} or argv0.startswith("ipykernel"):
        return "python-notebook" if "ipykernel" in sys.modules else "python"
    return argv0


class WindClient:
    """Thread-safe (stateless) client for the local Wind broker, or, with a
    workstation key (``api_key=`` / ``WIND_BROKER_KEY``) and a hub URL
    (``base_url=`` / ``WIND_BROKER_URL``), for another Mac's LAN router.

    ``error_class`` lets a host application receive its own exception type,
    e.g. ``WindClient(error_class=app.providers.base.ProviderError)``.
    """

    error_class: type[Exception] = ProviderError

    def __init__(self, *, module_path: str = DEFAULT_WIND_MODULE, startup_seconds: int = 300,
                 request_seconds: float = 90, history_cache_dir: str | None = None,
                 base_url: str | None = None, socket_path: str | None = None, client_name: str | None = None,
                 autospawn: bool = True, error_class: type[Exception] | None = None,
                 api_key: str | None = None):
        if startup_seconds <= 0 or request_seconds <= 0:
            raise ValueError("Wind timeouts must be positive")
        # Kept for compatibility/introspection; the daemon owns SDK loading and
        # its history cache (configure via ~/.wind-broker/config.json).
        self.module_path = str(module_path)
        self.startup_seconds = startup_seconds
        self.request_seconds = request_seconds
        self.history_cache_dir = Path(history_cache_dir) if history_cache_dir else None
        self.client_name = client_name or default_client_name()
        if error_class is not None:
            self.error_class = error_class
        self.transport = Transport(base_url=base_url, socket_path=socket_path, client_name=self.client_name,
                                   api_key=api_key)
        # A workstation (remote mode) uses the hub's session; never start a local windd.
        self.autospawn = autospawn and not self.transport.remote

    @property
    def remote(self) -> bool:
        """True when talking to another Mac's LAN router with a workstation key."""

        return self.transport.remote

    # -------------------------------------------------------------- helpers
    def _raise(self, message: str, *, kind: str = "error", code: int | None = None) -> NoReturn:
        error_class: Any = self.error_class
        if issubclass(error_class, ProviderError):
            raise error_class(message, kind=kind, code=code)
        error = self.error_class(message)
        for attribute, value in (("kind", kind), ("code", code)):
            try:
                setattr(error, attribute, value)
            except AttributeError:
                pass
        raise error

    def _request(self, verb: str, path: str, body: dict[str, Any] | None = None,
                 timeout: float = 15.0, *, check: bool = True) -> dict[str, Any]:
        try:
            try:
                status, data = self.transport.request(verb, path, body, timeout=timeout)
            except DaemonUnavailable:
                if not self.autospawn:
                    raise
                if not spawn_daemon(transport=self.transport):
                    raise DaemonUnavailable("windd could not be started; see ~/.wind-broker/windd.log") from None
                status, data = self.transport.request(verb, path, body, timeout=timeout)
        except ProviderError as exc:
            if isinstance(exc, self.error_class):
                raise  # keep subclasses such as DaemonUnavailable
            self._raise(str(exc), kind=exc.kind, code=exc.code)
        if check and (status >= 400 or not data.get("ok", False)):
            error = data.get("error") or {}
            self._raise(error.get("message") or data.get("last_error") or f"windd HTTP {status}",
                        kind=error.get("kind", "error"), code=error.get("code"))
        return data

    def _call_timeout(self, request_seconds: float) -> float:
        # The daemon enforces the real deadlines; this is only a safety net
        # that covers a pending login plus one queued request ahead of us.
        return self.startup_seconds + 2 * request_seconds + 30

    # ------------------------------------------------------------------ API
    def call_raw(self, method: str, args: list | tuple = (), kwargs: dict | None = None, *,
                 usedf: bool = True, request_seconds: float | None = None,
                 frame_format: str = "exact") -> dict[str, Any]:
        seconds = float(request_seconds or self.request_seconds)
        body = {"method": method, "args": tag_tree(plain_args(list(args))),
                "kwargs": tag_tree(plain_args(dict(kwargs or {}))), "client": self.client_name,
                "request_seconds": seconds, "usedf": usedf, "frame_format": frame_format}
        return self._request("POST", "/v1/call", body, timeout=self._call_timeout(seconds))

    def call(self, method: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
        if method not in DEFAULT_METHODS:
            raise ValueError("Unsupported Wind request")
        request_seconds = kwargs.pop("request_seconds", None)
        body = self.call_raw(method, args, kwargs, request_seconds=request_seconds)
        return decode_frame(body)

    def windata(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """WindData-style payload (ErrorCode/Codes/Fields/Times/Data)."""

        body = self.call_raw(method, args, kwargs, usedf=False)
        return untag_tree(body["windata"])

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status")

    def is_connected(self) -> bool:
        return bool(self.status().get("connected"))

    def login(self, *, wait: bool = True, wait_seconds: float | None = None) -> dict[str, Any]:
        seconds = wait_seconds or self.startup_seconds
        return self._request("POST", "/v1/login", {"wait": wait, "wait_seconds": seconds},
                             timeout=seconds + self.request_seconds + 60, check=wait)

    def logout(self) -> dict[str, Any]:
        """Explicitly end the shared Wind session for *every* app on this Mac."""

        return self._request("POST", "/v1/logout", timeout=self.request_seconds + 60)

    def logs(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/logs?limit={int(limit)}")["logs"]

    def test(self, preset: str = "spot_live", **custom: Any) -> dict[str, Any]:
        return self._request("POST", "/v1/test", {"preset": preset, **custom},
                             timeout=self._call_timeout(self.request_seconds))

    def get_config(self) -> dict[str, Any]:
        return self._request("GET", "/v1/config")

    def set_config(self, **changes: Any) -> dict[str, Any]:
        return self._request("POST", "/v1/config", changes)

    # ------------------------------------------------------- LAN router (hub)
    def router_status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/router")

    def router_add_client(self, name: str) -> dict[str, Any]:
        """Issue a workstation key. The key is in the response once and never again."""

        return self._request("POST", "/v1/router/clients", {"name": name})

    def router_revoke(self, name: str) -> dict[str, Any]:
        return self._request("POST", "/v1/router/clients/revoke", {"name": name})

    def shutdown_daemon(self) -> dict[str, Any]:
        return self._request("POST", "/v1/shutdown", timeout=30)

    def close(self) -> None:
        """No-op: the Wind session belongs to windd and must outlive this client."""

    def __enter__(self) -> "WindClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
