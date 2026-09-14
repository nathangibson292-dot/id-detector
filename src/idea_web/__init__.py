"""FastAPI application and durable worker adapters for IDea.

The package root stays import-light on purpose: the local worker process imports
``idea_web.jobs`` and must never load an HTTP framework, so the web application is resolved lazily.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

__all__ = ["WebSettings", "create_app"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from idea_web import application

        return getattr(application, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
