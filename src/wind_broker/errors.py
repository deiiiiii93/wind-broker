"""Exceptions raised by the client SDK."""

from __future__ import annotations


class ProviderError(RuntimeError):
    """A Wind request failed (vendor error code, timeout, broker unavailable...).

    ``kind`` is the broker's classification (``vendor``, ``timeout``, ``busy``,
    ``login``, ``unavailable``...) and ``code`` the Wind ErrorCode when known.
    """

    def __init__(self, message: str, *, kind: str = "error", code: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.code = code


class DaemonUnavailable(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, kind="unavailable")
