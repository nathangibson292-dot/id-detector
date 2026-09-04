"""Disabled Panako provider skeleton for the owner's pending JDK decision."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from id_detector.providers.base import ProviderCapability, ProviderUnavailable

PROVIDER = "panako"
PROVIDER_CONFIG_VERSION = "panako-v1.json"
CAPABILITY = ProviderCapability(
    PROVIDER,
    "local_index_query",
    False,
    "JDK not found — Panako disabled",
)


@dataclass(frozen=True)
class PanakoConfig:
    index_path: Path


def doctor_detail() -> tuple[str, str]:
    if shutil.which("java") is None:
        return "WARN", "JDK not found — Panako disabled"
    return "WARN", "JDK found, but Panako remains disabled pending the owner's v1 decision"


class PanakoProvider:
    """Capability placeholder; no Java process is started in Stage 3."""

    capability = CAPABILITY

    def __init__(self, config: PanakoConfig) -> None:
        self.config = config

    @staticmethod
    def _unavailable() -> NoReturn:
        raise ProviderUnavailable("JDK not found")

    def create_index(self, *_: object, **__: object) -> NoReturn:
        self._unavailable()

    def query(self, *_: object, **__: object) -> NoReturn:
        self._unavailable()

    def recognise(self, *_: object, **__: object) -> NoReturn:
        self._unavailable()

    def close(self) -> NoReturn:
        self._unavailable()
