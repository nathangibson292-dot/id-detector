"""Deterministic, network-free search links and the SoundCloud gate-host classifier.

Bandcamp, Beatport and Traxsource have no usable public catalogue API for individuals (see
``docs/research/01-ingestion-and-sources.md`` Part B), so we only ever emit their *search* pages —
built from the episode's artist and title with no request.  Juno Download is closed and Odesli is
dead, so neither is included.  The gate-host list classifies a SoundCloud ``purchase_url`` as a
download gate (Hypeddit et al.) that we deliberately link to and *never* automate.
"""

from __future__ import annotations

from urllib.parse import quote_plus, urlsplit

from id_detector.enrich.match import parse_title, tokens

# Newer download-gate hosts (the 2026 landscape after SoundCloud paused Hypeddit's API); classified
# by registrable-domain suffix so subdomains (go.hypeddit.com, …) match too.  Never automated.
GATE_HOSTS = frozenset(
    {
        "hypeddit.com",
        "hypeddit.co",
        "bettergate.com",
        "timbrgate.com",
        "backstaged.com",
        "backstaged.io",
        "fangate.eu",
        "stillhype.com",
        "toneden.io",
        "theartistunion.com",
        "gate.fm",
    }
)


def _search_query(artist: str, title: str) -> str:
    base, _version = parse_title(title)
    # Keep the raw artist and title words; drop only empties.  This mirrors the plan's `<artist
    # title>` search term while staying deterministic.
    parts = [part for part in (artist.strip(), title.strip()) if part]
    return " ".join(parts) if parts else " ".join(base)


def search_links(artist: str, title: str) -> list[dict[str, str]]:
    """Always-available search-page links for the three no-API stores, in a fixed order."""

    query = _search_query(artist, title)
    encoded = quote_plus(query)
    return [
        {"source": "bandcamp", "url": f"https://bandcamp.com/search?q={encoded}&item_type=t"},
        {"source": "beatport", "url": f"https://www.beatport.com/search/tracks?q={encoded}"},
        {"source": "traxsource", "url": f"https://www.traxsource.com/search?term={encoded}"},
    ]


def _registrable_suffix(host: str) -> str:
    labels = host.casefold().split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host.casefold()


def is_gate_host(url: str | None) -> bool:
    if not url:
        return False
    host = urlsplit(url).hostname or ""
    if not host:
        return False
    if _registrable_suffix(host) in GATE_HOSTS:
        return True
    return any(
        host.casefold() == gate or host.casefold().endswith("." + gate) for gate in GATE_HOSTS
    )


def classify_soundcloud(
    *,
    downloadable: bool | None,
    has_downloads_left: bool | None,
    purchase_url: str | None,
) -> str:
    """Map SoundCloud track flags to the plan's acquisition classes.

    Priority: a *native* free download wins; else a ``purchase_url`` is a download gate when its
    host is a known gate, else a plain buy link; otherwise nothing is offered.
    """

    if downloadable and has_downloads_left:
        return "free_download_native"
    if purchase_url:
        return "gate_link" if is_gate_host(purchase_url) else "buy_link"
    return "none"


def token_query_is_empty(artist: str, title: str) -> bool:
    return not tokens(artist) and not tokens(title)
