"""The Genre page and its endpoints (local mode), with a fake checker: offline, no network."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from id_detector.genre.service import STEPS
from id_detector.genre.source import SourceError, classify
from id_detector.present import theme
from idea_web import genre_page, pages
from idea_web.application import create_app
from idea_web.http import inline_script_hashes
from tests.idea_web.test_parity import request, token


class _Check:
    def __init__(self, check_id: str, text: str, pick: str | None) -> None:
        self.id = check_id
        self.text = text
        self.pick = pick

    def public(self) -> dict:
        return {
            "id": self.id,
            "status": "running",
            "steps": [{"key": k, "name": n, "state": "waiting"} for k, n in STEPS if k != "model"],
            "message": "Finding the track",
            "download": None,
            "candidates": [],
            "track": None,
            "result": None,
            "error": None,
            "elapsed": 0.1,
        }


class FakeGenre:
    """Stands in for ``GenreService``: records starts, serves one saved cover image."""

    def __init__(self, folder: Path) -> None:
        self.started: list[tuple[str, str | None]] = []
        self.checks: dict[str, _Check] = {}
        self.art = folder / "art-0123456789abcdef01234567.png"
        self.art.write_bytes(b"\x89PNG fake")

    def start(self, text: str, pick: str | None = None) -> _Check:
        classify(text)  # the real input rules, so a bad input is refused the real way
        check = _Check(f"{len(self.started):016x}", text, pick)
        self.started.append((text, pick))
        self.checks[check.id] = check
        return check

    def get(self, check_id: str):
        return self.checks.get(check_id)

    def model_ready(self) -> bool:
        return False

    def recent(self, limit: int = 8) -> list[dict]:
        return [
            {
                "input": "Double 99 - RIP Groove",
                "title": "RIP Groove",
                "genre": "Speed Garage",
                "bpm": 128.0,
            }
        ]

    def artwork_path(self, art_id: str):
        return self.art if art_id == "0123456789abcdef01234567" else None


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name in ("DISCOGS_API_TOKEN", "DISCOGS_TOKEN", "LASTFM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    fake = FakeGenre(tmp_path)
    application = create_app(tmp_path / "work", genre_service=fake)
    application.state.fake_genre = fake
    return application


def test_the_genre_page_renders_with_a_strict_policy(app):
    response = request(app, "GET", "/genre?q=Double%2099%20-%20RIP%20Groove")
    assert response.status_code == 200
    body = response.text
    assert 'id="genre-form"' in body and 'id="gq"' in body
    assert 'value="Double 99 - RIP Groove"' in body
    assert "What genre" in body
    # No inline script: the page script is the static app.js.
    assert inline_script_hashes(body.encode()) == []
    assert pages.SCRIPT_SRC in body
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy and "'unsafe-inline'" not in policy.split("style-src")[0]
    # Look-ups off: the page says so and how to turn them on; first run warns of the download.
    assert "DISCOGS_API_TOKEN" in body and "LASTFM_API_KEY" in body
    assert "downloads the genre model" in body
    # Recent answers link back with the question.
    assert 'href="/genre?q=Double%2099%20-%20RIP%20Groove"' in body and "128 BPM" in body
    csrf = re.search(r'data-csrf="([^"]+)"', body).group(1)
    assert csrf == token(app)


def test_lookups_on_are_shown(app, monkeypatch):
    monkeypatch.setenv("DISCOGS_API_TOKEN", "x")
    monkeypatch.setenv("LASTFM_API_KEY", "y")
    body = request(app, "GET", "/genre").text
    assert "Discogs and Last.fm look-ups on" in body


def test_the_top_bar_and_home_page_lead_to_genre(app):
    assert 'href="/genre"' in theme.topbar_html()
    assert 'href="/genre">Genre</a>' in request(app, "GET", "/genre").text


def test_starting_a_check_needs_the_token(app):
    refused = request(app, "POST", "/genre/check", json={"input": "Double 99 - RIP Groove"})
    assert refused.status_code == 403
    assert app.state.fake_genre.started == []


def test_starting_and_polling_a_check(app):
    headers = {"X-CSRF-Token": token(app)}
    started = request(
        app, "POST", "/genre/check", json={"input": "Double 99 - RIP Groove"}, headers=headers
    )
    assert started.status_code == 200
    check_id = started.json()["id"]
    assert app.state.fake_genre.started == [("Double 99 - RIP Groove", None)]
    status = request(app, "GET", f"/genre/check/{check_id}")
    assert status.status_code == 200
    assert [s["key"] for s in status.json()["steps"]] == ["find", "listen", "lookup", "done"]
    missing = request(app, "GET", "/genre/check/ffffffffffffffff")
    assert missing.status_code == 404 and "expired" in missing.json()["error"]
    picked = request(
        app,
        "POST",
        "/genre/check",
        headers=headers,
        json={"input": "Double 99 - RIP Groove", "pick": "https://youtu.be/abc"},
    )
    assert picked.status_code == 200
    assert app.state.fake_genre.started[-1] == ("Double 99 - RIP Groove", "https://youtu.be/abc")


@pytest.mark.parametrize(
    "body",
    [{"input": ""}, {"input": "https://example.com/x"}, {"input": 5}, {"input": "a", "pick": 3}],
)
def test_bad_input_is_a_plain_error(app, body):
    response = request(app, "POST", "/genre/check", json=body, headers={"X-CSRF-Token": token(app)})
    assert response.status_code == 400
    assert response.json()["error"]


def test_a_saved_cover_is_served_and_nothing_else(app):
    art = request(app, "GET", "/genre/art/0123456789abcdef01234567")
    assert art.status_code == 200 and art.headers["content-type"] == "image/png"
    assert request(app, "GET", "/genre/art/ffffffffffffffffffffffff").status_code == 404
    assert request(app, "GET", "/genre/art/../../etc").status_code == 404
    assert request(app, "GET", "/genre/nope").status_code == 404


def test_the_page_script_is_in_the_static_bundle_and_parses():
    assert genre_page.GENRE_JS.strip() in pages.STATIC_JS.decode("utf-8")
    assert ".g-card" in pages.STATIC_CSS.decode("utf-8")
    # Untrusted text is only ever set as text, never parsed as HTML.
    assert "innerHTML" not in genre_page.GENRE_JS
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(genre_page.__file__).with_name("genre.js")
    result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_home_entry_is_a_plain_get_form():
    entry = genre_page.home_entry_html()
    assert 'action="/genre"' in entry and 'method="get"' in entry and 'name="q"' in entry


def test_the_fake_uses_the_real_input_rules():
    with pytest.raises(SourceError):
        classify("https://example.com/x")


def test_the_home_template_places_the_genre_entry():
    template = Path(genre_page.__file__).with_name("templates") / "home.html"
    assert "{{ genre_entry | safe }}" in template.read_text("utf-8")
