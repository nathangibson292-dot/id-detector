"""Identity graph v0: work equality, corroborated recordings, and conflict vetoes."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    HintRecord,
    IdentitiesRecord,
    IdentityAssertion,
    IdentityCandidate,
    IdentityNode,
    IdentityWork,
    ObservationRecord,
    compose_natural_key,
    make_id,
)
from id_detector.io import atomic_write_json, write_completion_sidecar
from id_detector.semantics import RECORDING_NAMESPACES, merge_recording_identities

_ALLOWED_NAMESPACES = RECORDING_NAMESPACES | {"mb_work", "mb_release", "text"}
_NS_ALIASES = {
    "apple_id": "apple",
    "apple_music": "apple",
    "deezer_id": "deezer",
    "isrc_id": "isrc",
    "musicbrainz_recording": "mb_recording",
    "musicbrainz_work": "mb_work",
    "spotify_id": "spotify",
}
#: The featuring marker carries no identity of its own: "MPH ft. Cecelia - Rush" and "MPH - Rush
#: (feat. Cecelia)" name the same people, and differ by nothing but the spelling of "featuring" —
#: two tokens too short for the near-spelling rule.  The featured NAME stays in the word set.
#: (1b-ii, decided on the release-1 corpus: three duplicate works merged, no listed row changed.)
_FEATURING = frozenset({"ft", "feat", "featuring"})


def normalise_text(value: str | None) -> str:
    """Normalise display text without erasing version-significant words."""

    if not value:
        return ""
    normalised = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"\s+", " ", normalised)


def display_label_of(fields: tuple[str | None, str | None]) -> str:
    """``Artist - Title`` of a stored (artist, title) pair, as :func:`display_label` shows it."""

    return f"{fields[0] or 'Unknown artist'} - {fields[1] or 'Unknown title'}"


def work_text_key(observation: ObservationRecord) -> str | None:
    artist = normalise_text(observation.raw_label.artist)
    title = normalise_text(observation.raw_label.title)
    return f"{artist}|{title}" if artist and title else None


def display_label(observation: ObservationRecord) -> str:
    artist = observation.raw_label.artist or "Unknown artist"
    title = observation.raw_label.title or "Unknown title"
    return f"{artist} - {title}"


def _provider_nodes(observation: ObservationRecord) -> list[tuple[str, str]]:
    nodes: list[tuple[str, str]] = []
    for raw_ns, raw_value in sorted(observation.provider_ids.items()):
        ns = _NS_ALIASES.get(raw_ns.casefold(), raw_ns.casefold())
        if ns not in _ALLOWED_NAMESPACES or isinstance(raw_value, (dict, list, bool)):
            continue
        value = str(raw_value).strip()
        if value:
            nodes.append((ns, value))
    return nodes


def candidate_recording_supported(
    *,
    contested: bool,
    member_nodes: Iterable[str],
    recording_node_sources: Mapping[str, set[str]],
) -> bool:
    """Authoritative recording-support test, shared by fusion and calibration reconstruction.

    A candidate's recording (version) identity is *supported* when it is not contested and either
    at least two of its member nodes are recording-specific ids, or a single recording node is
    asserted by at least two independent sources.  Computed identically at analyse time (the
    identity graph) and at fit time (``calibrate.reconstruct``) so the version feature can never
    differ between the two.
    """

    if contested:
        return False
    nodes = list(member_nodes)
    recording_nodes = [node for node in nodes if node.split(":", 1)[0] in RECORDING_NAMESPACES]
    if len(recording_nodes) >= 2:
        return True
    return any(len(recording_node_sources.get(node, ())) >= 2 for node in nodes)


def recording_node_sources_from_observations(
    observations: Iterable[ObservationRecord],
) -> dict[str, set[str]]:
    """Recompute, from persisted observations, the independent sources for each recording node.

    Mirrors the analyse-time construction inside :func:`build_identity_graph` (final matches only,
    provider node ids, one ``provider:<name>`` source per node) so that
    :func:`candidate_recording_supported` sees the same evidence at fit time as at analyse time.
    """

    sources: dict[str, set[str]] = {}
    for observation in observations:
        if not (observation.is_final and observation.status == "match"):
            continue
        for ns, value in _provider_nodes(observation):
            if ns in RECORDING_NAMESPACES:
                sources.setdefault(f"{ns}:{value}", set()).add(f"provider:{observation.provider}")
    return sources


def _assertion(
    media_key: str,
    *,
    a: str,
    b: str,
    relation: str,
    source_kind: str,
    source_record_id: str,
    independent_of: str,
    confidence: int,
) -> IdentityAssertion:
    left, right = sorted((a, b))
    values = {
        "a": left,
        "b": right,
        "relation": relation,
        "source": {"record_id": source_record_id},
    }
    return IdentityAssertion(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        id=make_id(
            media_key,
            "identity_assertion",
            compose_natural_key("identity_assertion", values),
        ),
        a=left,
        b=right,
        relation=relation,
        source={"kind": source_kind, "record_id": source_record_id},
        independent_of=independent_of,
        confidence=confidence,
    )


@dataclass(frozen=True)
class IdentityBuildResult:
    record: IdentitiesRecord
    observation_candidates: dict[str, str]
    hint_candidates: dict[str, str]
    hint_work_ids: dict[str, str]
    candidate_labels: dict[str, tuple[str, str]]
    recording_supported: frozenset[str]
    #: Every link a TOLERANCE made (fusion:5): ``(kind, left label, right label, tolerances)``,
    #: ``kind`` being ``"hint_to_audio"`` or ``"hint_to_hint"`` — the audit trail of the one rule.
    tolerant_links: tuple[tuple[str, str, str, tuple[str, ...]], ...] = ()
    #: The names solid in this mix (credited on two or more recognised works): what initials may
    #: stand for.
    solid: frozenset[tuple[str, ...]] = frozenset()


class _UnionFind:
    def __init__(self, values: list[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        keep, discard = sorted((left_root, right_root))
        self.parent[discard] = keep


def _within_one_edit(x: str, y: str) -> bool:
    """True if x and y are equal or one insertion/deletion/substitution apart (Levenshtein ≤ 1)."""

    if x == y:
        return True
    lx, ly = len(x), len(y)
    if abs(lx - ly) > 1:
        return False
    if lx == ly:  # a single substitution
        return sum(cx != cy for cx, cy in zip(x, y, strict=True)) == 1
    if lx > ly:  # make x the shorter one; y is x with one extra char
        x, y = y, x
    i = j = 0
    edited = False
    while i < len(x) and j < len(y):
        if x[i] == y[j]:
            i += 1
            j += 1
        elif edited:
            return False
        else:
            edited = True
            j += 1
    return True


def _near_spelling(x: str, y: str) -> bool:
    """Whether two DIFFERENT tokens are one word spelled two ways (the tolerant half of
    :func:`_word_sets_corroborate`)."""

    if len(x) >= 4 and len(y) >= 4 and _within_one_edit(x, y):
        return True
    # A short word missing or gaining one trailing LETTER ("kno"/"know", "od"/"odf") is the
    # same word too; a substitution at that length ("up"/"us") or a digit ("1"/"12", two
    # numbered titles) is not.  Decided on the release-1 corpus (1b-ii): both instances were
    # the same track, no listed row changed, pooled likely precision unchanged.
    short, long = sorted((x, y), key=len)
    return (
        len(short) >= 2
        and len(long) == len(short) + 1
        and short.isalpha()
        and long.isalpha()
        and long.startswith(short)
    )


def _word_sets_corroborate(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether a hint word set and an audio word set name the same work (order-independent).

    Exact rule: one set fully contains the other (≥2 words), so collaborators/extra words never
    block a genuine ID.  Tolerant rule: everything matches except a single long title token that is
    only a near-spelling apart (e.g. "clubgrls" vs "clubgirls") — that is a crowd ID of the SAME
    track, not a different one, so it should corroborate the audio match rather than duplicate it
    — or a single short word one trailing letter apart ("kno" vs "know").
    """

    if min(len(a), len(b)) < 2:
        return False
    if a <= b or b <= a:
        return True
    a_only = a - b
    b_only = b - a
    if len(a_only) == 1 and len(b_only) == 1 and (a & b):
        (x,) = tuple(a_only)
        (y,) = tuple(b_only)
        return _near_spelling(x, y)
    return False


#: "C.R.T.B." / "c.r.t.b" is one name, the same one a label spells "CRTB".
_DOTTED_INITIALS = re.compile(r"(?<![a-z0-9])(?:[a-z0-9]\.){2,}(?:[a-z0-9](?![a-z0-9]))?")
#: A bracket that says which VERSION played, never what the work is called.  A featuring bracket
#: is NOT one: the featured name is part of who made the work (1b-ii: a different featured artist
#: stays a different work), so it stays in the words.
_VERSION_BRACKET = re.compile(
    r"[(\[](?![^()\[\]]*\b(?:feat|ft|featuring)\b)[^()\[\]]*\b(?:remix|edit|rework|bootleg|mix"
    r"|mixed|vip|dub|version|extended|original|refix|flip)\b[^()\[\]]*[)\]]"
)
_FEATURING_BRACKET = re.compile(r"[(\[][^()\[\]]*\b(?:feat|ft|featuring)\b[^()\[\]]*[)\]]")
#: A listener's note about the release, not part of the name: "(unreleased)", "(UR)", "[free dl]".
_ANNOTATION_BRACKET = re.compile(
    r"[(\[]\s*(?:unreleased\*?|ur|forthcoming[^()\[\]]*|dubplate|free(?:\s+(?:dl|download))?)"
    r"\s*[)\]]"
)
_ANY_BRACKET = re.compile(r"[(\[]([^()\[\]]*)[)\]]")
#: Words of a version bracket that credit nobody ("Extended Mix"); the rest of it is a remixer.
_VERSION_WORDS = frozenset(
    {
        "remix", "edit", "rework", "bootleg", "mix", "mixed", "vip", "dub", "version",
        "extended", "original", "refix", "flip", "radio", "club",
    }
)  # fmt: skip
#: A listener hedging around the name — "i think it's …", "… i think", "pretty sure this is …".
#: Only at the two ends of a field, and only these set phrases: a title is never trimmed inside.
_FILLER_LEAD = re.compile(
    r"^\s*(?:(?:i\s+)?(?:think|believe|reckon)\s+|(?:i'?m\s+)?pretty\s+sure\s+|sounds\s+like\s+)?"
    r"(?:(?:it'?s|it\s+is|this\s+is|that'?s|that\s+is)\s+)?"
)
_FILLER_TAIL = re.compile(r"\s+(?:i\s+(?:think|believe|reckon)|maybe|probably)[\s.!?]*$")
#: A "name" made of nothing but these names no track and no artist: "Benwal - ID", "TBC - TBC",
#: "Artist - unreleased".  Placeholders never corroborate and never get an identity of their own.
_PLACEHOLDER_WORDS = frozenset(
    {"id", "ids", "tbc", "tba", "unknown", "unreleased", "ur", "forthcoming", "dubplate"}
)
#: Words too common to identify anybody: "DJ" alone is not "DJ Alice".
_COMMON_NAME_WORDS = frozenset(
    {
        "dj", "mc", "the", "and", "a", "an", "of", "by", "vs", "x", "mr", "mrs", "ms", "dr",
        "de", "la", "le", "el", "da", "van", "von", "with", "w", "presents", "pres",
    }
)  # fmt: skip
#: What separates one credited name from the next in an artist field.
_NAME_SEPARATOR = re.compile(
    r"\s*(?:&|,|/|\+|;|\s+x\s+|\s+and\s+|\s+vs\.?\s+|\s+with\s+|\bw/|\b(?:feat|ft|featuring)\b\.?)\s*"
)
#: An un-bracketed featuring credit that closes a field: "Song feat. Guest".
_FEATURING_TAIL = re.compile(r"\s(?:feat|ft|featuring)\b\.?\s+(.*)$")
#: A joined spelling is only trusted at a length no two unrelated short words reach by accident.
_JOINED_MIN_CHARS = 5
#: One spelling slip is only trusted in a word this long, and only as a dropped or added letter:
#: "Temors" is "Tremors", but "Angel" is not "Anger" and "Falling" is not "Calling".
_SLIP_MIN_CHARS = 6
#: A comment answer typed with a bare hyphen ("juice-mall grab"): the parser leaves it a title.
_BARE_HYPHEN_PAIR = re.compile(r"^([^-]*\S)-(\S[^-]*)$")


def _hint_field(value: str | None, *, hedges: bool = True) -> str:
    """One field of a casual answer, without the release note and (unless ``hedges``) without
    the listener's own hedging words around the name."""

    text = _ANNOTATION_BRACKET.sub(" ", normalise_text(value))
    if hedges:
        return text
    return _FILLER_TAIL.sub("", _FILLER_LEAD.sub("", text))


def _label_tokens(value: str | None) -> list[str]:
    """The identity words of one label field, in order, spelled the way both sides are compared.

    Case, punctuation and accents carry no identity ("Tiësto" is "Tiesto"), a dotted run of
    initials is one word ("C.R.T.B." is "CRTB"), and the featuring marker is dropped (its NAME
    stays) — the same folding for a provider label and for a casual comment answer.
    """

    text = unicodedata.normalize("NFKD", normalise_text(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _DOTTED_INITIALS.sub(lambda match: match.group(0).replace(".", ""), text)
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in _FEATURING]


def _fold_joined(tokens: list[str], other: frozenset[str]) -> list[str]:
    """Join two adjacent words the other side spells as one ("bullet tooth" / "bullettooth")."""

    folded: list[str] = []
    index = 0
    while index < len(tokens):
        joined = tokens[index] + tokens[index + 1] if index + 1 < len(tokens) else ""
        if (
            len(joined) >= _JOINED_MIN_CHARS
            and joined in other
            and tokens[index] not in other
            and tokens[index + 1] not in other
        ):
            folded.append(joined)
            index += 2
        else:
            folded.append(tokens[index])
            index += 1
    return folded


def _without_trailing_version(tokens: list[str]) -> list[str]:
    """ "Song Original Mix" and "Never Let You Go Edit" are "Song" and "Never Let You Go"."""

    trimmed = list(tokens)
    while len(trimmed) > 1 and trimmed[-1] in _VERSION_WORDS:
        trimmed.pop()
    return trimmed


def is_placeholder(value: str | None) -> bool:
    """Whether a field names nothing: empty, or only "ID" / "TBC" / "unknown" / a release note."""

    tokens = _label_tokens(_ANNOTATION_BRACKET.sub(" ", normalise_text(value)))
    return not set(tokens) - _PLACEHOLDER_WORDS


def names_a_title(title: str | None) -> bool:
    """Whether a hint's title is a name at all, rather than a note that the track has none."""

    return not is_placeholder(title)


@dataclass(frozen=True)
class _LabelSide:
    """One label read the one way both sides are read: who is credited, and what it is called."""

    names: tuple[tuple[str, ...], ...]  # every credited name, as its words
    artist_words: frozenset[str]  # every word of the artist field (a hint's may hold chatter)
    main: frozenset[frozenset[str]]  # the artist field's own names, each as its identity
    featured: frozenset[frozenset[str]]  # each featured guest, as a WHOLE name's identity
    titles: tuple[tuple[str, ...], ...]  # the title, in each form it may be quoted in


def _split_names(text: str) -> list[tuple[str, ...]]:
    names = [tuple(_label_tokens(part)) for part in _NAME_SEPARATOR.split(text)]
    return [name for name in names if name]


def _name_identity(name: tuple[str, ...]) -> frozenset[str]:
    """What identifies one credited name: its words without the common ones ("DJ Boring" is
    "boring", "Bob Jones" is both words) — so two names are compared whole, never word by word:
    "DJ Boring" is not "DJ Seinfeld" and "Bob Jones" is not "Bob Smith"."""

    return frozenset(word for word in name if word not in _COMMON_NAME_WORDS) or frozenset(name)


def _read_label(artist: str, title: str) -> _LabelSide:
    """Read ``artist`` / ``title`` (already normalised text) into names and title forms."""

    featured_text = [match.group(0) for match in _FEATURING_BRACKET.finditer(title)]
    remixers = [
        tuple(token for token in _label_tokens(match.group(0)) if token not in _VERSION_WORDS)
        for match in _VERSION_BRACKET.finditer(title)
    ]
    core = _FEATURING_BRACKET.sub(" ", _VERSION_BRACKET.sub(" ", title))
    tail = _FEATURING_TAIL.search(core)
    if tail is not None:
        featured_text.append(tail.group(1))
        core = core[: tail.start()]
    main_artist = artist
    artist_tail = _FEATURING_TAIL.search(f" {artist}")
    if artist_tail is not None:
        featured_text.append(artist_tail.group(1))
        main_artist = f" {artist}"[: artist_tail.start()]
    featured_names = [name for text in featured_text for name in _split_names(text)]

    forms: list[tuple[str, ...]] = []
    candidates = [
        _label_tokens(core),
        _label_tokens(_ANY_BRACKET.sub(" ", core)),
        *(
            tokens
            for match in _ANY_BRACKET.finditer(core)
            if len(tokens := _label_tokens(match.group(1))) >= 2
        ),
    ]
    for tokens in candidates:
        for form in (tuple(tokens), tuple(_without_trailing_version(tokens))):
            if form and form not in forms:
                forms.append(form)
    return _LabelSide(
        names=tuple([*_split_names(artist), *featured_names, *(name for name in remixers if name)]),
        artist_words=frozenset(_label_tokens(artist)),
        main=frozenset(_name_identity(name) for name in _split_names(main_artist)),
        featured=frozenset(_name_identity(name) for name in featured_names),
        titles=tuple(forms),
    )


def _title_slip(x: str, y: str, exact_words: int) -> bool:
    """Whether two different title words are one word with a letter dropped or added.

    Only in a long word ("Temors" / "Tremors", "Emotion" / "Emotions"), or — the 1b-ii trailing
    letter ("Kno" / "Know") — in a title whose other words (at least two) all agree.  A
    substitution is never a slip: "Angel" / "Anger", "Falling" / "Calling" are different titles,
    and "Run" / "Runs" with nothing else to go on is too.
    """

    if abs(len(x) - len(y)) != 1 or not _within_one_edit(x, y):
        return False
    if min(len(x), len(y)) >= _SLIP_MIN_CHARS:
        return True
    return exact_words >= 2 and _near_spelling(x, y)


def _titles_equivalent(hint: tuple[str, ...], audio: tuple[str, ...]) -> bool:
    """The same title: the same words in the same order, give or take one slip or one join."""

    left = _fold_joined(list(hint), frozenset(audio))
    right = _fold_joined(list(audio), frozenset(hint))
    if left == right:
        return bool(left)
    if len(left) != len(right):
        return False
    differing = [(x, y) for x, y in zip(left, right, strict=True) if x != y]
    if len(differing) != 1:
        return False
    return _title_slip(*differing[0], exact_words=len(left) - 1)


def _names_a_credit(hint_artist_words: frozenset[str], audio: _LabelSide) -> bool:
    """The hint's artist field holds one WHOLE credited name of the audio label — every word of it
    that identifies anybody ("Alice" for "DJ Alice"; "DJ" alone identifies nobody)."""

    for name in audio.names:
        folded = _fold_joined(list(name), hint_artist_words)
        distinctive = [word for word in folded if word not in _COMMON_NAME_WORDS] or folded
        if all(word in hint_artist_words for word in distinctive):
            return True
    return False


def _fields_corroborate(hint_artist: str, hint_title: str, audio: _LabelSide) -> bool:
    """ONE orientation: this hint field is the artist, that one the title."""

    if is_placeholder(hint_artist) or is_placeholder(hint_title):
        return False
    hint = _read_label(hint_artist, hint_title)
    if hint.featured and audio.featured and not hint.featured & audio.featured:
        return False  # "… (feat. Somebody Else)" is another work (1b-ii)
    audio_name_words = frozenset(word for name in audio.names for word in name)
    hint_artist_words = frozenset(_fold_joined(_label_tokens(hint_artist), audio_name_words))
    if not _names_a_credit(hint_artist_words, audio):
        return False
    return any(_titles_equivalent(mine, theirs) for mine in hint.titles for theirs in audio.titles)


def hint_label_corroborates(
    hint_artist: str | None,
    hint_title: str | None,
    audio_artist: str | None,
    audio_title: str | None,
) -> bool:
    """Whether a crowd label and a recogniser label name the same work — FIELD BY FIELD.

    A casual answer is as often "Title - Artist" as "Artist - Title", so both orientations are
    tried; in the one that is accepted, the field read as the TITLE must be the recogniser's title
    (the same words in the same order after the clean-up both sides get: case, punctuation,
    accents, dotted initials, version brackets and trailing version words, featuring credits, a
    release note, two words typed as one, and at most one dropped or added letter in a long word)
    and the field read as the ARTIST must hold one whole credited name of the recogniser's label
    (artist, featured guest or remixer) — a name, not a common word such as "DJ".  The words of
    the two fields are never pooled: a title word cannot stand in for an artist and the other way
    round, a longer or shorter title is another title, and a placeholder ("ID", "TBC",
    "unknown", "unreleased") names nothing.  The listener's hedging ("i think …") may be taken
    off the two ends of a field; the answer is read once as typed and once without it, because
    "It's Yours" is a title.  An answer typed with a bare hyphen and no artist ("juice-mall
    grab") is split at that hyphen and read the same way.

    The WORK is what is matched, never the version: hints do not vote for the version tier.
    """

    if is_placeholder(audio_artist) or is_placeholder(audio_title):
        return False
    if not hint_artist and hint_title:
        pair = _BARE_HYPHEN_PAIR.match(normalise_text(hint_title))
        if pair is None:
            return False
        hint_artist, hint_title = pair.group(1), pair.group(2)
    audio = _read_label(normalise_text(audio_artist), normalise_text(audio_title))
    for hedges in (True, False):
        first = _hint_field(hint_artist, hedges=hedges)
        second = _hint_field(hint_title, hedges=hedges)
        if _fields_corroborate(first, second, audio) or _fields_corroborate(second, first, audio):
            return True
    return False


# --- fusion:5 — the tolerant half of the one identity rule ------------------------------------
#
# Real tracklists name a track correctly and still fail the rule above: words joined or split
# ("4raws" / "4 Raws"), letters typed apart ("t e s t p r e s s"), a phonetic respelling ("rock
# the house" / "Rok da House"), an inflected word ("Feel Emotion" / "Feeling Emotions"), a
# descriptor kept on one side only ("Long season MG Edit" / "Long Season (MG Edit)"), a bracket
# offering alternative titles ("ID (LET ME SEE U / ROK DA HOUSE!)"), one dropped letter in a name
# ("Tomlison"), a comment's lead-in ("FULL TRACK LIST: - …"), or initials ("MG" for Mall Grab).
# Each is one named TOLERANCE.  Tolerance is where wrong tracks get in, so each is guarded:
#
# * the other field must agree EXACTLY — a tolerant title needs the artist, a tolerant artist the
#   title (the both-parts rule is never relaxed on both sides at once);
# * initials resolve only to a name SOLID in this mix (credited on two or more recognised works),
#   and only when no other solid name has the same initials;
# * a tolerant match is used only when it is UNIQUE — the caller (identity fusion, the corpus
#   scorer) refuses one that fits two different works;
# * titles are compared whole: two titles that merely share words never match.
#
# The established rule (:func:`hint_label_corroborates`) is tried first and is unchanged; a
# tolerance is only consulted where it found nothing.  The corpus scorer calls the same
# :func:`same_work_labels`, so the score measures what the tool does.

#: Letters typed apart: "t e s t p r e s s" is "testpress" (three or more single characters).
_SPACED_LETTERS = re.compile(r"(?<![a-z0-9])(?:[a-z0-9] ){2,}[a-z0-9](?![a-z0-9])")
#: A trailing "<word> Edit" outside brackets is the bracketed "(<word> Edit)": a descriptor.
_DESCRIPTOR_VERSION_WORDS = frozenset(
    {"edit", "remix", "rework", "bootleg", "refix", "flip", "dub"}
)
#: Words of a descriptor that describe the cut, never who made it ("Intro Edit", "Club Dub").
_DESCRIPTOR_NOT_NAMES = frozenset(
    {
        "intro", "outro", "radio", "club", "extended", "original", "vocal", "instrumental",
        "short", "long", "full", "main", "alt", "alternative", "vip", "dub", "mix", "remix",
        "edit", "special", "new", "old", "my", "the", "a", "an", "live",
    }
)  # fmt: skip
#: Phonetic spellings of one word ("da" for "the", "u" for "you").
_PHONETIC_ALIASES = {
    "da": "the", "tha": "the", "teh": "the",
    "u": "you", "ya": "you", "yu": "you",
    "ur": "your", "yor": "your",
    "n": "and",
    "luv": "love",
    "nite": "night",
    "tonite": "tonight",
}  # fmt: skip
#: A stem is only trusted when this much of the word is left: "feeling" / "feel", never "is" / "i".
_STEM_MIN_CHARS = 3
_INITIALS_MIN, _INITIALS_MAX = 2, 4
#: A changed word (stem, phonetic) is only tolerated in a title of at least this many words, and a
#: misspelt name only beside one (fix pass 1: "Dream" / "Dreams", "Nite" / "Night").
_TOLERANT_TITLE_MIN_WORDS = 2

TOLERANCES = (
    "lead_in",
    "spaced",
    "joined",
    "descriptor",
    "alt_title",
    "stem",
    "phonetic",
    "artist_slip",
    "initials",
)


@dataclass(frozen=True)
class LabelMatch:
    """How two labels were found to name one work: ``tolerances`` is empty for the established
    rule, otherwise the named tolerances that one accepted reading needed."""

    tolerances: tuple[str, ...] = ()

    @property
    def tolerant(self) -> bool:
        return bool(self.tolerances)


def _unspaced(text: str) -> str:
    return _SPACED_LETTERS.sub(lambda match: match.group(0).replace(" ", ""), text)


def _descriptor_split(title: str) -> tuple[str, tuple[tuple[str, ...], ...]] | None:
    """``"long season mg edit"`` -> (``"long season"``, ``("mg",)``): a trailing, unbracketed
    ``<word> <edit|remix|dub|…>`` read as the bracket it stands for.  The core keeps at least two
    words, so "Tokyo Dub" or "Home T Dub" are never cut down to a single word."""

    tokens = _label_tokens(title)
    if len(tokens) < 4 or tokens[-1] not in _DESCRIPTOR_VERSION_WORDS:
        return None
    word = tokens[-2]
    if word in _VERSION_WORDS and word not in _DESCRIPTOR_NOT_NAMES:
        return None
    core = tokens[:-2]
    while len(core) > 2 and core[-1] in _VERSION_WORDS:
        core = core[:-1]
    if len(core) < 2:
        return None
    names = () if word in _DESCRIPTOR_NOT_NAMES or word.isdigit() else ((word,),)
    return " ".join(core), names


@dataclass(frozen=True)
class _TolerantSide:
    """One label as the tolerant reading sees it: the established reading of the unspaced text
    plus the extra title forms and names each tolerance may use."""

    base: _LabelSide
    #: title forms the descriptor or an alternative-title bracket adds, with their tolerance
    extra_titles: tuple[tuple[tuple[str, ...], str], ...]
    #: credited names a trailing descriptor adds ("mg" of "… MG Edit")
    descriptor_names: tuple[tuple[str, ...], ...]
    spaced: bool


def _read_tolerant(artist: str, title: str) -> _TolerantSide:
    unspaced_artist, unspaced_title = _unspaced(artist), _unspaced(title)
    spaced = (unspaced_artist, unspaced_title) != (artist, title)
    base = _read_label(unspaced_artist, unspaced_title)
    extra: list[tuple[tuple[str, ...], str]] = []
    names: list[tuple[str, ...]] = []
    core = _FEATURING_BRACKET.sub(" ", _VERSION_BRACKET.sub(" ", unspaced_title))
    split = _descriptor_split(_ANY_BRACKET.sub(" ", core))
    if split is not None:
        extra.append((tuple(_label_tokens(split[0])), "descriptor"))
        names.extend(split[1])
    for match in _ANY_BRACKET.finditer(core):
        parts = [part for part in match.group(1).split("/") if part.strip()]
        if len(parts) < 2:
            continue
        for part in parts:
            tokens = tuple(_without_trailing_version(_label_tokens(part)))
            if tokens and not is_placeholder(part):
                extra.append((tokens, "alt_title"))
    return _TolerantSide(
        base=base,
        extra_titles=tuple(dict.fromkeys(extra)),
        descriptor_names=tuple(dict.fromkeys(names)),
        spaced=spaced,
    )


def _stem(word: str) -> str:
    for suffix in ("ings", "ing", "es", "s", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= _STEM_MIN_CHARS:
            return word[: -len(suffix)]
    return word


def _phonetic(word: str) -> str:
    word = _PHONETIC_ALIASES.get(word, word)
    word = word.replace("ck", "k").replace("ph", "f")
    return word[:-1] + "s" if word.endswith("z") and len(word) > 2 else word


def _title_tolerance(left: tuple[str, ...], right: tuple[str, ...]) -> str | None:
    """``None`` when two title forms are not one title; ``"exact"`` when the established rule
    already equates them; else the one tolerance (joined, stem, phonetic) that does.  Titles are
    compared whole and in order: the same number of words, each pair one word."""

    if not left or not right:
        return None
    if _titles_equivalent(left, right):
        return "exact"
    joined_left = _fold_joined(list(left), frozenset(right))
    joined_right = _fold_joined(list(right), frozenset(left))
    if joined_left == joined_right:
        return "joined"
    if len(left) != len(right) or len(left) < _TOLERANT_TITLE_MIN_WORDS:
        # A ONE-word title carries too little to tolerate a changed word: "Dream" / "Dreams" and
        # "Nite" / "Night" are as likely two tracks as one.  Only an exact, joined or spaced-letter
        # spelling of a one-word title counts (fix pass 1).
        return None
    kinds: set[str] = set()
    for x, y in zip(left, right, strict=True):
        if x == y:
            continue
        if _stem(x) == _stem(y):
            kinds.add("stem")
        elif _phonetic(x) == _phonetic(y) or _stem(_phonetic(x)) == _stem(_phonetic(y)):
            kinds.add("phonetic")
        else:
            return None
    if not kinds:
        return "exact"
    return "phonetic" if "phonetic" in kinds else "stem"


def _title_readings(hint: _TolerantSide, audio: _TolerantSide) -> list[tuple[str, int]]:
    """Every way the two titles agree: ``(kind, words)`` — ``"exact"`` or the tolerance used, and
    how many words the shorter matched title form has."""

    forms_left = [(form, None) for form in hint.base.titles] + list(hint.extra_titles)
    forms_right = [(form, None) for form in audio.base.titles] + list(audio.extra_titles)
    found: list[tuple[str, int]] = []
    for left, left_kind in forms_left:
        for right, right_kind in forms_right:
            kind = _title_tolerance(left, right)
            if kind is None:
                continue
            via = [item for item in (left_kind, right_kind) if item]
            words = min(len(left), len(right))
            if kind == "exact":
                kind = via[0] if via else "exact"
            found.append((kind, words))
    return found


def _best_title(hint: _TolerantSide, audio: _TolerantSide) -> str | None:
    """The least tolerant way the two titles agree, or ``None``."""

    order = {"exact": -1, **{name: index for index, name in enumerate(TOLERANCES)}}
    found = [kind for kind, _ in _title_readings(hint, audio)]
    return min(found, key=order.__getitem__) if found else None


SolidName = tuple[str, ...]


def _distinctive(name: tuple[str, ...]) -> tuple[str, ...]:
    """A credited name's identifying words, in order ("DJ Alice" is ``("alice",)``)."""

    return tuple(word for word in name if word not in _COMMON_NAME_WORDS) or name


def _initials(name: SolidName) -> str:
    """ "mg" for Mall Grab; a one-word name has no initials."""

    return "".join(word[0] for word in name) if len(name) >= 2 else ""


def _as_initials(name: tuple[str, ...]) -> str | None:
    """The name when it is written as initials: one short all-letter word ("MG", "CRTB")."""

    if len(name) == 1 and name[0].isalpha() and _INITIALS_MIN <= len(name[0]) <= _INITIALS_MAX:
        return name[0]
    return None


def _initials_owner(initials: str, solid: frozenset[SolidName]) -> SolidName | None:
    """The ONE solid name these initials stand for; ``None`` for none, or for two."""

    owners = sorted(name for name in solid if _initials(name) == initials)
    return owners[0] if len(owners) == 1 else None


def _credited_names(artist: str | None, title: str | None) -> list[tuple[str, ...]]:
    """The artist and featured names a recogniser label credits (not its remixers)."""

    artist_text, title_text = normalise_text(artist), normalise_text(title)
    featured = [match.group(0) for match in _FEATURING_BRACKET.finditer(title_text)]
    tail = _FEATURING_TAIL.search(f" {artist_text}")
    if tail is not None:
        featured.append(tail.group(1))
        artist_text = f" {artist_text}"[: tail.start()]
    return [*_split_names(artist_text), *(name for text in featured for name in _split_names(text))]


def solid_names(
    recognised_labels: Iterable[Iterable[tuple[str | None, str | None]]],
) -> frozenset[SolidName]:
    """The names SOLID in a mix: credited (as the artist or a featured guest) on two or more
    different recognised works.  ``recognised_labels`` holds, per recognised work, the recogniser's
    labels of it.  A DJ playing their own music is solid; a famous track "heard" once is not, and
    nothing a comment says can make a name solid."""

    works_by_name: dict[SolidName, set[int]] = {}
    for index, labels in enumerate(recognised_labels):
        for artist, title in labels:
            for name in _credited_names(artist, title):
                works_by_name.setdefault(_distinctive(name), set()).add(index)
    return frozenset(name for name, works in works_by_name.items() if len(works) >= 2)


def _artist_tolerance(
    hint_artist_text: str, audio: _TolerantSide, solid: frozenset[SolidName]
) -> str | None:
    """``"exact"`` when the hint's artist field holds one whole credited name of the audio label
    (the established reading, a descriptor's name included); else the one tolerance that finds
    one — joined, artist_slip, initials — else ``None``."""

    names = [*audio.base.names, *audio.descriptor_names]
    tokens = _label_tokens(hint_artist_text)
    words = frozenset(tokens)
    if any(all(word in words for word in _distinctive(name)) for name in names):
        return "exact"
    folded = frozenset(_fold_joined(tokens, frozenset(word for name in names for word in name)))
    for name in names:
        if all(word in folded for word in _distinctive(tuple(_fold_joined(list(name), folded)))):
            return "joined"
    for name in names:
        distinctive = _distinctive(name)
        missing = [word for word in distinctive if word not in words]
        if (
            len(distinctive) >= 2
            and len(missing) == 1
            and any(
                abs(len(missing[0]) - len(word)) == 1
                and min(len(missing[0]), len(word)) >= _SLIP_MIN_CHARS
                and _within_one_edit(missing[0], word)
                for word in words - set(distinctive)
            )
        ):
            return "artist_slip"
    # Initials, either way round — "MG" typed for a solid "Mall Grab" on the label, or a solid
    # "Mall Grab" typed for an "MG" the label credits — only ever to a SOLID name, and only when
    # it is the one solid name with those initials.
    hint_names = _split_names(hint_artist_text)
    if len(hint_names) == 1 and (initials := _as_initials(hint_names[0])):
        owner = _initials_owner(initials, solid)
        if owner is not None and any(_distinctive(name) == owner for name in names):
            return "initials"
    for name in names:
        if (initials := _as_initials(name)) is None:
            continue
        owner = _initials_owner(initials, solid)
        if owner is not None and any(_distinctive(item) == owner for item in hint_names):
            return "initials"
    return None


def _tolerant_orientation(
    hint_artist: str, hint_title: str, audio: _TolerantSide, solid: frozenset[SolidName]
) -> tuple[str, ...] | None:
    if is_placeholder(hint_artist) or is_placeholder(hint_title):
        return None
    hint = _read_tolerant(hint_artist, hint_title)
    if hint.base.featured and audio.base.featured and not hint.base.featured & audio.base.featured:
        return None  # "… (feat. Somebody Else)" is another work, tolerance or not
    artist = _artist_tolerance(_unspaced(hint_artist), audio, solid)
    if artist is None:
        return None
    readings = _title_readings(hint, audio)
    if artist == "artist_slip":
        # A misspelt name is only trusted against a title of two or more words, matched exactly: a
        # one-word title plus a nearly-right name is two weak signals, not one strong one.
        readings = [item for item in readings if item[1] >= _TOLERANT_TITLE_MIN_WORDS]
    order = {"exact": -1, **{name: index for index, name in enumerate(TOLERANCES)}}
    title = min((kind for kind, _ in readings), key=order.__getitem__, default=None)
    if title is None:
        return None
    if artist != "exact" and title != "exact":
        return None  # both parts loose: never
    used = [kind for kind in (artist, title) if kind != "exact"]
    if (hint.spaced or audio.spaced) and "spaced" not in used:
        used.insert(0, "spaced")
    return tuple(used)


def label_match(
    hint_artist: str | None,
    hint_title: str | None,
    audio_artist: str | None,
    audio_title: str | None,
    *,
    solid: frozenset[SolidName] = frozenset(),
) -> LabelMatch | None:
    """THE identity rule: whether a (crowd or truth) label names the work of a recogniser label.

    The established rule (:func:`hint_label_corroborates`) first; failing that, the tolerances of
    ``TOLERANCES``, each guarded as the section above says.  Returns ``None`` for no match, a
    :class:`LabelMatch` saying which tolerances were needed otherwise.  Uniqueness within the mix
    is the caller's to enforce (it knows the other works).
    """

    if hint_label_corroborates(hint_artist, hint_title, audio_artist, audio_title):
        return LabelMatch()
    (hint_artist, hint_title), hint_lead = _without_lead_in(hint_artist, hint_title)
    (audio_artist, audio_title), audio_lead = _without_lead_in(audio_artist, audio_title)
    lead_in: tuple[str, ...] = ("lead_in",) if hint_lead or audio_lead else ()
    if lead_in and hint_label_corroborates(hint_artist, hint_title, audio_artist, audio_title):
        return LabelMatch(lead_in)
    if is_placeholder(audio_artist) or is_placeholder(audio_title):
        return None
    if not hint_artist and hint_title:
        pair = _BARE_HYPHEN_PAIR.match(normalise_text(hint_title))
        if pair is None:
            return None
        hint_artist, hint_title = pair.group(1), pair.group(2)
    if not hint_artist or not hint_title:
        return None
    audio = _read_tolerant(normalise_text(audio_artist), normalise_text(audio_title))
    found: list[tuple[str, ...]] = []
    for hedges in (True, False):
        first = _hint_field(hint_artist, hedges=hedges)
        second = _hint_field(hint_title, hedges=hedges)
        for artist, title in ((first, second), (second, first)):
            used = _tolerant_orientation(artist, title, audio, solid)
            if used is not None:
                found.append(used)
    if not found:
        return None
    return LabelMatch(lead_in + min(found, key=lambda item: (len(item), item)))


def _without_lead_in(
    artist: str | None, title: str | None
) -> tuple[tuple[str | None, str | None], bool]:
    """ "FULL TRACK LIST: - Fishmans - Long Season" is "Fishmans - Long Season": a field that ends
    in a colon is a comment's lead-in, never a name, and the label is in the other field."""

    if artist and title and artist.strip().endswith(":") and " - " in title:
        left, right = title.split(" - ", 1)
        return (left, right), True
    return (artist, title), False


def same_work_labels(
    left: tuple[str | None, str | None],
    right: tuple[str | None, str | None],
    *,
    solid: frozenset[SolidName] = frozenset(),
) -> LabelMatch | None:
    """:func:`label_match` read both ways round (neither label is privileged): the rule for two
    crowd labels, or a listed label against a truth row."""

    matches = [
        match
        for match in (
            label_match(*left, *right, solid=solid),
            label_match(*right, *left, solid=solid),
        )
        if match is not None
    ]
    if not matches:
        return None
    return min(matches, key=lambda item: (len(item.tolerances), item.tolerances))


AudioWorkKey = tuple[frozenset[frozenset[str]], tuple[str, ...]]


def audio_work_key(artist: str | None, title: str | None) -> AudioWorkKey:
    """The same artist names and the same title once version brackets and featuring credits are
    set aside: what "Party Drumz" and "Party Drumz (Club Mix)" share."""

    side = _read_label(normalise_text(artist), normalise_text(title))
    return side.main, min(side.titles, key=len, default=())


def audio_featured(artist: str | None, title: str | None) -> frozenset[frozenset[str]]:
    """The featured guests of a recogniser label, each as a whole name."""

    return _read_label(normalise_text(artist), normalise_text(title)).featured


def build_identity_graph(
    media_key: str,
    observations: list[ObservationRecord] | tuple[ObservationRecord, ...],
    *,
    hints: list[HintRecord] | tuple[HintRecord, ...] = (),
    extra_assertions: list[IdentityAssertion] | tuple[IdentityAssertion, ...] = (),
    prior_recording_components: tuple[tuple[str, ...], ...] = (),
) -> IdentityBuildResult:
    """Resolve final matches into deterministic work and recording components.

    The Stage 0 ``merge_recording_identities`` helper remains the sole implementation of
    corroboration, privileged-source handling, conflict veto, and late-conflict contesting.
    """

    final_matches = sorted(
        (item for item in observations if item.is_final and item.status == "match"),
        key=lambda item: item.id,
    )
    node_labels: dict[str, str] = {}
    assertion_by_id: dict[str, IdentityAssertion] = {item.id: item for item in extra_assertions}
    observation_nodes: dict[str, list[str]] = {}
    observation_text: dict[str, str] = {}
    recording_node_sources: dict[str, set[str]] = {}
    hint_text: dict[str, str] = {}
    audio_fields: dict[str, tuple[str | None, str | None]] = {}
    hint_fields: dict[str, tuple[str | None, str | None]] = {}
    # Placeholder lines name no work, but their exact parsed wording can still be useful when
    # presentation labels a row whose identity came from independent evidence.  Keep that wording
    # outside the identity graph: no node, assertion, work, candidate or hint mapping can be made
    # from this collection.
    display_only_labels = sorted(
        {
            f"{hint.artist} - {hint.title}"
            for hint in hints
            if hint.mirror_status == "verified"
            and hint.flags.id_unknown
            and hint.artist
            and hint.title
        }
    )

    for observation in final_matches:
        label = display_label(observation)
        key = work_text_key(observation)
        text_node = f"text:{key}" if key is not None else None
        if text_node is not None:
            node_labels[text_node] = label
            observation_text[observation.id] = text_node
            audio_fields.setdefault(
                text_node, (observation.raw_label.artist, observation.raw_label.title)
            )
        provider_nodes = []
        for ns, value in _provider_nodes(observation):
            node_id = f"{ns}:{value}"
            node_labels.setdefault(node_id, label)
            provider_nodes.append(node_id)
            if ns in RECORDING_NAMESPACES:
                recording_node_sources.setdefault(node_id, set()).add(
                    f"provider:{observation.provider}"
                )
        observation_nodes[observation.id] = provider_nodes

        source_kind = (
            "aligned_held_reference"
            if observation.provider == "local_fixture"
            else "provider_observation"
        )
        independent = f"provider:{observation.provider}"
        if text_node is not None:
            for provider_node in provider_nodes:
                item = _assertion(
                    media_key,
                    a=text_node,
                    b=provider_node,
                    relation="same_work",
                    source_kind=source_kind,
                    source_record_id=observation.id,
                    independent_of=independent,
                    confidence=10_000 if observation.provider == "local_fixture" else 8_000,
                )
                assertion_by_id[item.id] = item
        recording_nodes = [
            node for node in provider_nodes if node.split(":", 1)[0] in RECORDING_NAMESPACES
        ]
        for index, left in enumerate(recording_nodes):
            for right in recording_nodes[index + 1 :]:
                item = _assertion(
                    media_key,
                    a=left,
                    b=right,
                    relation="same_recording",
                    source_kind=source_kind,
                    source_record_id=observation.id,
                    independent_of=independent,
                    confidence=10_000 if observation.provider == "local_fixture" else 9_000,
                )
                assertion_by_id[item.id] = item

    # Casual "ID" answers in comments are as often written "Title - Artist" as the provider's
    # "Artist - Title".  That leaves the answer's text node (e.g. ``text:senses|bakey``) in an
    # identity component of its own, unable to corroborate the audio match
    # (``text:bakey|senses``).  Join a hint to an audio text node whenever
    # :func:`hint_label_corroborates` says they name one work, FIELD BY FIELD in either order.
    #
    # A hint joins ONE recognised work or none.  "Recognised work" is what existed before any hint
    # was read: the provider-linked components, with two labels that differ only by a version
    # bracket or a featuring credit (:func:`audio_work_key`) counted as one.  A hint whose label
    # fits two of them is ambiguous: it backs nothing and gets no identity of its own, so it can
    # neither promote the wrong one nor list a third row for a track already heard.
    audio_text_nodes = {node for node in observation_text.values() if node}
    recognised = _UnionFind(list(node_labels))
    for assertion in assertion_by_id.values():
        if (
            assertion.relation in {"same_work", "same_recording"}
            and assertion.a in recognised.parent
            and assertion.b in recognised.parent
        ):
            recognised.union(assertion.a, assertion.b)
    # Same names and title: one work — unless the labels credit DIFFERENT featured guests, which
    # stay different works (1b-ii).  A label with no featured credit sides with the single
    # credited version there is; with two or more, only equal credits are joined, so an
    # uncredited label can never bridge "… (feat. Bob Jones)" and "… (feat. Bob Smith)".
    by_key: dict[AudioWorkKey, list[str]] = {}
    for node in sorted(audio_text_nodes):
        by_key.setdefault(audio_work_key(*audio_fields[node]), []).append(node)
    for nodes in by_key.values():
        featured_of = {node: audio_featured(*audio_fields[node]) for node in nodes}
        one_credit = len({credit for credit in featured_of.values() if credit}) <= 1
        first_with_credit: dict[frozenset[frozenset[str]], str] = {}
        for node in nodes:
            credit = frozenset() if one_credit else featured_of[node]
            recognised.union(node, first_with_credit.setdefault(credit, node))

    named_by_label: dict[tuple[str | None, str], list[str]] = {}

    def _named(artist: str | None, title: str) -> list[str]:
        if (artist, title) not in named_by_label:
            named_by_label[(artist, title)] = [
                node
                for node in sorted(audio_text_nodes)
                if hint_label_corroborates(artist, title, *audio_fields[node])
            ]
        return named_by_label[(artist, title)]

    # What initials may stand for: the names credited on two or more recognised works.
    recognised_groups: dict[str, list[tuple[str | None, str | None]]] = {}
    for node in sorted(audio_text_nodes):
        recognised_groups.setdefault(recognised.find(node), []).append(audio_fields[node])
    solid = solid_names(recognised_groups[root] for root in sorted(recognised_groups))
    tolerant_links: list[tuple[str, str, str, tuple[str, ...]]] = []
    tolerant_by_label: dict[tuple[str, str], tuple[list[str], tuple[str, ...], bool]] = {}

    def _tolerantly_named(artist: str, title: str) -> tuple[list[str], tuple[str, ...], bool]:
        """Only where the established rule named nothing: the recognised labels a TOLERANCE
        names, kept only when they are all one recognised work (unique within the mix); the flag
        says the tolerance fitted two or more recognised works (ambiguous)."""

        if (artist, title) not in tolerant_by_label:
            found = {
                node: match
                for node in sorted(audio_text_nodes)
                if (match := label_match(artist, title, *audio_fields[node], solid=solid))
                is not None
            }
            works = len({recognised.find(node) for node in found})
            unique = works == 1
            tolerances = min(
                (match.tolerances for match in found.values()),
                key=lambda item: (len(item), item),
                default=(),
            )
            tolerant_by_label[(artist, title)] = (
                sorted(found) if unique else [],
                tolerances,
                works > 1,
            )
        return tolerant_by_label[(artist, title)]

    audio_matched_nodes: set[str] = set()
    for hint in sorted(hints, key=lambda item: item.id):
        if hint.mirror_status != "verified" or hint.flags.id_unknown or not hint.title:
            continue
        if not hint.artist:
            # An answer typed with a bare hyphen ("juice-mall grab") reaches here as a title with
            # no artist.  It names both parts, so it may JOIN the one audio work it names — and
            # only that: it gets no node of its own, so it can never become a track.
            if hint.kind in {"answer", "correction"} and _BARE_HYPHEN_PAIR.match(
                normalise_text(hint.title)
            ):
                named = _named(None, hint.title)
                if len({recognised.find(node) for node in named}) == 1:
                    hint_text[hint.id] = named[0]
            continue
        artist = normalise_text(hint.artist)
        title = normalise_text(hint.title)
        if not artist or not title or is_placeholder(hint.artist) or is_placeholder(hint.title):
            continue  # "Artist - ID", "TBC - TBC", "Artist - unreleased" name no work
        text_node = f"text:{artist}|{title}"
        # EVERY ordinary hint is read against every recognised label first — also one whose text
        # is a recognised label letter for letter: "Artist - Song" is exactly the solo release
        # AND fits "Artist & Guest - Song", so it says which of the two no better than any other.
        named = _named(hint.artist, hint.title)
        if len({recognised.find(node) for node in named}) > 1:
            continue  # ambiguous: no identity, no assertion — backs nothing, contradicts nothing
        confidence = 7_000
        if not named:
            named, tolerances, ambiguous = _tolerantly_named(hint.artist, hint.title)
            if ambiguous:
                continue  # a tolerance fits two recognised works: as the veto above, no identity
            if named:
                confidence = 6_500  # a tolerance joined it: a notch below the established rule
                tolerant_links.append(
                    (
                        "hint_to_audio",
                        f"{hint.artist} - {hint.title}",
                        display_label_of(audio_fields[named[0]]),
                        tolerances,
                    )
                )
        node_labels.setdefault(text_node, f"{hint.artist} - {hint.title}")
        hint_text[hint.id] = text_node
        hint_fields.setdefault(text_node, (hint.artist, hint.title))
        for audio_node in named:
            if audio_node == text_node:
                continue
            audio_matched_nodes.add(text_node)
            item = _assertion(
                media_key,
                a=text_node,
                b=audio_node,
                relation="same_work",
                source_kind="hint_text_match",
                source_record_id=hint.id,
                independent_of="hint:comment_answer",
                confidence=confidence,
            )
            assertion_by_id[item.id] = item

    # Two crowd IDs of the SAME track — "Entasia - Satalite" and "Entasia - Satalite (unreleased)",
    # "MG - 1ofthozedaze" and "1ofthozedaze - Mall Grab" — otherwise land in separate works and the
    # track is listed twice.  They are joined by THE rule (:func:`same_work_labels`, read both ways
    # round): the established reading first, then a tolerance — and a tolerant link only when it is
    # unique, i.e. every crowd work it would join this label to is one work.  Titles are compared
    # whole, so two works whose titles merely share words are never merged.  Audio-matched hints
    # are excluded — they already joined the audio work above — so nothing here can carry a hint
    # INTO a recognised work through a better-spelled neighbour: only the test above does that.
    unmatched = sorted(
        {
            node
            for node in hint_text.values()
            if node not in audio_text_nodes and node not in audio_matched_nodes
        }
    )
    source_of = {node: hint_id for hint_id, node in sorted(hint_text.items(), reverse=True)}
    crowd = _UnionFind(unmatched)
    tolerant_pairs: dict[str, list[tuple[str, tuple[str, ...]]]] = {node: [] for node in unmatched}
    for index, node_a in enumerate(unmatched):
        for node_b in unmatched[index + 1 :]:
            match = same_work_labels(hint_fields[node_a], hint_fields[node_b], solid=solid)
            if match is None:
                continue
            if match.tolerant:
                tolerant_pairs[node_a].append((node_b, match.tolerances))
                tolerant_pairs[node_b].append((node_a, match.tolerances))
                continue
            crowd.union(node_a, node_b)
            item = _assertion(
                media_key,
                a=node_a,
                b=node_b,
                relation="same_work",
                source_kind="hint_text_match",
                source_record_id=source_of[node_a],
                independent_of="hint:comment_answer",
                confidence=6_000,
            )
            assertion_by_id[item.id] = item

    # A tolerant link counts only when it resolves to exactly ONE work, on both sides, and it never
    # chains: the components the established rule built are fixed first; component A is joined to
    # component B only when every tolerant partner of A's labels lies in B and every tolerant
    # partner of B's labels lies in A.  "Alex Anderson" fitting both "Alex Andersson" and "Alex
    # Anderon" (which do not fit each other) is therefore joined to neither (fix pass 1).
    partner_components: dict[str, set[str]] = {}
    for node in unmatched:
        own = crowd.find(node)
        partner_components.setdefault(own, set()).update(
            crowd.find(other) for other, _ in tolerant_pairs[node] if crowd.find(other) != own
        )

    def _mutual(node_a: str, node_b: str) -> bool:
        left, right = crowd.find(node_a), crowd.find(node_b)
        return (
            left != right
            and partner_components.get(left) == {right}
            and partner_components.get(right) == {left}
        )

    for node_a in unmatched:
        for node_b, tolerances in tolerant_pairs[node_a]:
            if node_b < node_a or not _mutual(node_a, node_b):
                continue
            item = _assertion(
                media_key,
                a=node_a,
                b=node_b,
                relation="same_work",
                source_kind="hint_text_match",
                source_record_id=source_of[node_a],
                independent_of="hint:comment_answer",
                confidence=5_500,
            )
            assertion_by_id[item.id] = item
            tolerant_links.append(
                ("hint_to_hint", node_labels[node_a], node_labels[node_b], tolerances)
            )

    assertions = sorted(assertion_by_id.values(), key=lambda item: item.id)
    # Reuse the Stage 0 helper; this call is intentionally not duplicated below.
    merged = merge_recording_identities(
        {node: node.split(":", 1)[0] for node in node_labels},
        [item.model_dump(mode="json") for item in assertions],
        prior_components=prior_recording_components,
    )
    recording_component_by_node = {
        node: component for component in merged.components for node in component
    }

    work_union = _UnionFind(list(node_labels))
    for assertion in assertions:
        if assertion.relation in {"same_work", "same_recording"}:
            work_union.union(assertion.a, assertion.b)
    work_groups: dict[str, list[str]] = {}
    for node in node_labels:
        work_groups.setdefault(work_union.find(node), []).append(node)

    works: list[IdentityWork] = []
    work_id_by_node: dict[str, str] = {}
    for members in sorted(sorted(group) for group in work_groups.values()):
        text_keys = [node.removeprefix("text:") for node in members if node.startswith("text:")]
        normalised_key = min(text_keys) if text_keys else min(members)
        work_id = make_id(
            media_key,
            "identity_work",
            compose_natural_key("identity_work", {"normalised_artist_title": normalised_key}),
        )
        works.append(
            IdentityWork(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                work_id=work_id,
                member_nodes=members,
            )
        )
        work_id_by_node.update({node: work_id for node in members})

    provider_linked_text = {
        observation_text[observation_id]
        for observation_id, nodes in observation_nodes.items()
        if nodes and observation_id in observation_text
    }
    candidate_components = [
        component
        for component in merged.components
        if any(node.split(":", 1)[0] in RECORDING_NAMESPACES for node in component)
        or any(node.startswith("text:") and node not in provider_linked_text for node in component)
    ]
    contested_components = {tuple(item) for item in merged.contested}
    conflict_nodes: dict[tuple[str, ...], set[str]] = {
        tuple(component): set() for component in candidate_components
    }
    for assertion in assertions:
        if assertion.relation != "conflicts":
            continue
        for component in candidate_components:
            if assertion.a in component or assertion.b in component:
                conflict_nodes[tuple(component)].update((assertion.a, assertion.b))

    preliminary: list[tuple[tuple[str, ...], str, str, bool, list[str]]] = []
    for component in sorted(candidate_components):
        work_id = work_id_by_node[component[0]]
        canonical_id = make_id(
            media_key,
            "identity_candidate",
            compose_natural_key("identity_candidate", {"member_nodes": list(component)}),
        )
        conflicts = sorted(conflict_nodes[tuple(component)])
        preliminary.append(
            (component, canonical_id, work_id, component in contested_components, conflicts)
        )
    by_work: dict[str, list[str]] = {}
    for _, canonical_id, work_id, _, _ in preliminary:
        by_work.setdefault(work_id, []).append(canonical_id)
    candidates = [
        IdentityCandidate(
            schema_version=SCHEMA_VERSION,
            generated_by=GENERATED_BY,
            canonical_id=canonical_id,
            work_id=work_id,
            member_nodes=list(component),
            alternatives=sorted(item for item in by_work[work_id] if item != canonical_id),
            contested=contested,
            conflicts=conflicts,
        )
        for component, canonical_id, work_id, contested, conflicts in preliminary
    ]
    candidate_by_node = {
        node: candidate.canonical_id for candidate in candidates for node in candidate.member_nodes
    }
    observation_candidates: dict[str, str] = {}
    hint_candidates: dict[str, str] = {}
    hint_work_ids: dict[str, str] = {}
    candidate_labels: dict[str, tuple[str, str]] = {}
    for observation in final_matches:
        provider_nodes = observation_nodes[observation.id]
        preferred = next(
            (node for node in provider_nodes if node.startswith(f"{observation.provider}:")),
            provider_nodes[0] if provider_nodes else observation_text.get(observation.id),
        )
        if preferred is None:
            continue
        component = recording_component_by_node.get(preferred, (preferred,))
        candidate_id = candidate_by_node.get(component[0])
        if candidate_id is None:
            # A provider match with no recording id (e.g. an AudD clip whose only field is a song
            # link) contributes just a text node.  When that title is provider-linked — a Shazam
            # recording shares it — the pure-text component was excluded from the candidate set, so
            # attach the observation to that title's work candidate instead of failing.  This is how
            # a paid clip corroborates (and can promote) a Shazam track it agrees with.
            work_id = work_id_by_node.get(preferred)
            work_candidates = sorted(by_work.get(work_id, [])) if work_id else []
            if not work_candidates:
                continue
            candidate_id = work_candidates[0]
        observation_candidates[observation.id] = candidate_id
        candidate_labels.setdefault(
            candidate_id,
            (
                observation.raw_label.artist or "Unknown artist",
                observation.raw_label.title or "Unknown title",
            ),
        )
    candidates_by_work = {
        work_id: sorted(
            candidate.canonical_id for candidate in candidates if candidate.work_id == work_id
        )
        for work_id in {candidate.work_id for candidate in candidates}
    }
    hint_by_id = {hint.id: hint for hint in hints}
    for hint_id, text_node in sorted(hint_text.items()):
        work_id = work_id_by_node[text_node]
        hint_work_ids[hint_id] = work_id
        work_candidates = candidates_by_work.get(work_id, [])
        if work_candidates:
            hint_candidates[hint_id] = work_candidates[0]
            hint = hint_by_id[hint_id]
            candidate_labels.setdefault(
                work_candidates[0], (hint.artist or "Unknown artist", hint.title or "Unknown title")
            )

    recording_supported = frozenset(
        candidate.canonical_id
        for candidate in candidates
        if candidate_recording_supported(
            contested=candidate.contested,
            member_nodes=candidate.member_nodes,
            recording_node_sources=recording_node_sources,
        )
    )
    record = IdentitiesRecord(
        schema_version=SCHEMA_VERSION,
        generated_by=GENERATED_BY,
        nodes=[
            IdentityNode(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                id=node,
                ns=node.split(":", 1)[0],
                label=node_labels[node],
            )
            for node in sorted(node_labels)
        ],
        assertions=assertions,
        works=sorted(works, key=lambda item: item.work_id),
        candidates=sorted(candidates, key=lambda item: item.canonical_id),
        display_only_labels=display_only_labels,
    )
    return IdentityBuildResult(
        record=record,
        observation_candidates=observation_candidates,
        hint_candidates=hint_candidates,
        hint_work_ids=hint_work_ids,
        candidate_labels=candidate_labels,
        recording_supported=recording_supported,
        tolerant_links=tuple(tolerant_links),
        solid=solid,
    )


def write_identity_graph(
    media_dir: Path,
    generation: int,
    build: IdentityBuildResult,
    *,
    observations_path: Path,
    hints_path: Path | None = None,
) -> Path:
    path = media_dir / "fuse" / f"identities.gen{generation}.json"
    atomic_write_json(path, build.record)
    upstream = {observations_path.relative_to(media_dir).as_posix(): observations_path}
    if hints_path is not None:
        upstream[hints_path.relative_to(media_dir).as_posix()] = hints_path
    write_completion_sidecar(path, upstream)
    return path
