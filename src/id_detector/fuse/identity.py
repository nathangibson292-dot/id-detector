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
                confidence=7_000,
            )
            assertion_by_id[item.id] = item

    def _words(node: str) -> frozenset[str]:
        artist, title = hint_fields[node]
        return frozenset([*_label_tokens(_hint_field(artist)), *_label_tokens(_hint_field(title))])

    # Two crowd IDs of the SAME track — "Entasia - Satalite" and "Entasia - Satalite (unreleased)",
    # "…Pump It" and "…Pump It (Club Royalty 3)" — otherwise land in separate works and the track is
    # listed twice.  Union hint text nodes with each OTHER on the same conservative word-set rule
    # (subset with >=2 words, or a near-spelled long token), so a track named more than once in the
    # comments is one identity.  Audio-matched hints are excluded — they already joined the audio
    # work above — so this looser rule can never carry a hint INTO a recognised work through a
    # better-spelled neighbour: only the field-level test above does that.
    unmatched = sorted(
        (hint_id, node)
        for hint_id, node in hint_text.items()
        if node not in audio_text_nodes and node not in audio_matched_nodes
    )
    for index, (hint_a, node_a) in enumerate(unmatched):
        words_a = _words(node_a)
        for _hint_b, node_b in unmatched[index + 1 :]:
            if node_a == node_b or not _word_sets_corroborate(words_a, _words(node_b)):
                continue
            item = _assertion(
                media_key,
                a=node_a,
                b=node_b,
                relation="same_work",
                source_kind="hint_text_match",
                source_record_id=hint_a,
                independent_of="hint:comment_answer",
                confidence=6_000,
            )
            assertion_by_id[item.id] = item

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
    )
    return IdentityBuildResult(
        record=record,
        observation_candidates=observation_candidates,
        hint_candidates=hint_candidates,
        hint_work_ids=hint_work_ids,
        candidate_labels=candidate_labels,
        recording_supported=recording_supported,
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
