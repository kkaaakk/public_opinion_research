"""Public, runtime-independent API for the public opinion business package."""

from .health import VERSION as __version__
from .health import get_status, ping

__all__ = ["__version__", "get_status", "ping"]
