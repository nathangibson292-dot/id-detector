"""The Discogs 400-style vocabulary and the mapping of free-text tags onto it.

The Discogs-EffNet model predicts 400 Discogs styles, each named ``Parent---Style`` (for example
``Electronic---Speed Garage``).  Discogs release ``styles`` use exactly those style names, so they
map directly; Last.fm tags are free text ("ukg", "drum and bass", "seen live"), so they are
normalised, looked up in a synonym table, and anything that is not a style is ignored.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from importlib.resources import files

ELECTRONIC = "Electronic"


@lru_cache(maxsize=1)
def labels() -> tuple[str, ...]:
    """The model's 400 class labels, in output order (``Parent---Style``)."""

    text = files("id_detector.resources").joinpath("discogs400-styles.txt").read_text("utf-8")
    result = tuple(line.strip() for line in text.splitlines() if line.strip())
    if len(result) != 400:  # pragma: no cover - a packaging error, caught at import in tests
        raise RuntimeError(f"expected 400 Discogs styles, found {len(result)}")
    return result


def parent(label: str) -> str:
    return label.split("---", 1)[0]


def style(label: str) -> str:
    return label.split("---", 1)[-1]


def display(label: str) -> str:
    """A style's own name, with its parent added where the name alone is ambiguous."""

    name = style(label)
    if len(_by_style().get(_key(name), ())) > 1 and parent(label) != ELECTRONIC:
        return f"{name} ({parent(label)})"
    return name


def _key(text: str) -> str:
    """Lower-case, accents and punctuation folded, ``&``/``n`` spelled ``and``, single spaces."""

    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.casefold().replace("&", " and ").replace("'n'", " and ").replace("’", "")
    text = re.sub(r"[^a-z0-9+]+", " ", text)
    text = re.sub(r"\b(n|y)\b", "and", text)  # "drum n bass", "rock n roll"
    return " ".join(text.split())


@lru_cache(maxsize=1)
def _by_style() -> dict[str, tuple[str, ...]]:
    table: dict[str, list[str]] = {}
    for label in labels():
        table.setdefault(_key(style(label)), []).append(label)
    return {key: tuple(value) for key, value in table.items()}


#: Free-text tags (normalised by :func:`_key`) that mean a Discogs style of a different name.
#: Each maps to Electronic styles unless the value names the parent.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "ukg": ("UK Garage",),
    "uk garage": ("UK Garage",),
    "united kingdom garage": ("UK Garage",),
    "2 step": ("UK Garage",),
    "2step": ("UK Garage",),
    "two step": ("UK Garage",),
    "2 step garage": ("UK Garage",),
    "uk 2 step": ("UK Garage",),
    "future garage": ("UK Garage",),
    "garage": ("UK Garage", "Garage House"),
    "4x4 garage": ("Speed Garage",),
    "speedgarage": ("Speed Garage",),
    "speed garage": ("Speed Garage",),
    "bassline": ("Bassline",),
    "bassline house": ("Bassline",),
    "bass line": ("Bassline",),
    "niche": ("Bassline",),
    "4x4 bassline": ("Bassline",),
    "uk bassline": ("Bassline",),
    "bass house": ("Bassline", "House"),
    "uk bass": ("Bassline", "UK Garage"),
    "dnb": ("Drum n Bass",),
    "d and b": ("Drum n Bass",),
    "drum and bass": ("Drum n Bass",),
    "drumandbass": ("Drum n Bass",),
    "drum bass": ("Drum n Bass",),
    "liquid funk": ("Drum n Bass",),
    "liquid dnb": ("Drum n Bass",),
    "liquid drum and bass": ("Drum n Bass",),
    "neurofunk": ("Drum n Bass",),
    "jump up": ("Drum n Bass",),
    "jungle": ("Jungle",),
    "ragga jungle": ("Jungle",),
    "dubstep": ("Dubstep",),
    "brostep": ("Dubstep",),
    "riddim": ("Dubstep",),
    "deep dubstep": ("Dubstep",),
    "breaks": ("Breakbeat", "Breaks"),
    "breakbeat": ("Breakbeat",),
    "break beat": ("Breakbeat",),
    "nu skool breaks": ("Breaks",),
    "nu breaks": ("Breaks",),
    "uk breaks": ("Breaks",),
    "breakbeat hardcore": ("Hardcore",),
    "big beat": ("Big Beat",),
    "deep house": ("Deep House",),
    "tech house": ("Tech House",),
    "techhouse": ("Tech House",),
    "house": ("House",),
    "house music": ("House",),
    "chicago house": ("House",),
    "acid house": ("Acid House",),
    "electro house": ("Electro House",),
    "progressive house": ("Progressive House",),
    "tropical house": ("Tropical House",),
    "afro house": ("Tribal House",),
    "tribal house": ("Tribal House",),
    "minimal": ("Minimal",),
    "minimal techno": ("Minimal Techno",),
    "techno": ("Techno",),
    "hard techno": ("Hard Techno",),
    "acid techno": ("Acid", "Techno"),
    "dub techno": ("Dub Techno",),
    "trance": ("Trance",),
    "psytrance": ("Psy-Trance",),
    "psy trance": ("Psy-Trance",),
    "psychedelic trance": ("Psy-Trance",),
    "goa": ("Goa Trance",),
    "goa trance": ("Goa Trance",),
    "progressive trance": ("Progressive Trance",),
    "hardstyle": ("Hardstyle",),
    "hardcore": ("Hardcore",),
    "happy hardcore": ("Happy Hardcore",),
    "gabber": ("Gabber",),
    "hard house": ("Hard House",),
    "grime": ("Grime",),
    "garage house": ("Garage House",),
    "funky house": ("House",),
    "uk funky": ("House",),
    "disco house": ("Disco", "House"),
    "french house": ("House", "Disco"),
    "nu disco": ("Nu-Disco",),
    "nudisco": ("Nu-Disco",),
    "italo disco": ("Italo-Disco",),
    "euro disco": ("Euro-Disco",),
    "eurodance": ("Eurodance",),
    "euro house": ("Euro House",),
    "hi nrg": ("Hi NRG",),
    "electro": ("Electro",),
    "electroclash": ("Electroclash",),
    "idm": ("IDM",),
    "intelligent dance music": ("IDM",),
    "ambient": ("Ambient",),
    "dark ambient": ("Dark Ambient",),
    "downtempo": ("Downtempo",),
    "chillout": ("Downtempo",),
    "chill out": ("Downtempo",),
    "trip hop": ("Trip Hop",),
    "triphop": ("Trip Hop",),
    "synthwave": ("Synthwave",),
    "synthpop": ("Synth-pop",),
    "synth pop": ("Synth-pop",),
    "darkwave": ("Darkwave",),
    "ebm": ("EBM",),
    "industrial": ("Industrial",),
    "footwork": ("Juke",),
    "juke": ("Juke",),
    "halftime": ("Halftime",),
    "breakcore": ("Breakcore",),
    "glitch": ("Glitch",),
    "leftfield": ("Leftfield",),
    "experimental electronic": ("Experimental",),
    "vaporwave": ("Vaporwave",),
    "chillwave": ("Chillwave",),
    "chiptune": ("Chiptune",),
    "dance pop": ("Dance-pop",),
    "dancepop": ("Dance-pop",),
    "hip house": ("Hip-House",),
    "ghetto house": ("Ghetto House",),
    "broken beat": ("Broken Beat",),
    "future jazz": ("Future Jazz",),
    "acid jazz": ("Acid Jazz",),
    # Other parents: a few common free-text spellings.
    "hip hop": ("Hip Hop---Boom Bap", "Hip Hop---Pop Rap"),
    "rap": ("Hip Hop---Pop Rap",),
    "trap": ("Hip Hop---Trap",),
    "drill": ("Hip Hop---Hardcore Hip-Hop",),
    "r and b": ("Funk / Soul---Contemporary R&B",),
    "rnb": ("Funk / Soul---Contemporary R&B",),
    "contemporary r and b": ("Funk / Soul---Contemporary R&B",),
    "dancehall": ("Reggae---Dancehall",),
    "reggae": ("Reggae---Reggae",),
    "dub": ("Dub",),
    "funk": ("Funk / Soul---Funk",),
    "soul": ("Funk / Soul---Soul",),
    "disco": ("Disco",),
    "indie rock": ("Rock---Indie Rock",),
    "indie": ("Rock---Indie Rock",),
    "alternative rock": ("Rock---Alternative Rock",),
    "punk": ("Rock---Punk",),
    "metal": ("Rock---Heavy Metal",),
    "heavy metal": ("Rock---Heavy Metal",),
    "pop rock": ("Rock---Pop Rock",),
    "jazz": ("Jazz---Contemporary Jazz",),
}

#: Tags that are not a style: places, decades, moods, listening habits, broad umbrellas.
_JUNK = re.compile(
    r"^(seen live|favou?rites?|favou?rite songs?|loved|love|awesome|cool|chill|"
    r"\d{2,4}s?|\d+|british|uk|english|london|usa|american|german|french|dutch|irish|scottish|"
    r"male vocalists?|female vocalists?|vocal|instrumental|beautiful|party|summer|dance|"
    r"electronic|electronica|electro music|edm|club|bass|bass music|dance music|songs i love|"
    r"my music|spotify|soundcloud|youtube|radio|banger|bangers|classic|classics|remix|"
    r"underground|uk underground|all|music|free download|albums i own|pop|rock)$"
)
#: Broad umbrella tags that say only the parent genre.
_PARENT_TAGS = {
    "electronic": ELECTRONIC,
    "electronica": ELECTRONIC,
    "edm": ELECTRONIC,
    "dance": ELECTRONIC,
    "dance music": ELECTRONIC,
    "electro music": ELECTRONIC,
    "bass music": ELECTRONIC,
    "rock": "Rock",
    "pop": "Pop",
}


def _resolve(names: tuple[str, ...]) -> tuple[str, ...]:
    table = set(labels())
    result: list[str] = []
    for name in names:
        label = name if "---" in name else f"{ELECTRONIC}---{name}"
        if label in table and label not in result:
            result.append(label)
    return tuple(result)


def map_tag(tag: str) -> tuple[str, ...]:
    """The Discogs labels a free-text tag means (empty when it is junk or unknown)."""

    key = _key(tag)
    if not key or _JUNK.match(key):
        return ()
    if key in _SYNONYMS:
        return _resolve(_SYNONYMS[key])
    found = _by_style().get(key)
    if found:
        # A bare style name shared by several parents ("Disco") means the Electronic one first.
        electronic = tuple(label for label in found if parent(label) == ELECTRONIC)
        return electronic or found
    return ()


def map_discogs_style(name: str) -> tuple[str, ...]:
    """A Discogs release style is already in the vocabulary (unless it is outside the top 400)."""

    found = _by_style().get(_key(name))
    if found:
        return found
    return map_tag(name)


def parent_tag(tag: str) -> str | None:
    """The parent genre a broad umbrella tag names ("electronic" -> Electronic), else None."""

    return _PARENT_TAGS.get(_key(tag))
