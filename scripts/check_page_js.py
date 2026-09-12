"""Syntax-check **every** inline script of **every** page type this build can render.

The gate is deliberately blunt: render each page type as the server really renders it, pull every
inline ``<script>`` payload out of the produced HTML exactly as a browser would see it, and hand
each one to ``node --check``.  Nothing is hand-assembled here and no payload is skipped, because a
hand-written approximation of a page's script is precisely what stops being checked the moment the
real page changes.

Exit codes: ``0`` all payloads parse (or Node is absent, which is reported as a skip), non-zero if
any payload fails ``node --check``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

from id_detector.present import page, server
from id_detector.present.exports import build_projection
from id_detector.webapp.jobs import Job

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# The committed presentation fixtures and their loader: the check renders the same real inputs the
# 3a-i projection gate renders, rather than a stand-in that can drift away from the shipped page.
from tests.test_projection import _inputs, _source  # noqa: E402


class _Scripts(HTMLParser):
    """Collect the body of every inline ``<script>`` (a ``src=`` script has no body to check)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.items: list[str] = []
        self._inline = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._inline = not any(key == "src" for key, _ in attrs)
            if self._inline:
                self.items.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inline = False

    def handle_data(self, data: str) -> None:
        if self._inline:
            self.items[-1] += data

    def handle_entityref(self, name: str) -> None:  # pragma: no cover - scripts rarely carry these
        if self._inline:
            self.items[-1] += f"&{name};"

    def handle_charref(self, name: str) -> None:  # pragma: no cover - ditto
        if self._inline:
            self.items[-1] += f"&#{name};"


def _inline_scripts(html: str | bytes) -> list[str]:
    text = html.decode("utf-8") if isinstance(html, bytes) else html
    parser = _Scripts()
    parser.feed(text)
    parser.close()
    return [item for item in parser.items if item.strip()]


def _result_page(name: str, platform: str) -> str:
    """One real result page, rendered from a committed fixture through ``render_page``."""

    fixture, episodes, identities, acquire = _inputs(name)
    projection = build_projection(
        episodes, identities, acquire, min_track_ms=fixture["min_track_ms"]
    )
    return page.render_page(
        source=_source(platform).model_copy(update={"title": fixture["title"]}),
        episodes=episodes,
        identities=identities,
        duration_ms=fixture["duration_ms"],
        acquire=acquire,
        min_track_ms=fixture["min_track_ms"],
        projection=projection,
        status="degraded",
        reason="secondary_not_achieved",
        achieved="deep",
        analysed_at="2026-09-12T08:30:00Z",
    )


def _pages() -> list[tuple[str, str | bytes]]:
    """Every page type the build renders, named for the failure message."""

    running = Job(
        "0" * 32,
        "https://soundcloud.com/example/mix",
        "https://soundcloud.com/example/mix",
        "free",
        True,
        True,
        status="running",
        phase="recognise",
        resolved_title="Example mix",
    )
    failed = Job(
        "1" * 32,
        "https://soundcloud.com/example/other",
        "https://soundcloud.com/example/other",
        "max_accuracy",
        False,
        False,
        status="failed",
        phase="failed",
        failed_phase="ingest",
        error="boom",
    )
    # Every committed fixture, on every embed platform: the embed branch decides which player script
    # the page loads, and a fixture decides which row/lane/copy payloads it inlines.
    results = [
        (f"result page ({name}/{platform})", _result_page(name, platform))
        for name in ("garage", "boiler", "crowd", "cluster")
        for platform in ("soundcloud", "youtube", "mixcloud", "file")
    ]
    return results + [
        ("home (analyse enabled)", server._home_html([], [running, failed], "test-token")),
        (
            "home (inline form error)",
            server._home_html(
                [],
                [],
                "test-token",
                form_state=server._FormState(url="not a url", error="Use a complete web link."),
                failed_runs=[],
            ),
        ),
        ("read-only index", server._index_html([])),
        ("job page (running)", server._job_page_html(running, "test-token")),
        ("job page (failed)", server._job_page_html(failed, "test-token")),
    ]


def main() -> int:
    node = shutil.which("node")
    if node is None:
        print("page JavaScript check skipped: node is not installed (install Node to run it)")
        return 0

    payloads: list[tuple[str, str]] = []
    pages = _pages()
    for name, html in pages:
        scripts = _inline_scripts(html)
        # The read-only index deliberately ships no script; every other page type must have one, so
        # an empty extraction there means the extractor broke rather than the page changing.
        if not scripts and name != "read-only index":
            print(f"no inline script found in {name} — the extractor is broken")
            return 2
        payloads += [(f"{name} #{index}", body) for index, body in enumerate(scripts, start=1)]

    failures = 0
    with tempfile.TemporaryDirectory(prefix="idea-js-") as temporary:
        root = Path(temporary)
        for index, (name, payload) in enumerate(payloads, start=1):
            path = root / f"inline-{index}.js"
            path.write_text(payload, encoding="utf-8")
            checked = subprocess.run(
                [node, "--check", str(path)], capture_output=True, text=True, check=False
            )
            if checked.returncode:
                failures += 1
                print(f"FAILED node --check: {name}")
                print(checked.stderr.strip())
    if failures:
        print(f"page JavaScript check failed: {failures} of {len(payloads)} inline scripts")
        return 1
    print(
        f"page JavaScript check passed: {len(payloads)} inline scripts "
        f"across {len(pages)} page renders"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
