"""A ``w`` object that mimics ``WindPy.w`` but shares the broker session.

    from wind_broker import w      # instead of: from WindPy import w
    w.start()                       # no QR if windd is already logged in
    err, df = w.wsd("000300.SH", "close", "2024-01-01", "2024-01-31", usedf=True)
    data = w.wsq("000300.SH", "rt_last")    # WindData-like: .ErrorCode .Data ...
    w.stop()                        # no-op: never logs out the shared session

Like WindPy, vendor errors are *returned* (``ErrorCode != 0``), not raised.
"""

from __future__ import annotations

import functools
import warnings
from typing import Any

import pandas as pd

from .client import WindClient
from .config import DEFAULT_METHODS
from .errors import ProviderError

TIMEOUT_CODE = -40520008  # WindPy's own "Timeout Error!" code
GENERIC_CODE = -1


class WindData:
    """Same attributes as ``WindPy.w.WindData``."""

    def __init__(self, ErrorCode: int = 0, Codes: list | None = None, Fields: list | None = None,  # noqa: N803
                 Times: list | None = None, Data: list | None = None):
        self.ErrorCode = ErrorCode
        self.Codes = list(Codes or [])
        self.Fields = list(Fields or [])
        self.Times = list(Times or [])
        self.Data = list(Data or [])

    def __str__(self) -> str:
        return (f".ErrorCode={self.ErrorCode}\n.Codes={self.Codes}\n.Fields={self.Fields}\n"
                f".Times={self.Times}\n.Data={self.Data}")

    __repr__ = __str__


class WindProxy:
    def __init__(self, client: WindClient | None = None):
        self._client = client

    @property
    def client(self) -> WindClient:
        if self._client is None:
            self._client = WindClient()
        return self._client

    # -------------------------------------------------------------- session
    def start(self, options: Any = None, waitTime: int = 120, *args: Any, **kwargs: Any) -> WindData:  # noqa: N803
        try:
            if self.client.remote:
                # Workstations cannot log the hub in; report the hub's session instead.
                status = self.client.status()
                if not status.get("connected"):
                    return WindData(ErrorCode=GENERIC_CODE,
                                    Data=["the hub's Wind session is not logged in; log in on the hub"])
                return WindData(ErrorCode=0, Data=["OK!"])
            status = self.client.login(wait=True, wait_seconds=max(int(waitTime), 1))
        except ProviderError as exc:
            return WindData(ErrorCode=exc.code or GENERIC_CODE, Data=[str(exc)])
        if status.get("connected"):
            return WindData(ErrorCode=0, Data=["OK!"])
        return WindData(ErrorCode=GENERIC_CODE, Data=[status.get("last_error") or "Start Error!"])

    def isconnected(self) -> bool:
        try:
            return self.client.is_connected()
        except ProviderError:
            return False

    def stop(self) -> None:
        """No-op by design. Use ``w.logout()`` to really end the shared session."""

    close = stop

    def logout(self) -> None:
        self.client.logout()

    def status(self) -> dict[str, Any]:
        return self.client.status()

    # --------------------------------------------------------------- queries
    def _call(self, method: str, *args: Any, usedf: bool = False, **kwargs: Any):
        if "func" in kwargs:
            raise NotImplementedError("WindBroker cannot forward real-time callbacks; poll w.wsq() instead")
        try:
            if usedf:
                return 0, self.client.call(method, *args, **kwargs)
            payload = self.client.windata(method, *args, **kwargs)
            return WindData(**{k: payload.get(k) for k in ("ErrorCode", "Codes", "Fields", "Times", "Data")})
        except ProviderError as exc:
            code = exc.code if exc.code is not None else (TIMEOUT_CODE if exc.kind == "timeout" else GENERIC_CODE)
            if exc.kind not in {"vendor", "timeout"}:
                warnings.warn(f"WindBroker {method} failed: {exc}", RuntimeWarning, stacklevel=3)
            if usedf:
                return code, pd.DataFrame({"ErrorMessage": [str(exc)]})
            return WindData(ErrorCode=code, Data=[[str(exc)]])

    def __getattr__(self, name: str):
        if name in DEFAULT_METHODS:
            return functools.partial(self._call, name)
        raise AttributeError(name)

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(DEFAULT_METHODS))


w = WindProxy()
