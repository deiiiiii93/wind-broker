"""WindBroker: one persistent WindPy session shared by every app on this Mac."""

__version__ = "1.0.1"

from .client import WindClient  # noqa: E402
from .errors import DaemonUnavailable, ProviderError  # noqa: E402
from .proxy import WindData, WindProxy, w  # noqa: E402

__all__ = ["WindClient", "ProviderError", "DaemonUnavailable", "WindData", "WindProxy", "w", "__version__"]
