"""Recognition provider adapters and their shared safety contracts."""

from id_detector.providers.base import (
    ProviderProtocolError,
    ProviderUnavailable,
    UploadPermissionError,
)

__all__ = ["ProviderProtocolError", "ProviderUnavailable", "UploadPermissionError"]
