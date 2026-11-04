"""Alias module: ``from wind_broker_client import WindClient, w``."""

from wind_broker import DaemonUnavailable, ProviderError, WindClient, WindData, w  # noqa: F401

__all__ = ["WindClient", "ProviderError", "DaemonUnavailable", "WindData", "w"]
