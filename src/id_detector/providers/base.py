"""Small shared contracts for scanner providers."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot run in the current environment."""


class ProviderProtocolError(RuntimeError):
    """Raised for a known provider rejection or malformed response."""


class AmbiguousProviderOutcome(RuntimeError):
    """The request may have reached the provider, but no response was received."""


class UploadPermissionError(PermissionError):
    """Raised before I/O when both upload consent gates are not open."""


@dataclass(frozen=True)
class ProviderCapability:
    provider: str
    capability: str
    available: bool
    detail: str


@dataclass(frozen=True)
class AppConfig:
    """Non-secret local configuration relevant to third-party uploads."""

    allow_third_party_upload: bool = False

    @classmethod
    def load(cls, path: Path | None) -> AppConfig:
        if path is None or not path.is_file():
            return cls()
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
        value = payload.get("allow_third_party_upload", False)
        if not isinstance(value, bool):
            raise ValueError("allow_third_party_upload must be true or false")
        return cls(allow_third_party_upload=value)


def require_upload_permission(config: AppConfig, cli_confirmation: bool) -> None:
    """Enforce the plan's independent config and per-invocation consent gates."""

    missing: list[str] = []
    if not config.allow_third_party_upload:
        missing.append("allow_third_party_upload = true in config")
    if not cli_confirmation:
        missing.append("--i-own-this-audio-or-have-permission")
    if missing:
        message = "third-party upload refused; required: " + " and ".join(missing)
        raise UploadPermissionError(message)
