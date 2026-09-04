"""Panako ``local_index_query`` provider: JDK discovery, index store/query, normalisation.

Stage 8 turns the Stage 3 skeleton into a working reference-pool matcher.  Panako is a
self-hosted Java fingerprinter (AGPL); we drive its pinned release jar as a subprocess under the
Windows Job-Object launcher (:mod:`id_detector.process`) and normalise its CSV output into the
plan's :class:`~id_detector.contracts.ObservationRecord` contract, exactly like the file-scanner
path (``transform = null``; ``logical_trial_id = sha1(provider ‖ chunk_index)``; independent
source in the identity graph; no cascading suppression).

Two Windows realities are handled here so the provider is reproducible on this machine:

* **lmdbjava on JDK 16+** needs ``--add-opens java.base/java.nio`` (and ``sun.nio.ch``); without
  it ``java.nio.Buffer.address`` is inaccessible and the store never opens.
* **Panako hardcodes a 1 TiB LMDB map size.**  On Windows LMDB grows the data file to the full
  map size when it opens, which fails with *disk full* on any disk smaller than 1 TiB.  We let
  Panako write its (valid) initial meta pages, then mark ``data.mdb`` sparse and extend it to the
  map size, so the reopen maps an already-large file without consuming disk.  POSIX LMDB grows a
  sparse file natively, so this step is a no-op there.

The Panako query CSV is one ``;``-delimited row per query with the header::

    Index; Total ; Query path;Query start (s);Query stop (s); Match path;Match id;
    Match start (s); Match stop (s); Match score; Time factor (%); Frequency factor(%);
    Seconds with match (%)

``Time factor`` / ``Frequency factor`` are printed as ratios with a literal ``%`` glyph (``1.000``
means "no scale change"); we store them as integer ten-thousandths in ``native``.
"""

from __future__ import annotations

import glob
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from id_detector.contracts import (
    GENERATED_BY,
    SCHEMA_VERSION,
    Anchor,
    ObservationRecord,
    QueryRecord,
    RawLabel,
    compose_natural_key,
    make_id,
    sort_records,
)
from id_detector.fuse.scanners import scanner_logical_trial_id
from id_detector.io import canonical_json_bytes, native_path
from id_detector.process import ProcessResult, ProcessTimeout, run_process
from id_detector.providers.base import ProviderCapability, ProviderUnavailable

PROVIDER = "panako"
PROVIDER_CONFIG_VERSION = "panako-v2.json"
CAPABILITY_NAME = "local_index_query"

# --------------------------------------------------------------------------------------------------
# Pinned Panako release (verified sha256, recorded in the stage report).
# --------------------------------------------------------------------------------------------------
PANAKO_TAG = "joss"
PANAKO_VERSION = "2.1"
PANAKO_JAR_NAME = "Panako-2.1-all.jar"
PANAKO_JAR_SIZE = 6_431_377
PANAKO_JAR_SHA256 = "767cdd2cd0991658c4a25a0b8e887f9a2a38f69ae17781b02fe1652e1a7173d4"
PANAKO_DOWNLOAD_URL = "https://github.com/JorenSix/Panako/releases/download/joss/Panako-2.1-all.jar"

#: Panako's OLAF strategy fingerprints at 16 kHz — the same rate as our decoded PCM.
PANAKO_STRATEGY = "OLAF"
PANAKO_SAMPLE_RATE = 16_000

#: The map size Panako hardcodes for its OLAF LMDB store (``setMapSize(1_099_511_627_776L)``).
PANAKO_LMDB_MAPSIZE = 1_099_511_627_776

#: JDK 16+ module opens that lmdbjava needs to reach ``java.nio.Buffer.address`` reflectively.
JDK_MODULE_OPENS: tuple[str, ...] = (
    "--add-opens",
    "java.base/java.nio=ALL-UNNAMED",
    "--add-opens",
    "java.base/sun.nio.ch=ALL-UNNAMED",
)

# Default install-location globs, newest first is chosen by parsed version.  Adoptium/Temurin,
# Microsoft OpenJDK and Azul Zulu cover the JDKs a Windows owner is likely to have.
_WINDOWS_JDK_GLOBS: tuple[str, ...] = (
    r"C:\Program Files\Eclipse Adoptium\jdk-*\bin\java.exe",
    r"C:\Program Files\Eclipse Foundation\jdk-*\bin\java.exe",
    r"C:\Program Files\Microsoft\jdk-*\bin\java.exe",
    r"C:\Program Files\Zulu\zulu-*\bin\java.exe",
    r"C:\Program Files\Java\jdk-*\bin\java.exe",
    r"C:\Program Files\Amazon Corretto\jdk*\bin\java.exe",
)
_POSIX_JDK_GLOBS: tuple[str, ...] = (
    "/usr/lib/jvm/java-*/bin/java",
    "/usr/lib/jvm/jdk-*/bin/java",
    "/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java",
    "/opt/homebrew/opt/openjdk*/bin/java",
)


class PanakoError(RuntimeError):
    """A recognised Panako protocol/setup failure (jar missing, bad output, timeout)."""


# --------------------------------------------------------------------------------------------------
# JDK discovery
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class JavaResolution:
    path: Path
    source: str  # "JAVA_HOME" | "install" | "PATH"


def _java_executable_name() -> str:
    return "java.exe" if os.name == "nt" else "java"


def _default_install_globs() -> tuple[str, ...]:
    return _WINDOWS_JDK_GLOBS if os.name == "nt" else _POSIX_JDK_GLOBS


def _version_sort_key(path: Path) -> tuple[tuple[int, ...], str]:
    """Sort key that orders JDK install paths newest-first by the digits in the directory name."""

    numbers = tuple(int(part) for part in re.findall(r"\d+", str(path)))
    return (numbers, str(path))


def resolve_java(
    *,
    env: Mapping[str, str] | None = None,
    install_globs: Sequence[str] | None = None,
    which: Callable[[str], str | None] | None = None,
    exists: Callable[[Path], bool] | None = None,
) -> JavaResolution | None:
    """Resolve ``java`` in the plan's fixed order: ``JAVA_HOME`` → install globs → ``PATH``.

    Every dependency is injectable so the discovery order is unit-testable without a second JDK on
    the machine.  ``winget`` updates the registry ``PATH`` after a shell starts, so relying on
    :func:`shutil.which` alone would miss a freshly installed JDK — hence the explicit fallbacks.
    """

    environment = os.environ if env is None else env
    which_func = which if which is not None else _shutil_which
    exists_func = exists if exists is not None else (lambda candidate: candidate.is_file())

    java_home = environment.get("JAVA_HOME")
    if java_home:
        candidate = Path(java_home) / "bin" / _java_executable_name()
        if exists_func(candidate):
            return JavaResolution(candidate, "JAVA_HOME")

    patterns = _default_install_globs() if install_globs is None else tuple(install_globs)
    hits: list[Path] = []
    for pattern in patterns:
        for match in glob.glob(pattern):
            candidate = Path(match)
            if exists_func(candidate):
                hits.append(candidate)
    if hits:
        best = max(hits, key=_version_sort_key)
        return JavaResolution(best, "install")

    on_path = which_func("java")
    if on_path:
        return JavaResolution(Path(on_path), "PATH")
    return None


def _shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


async def java_version_line(java: Path, *, timeout: float = 20) -> str:
    """Return the first line of ``java -version`` (written to stderr)."""

    result = await run_process([str(java), "-version"], timeout=timeout, check=False)
    output = result.stderr + result.stdout
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    return "no version output"


def _parse_version_number(version_line: str) -> str:
    match = re.search(r'"([0-9][0-9._]*)"', version_line)
    return match.group(1) if match else version_line


def doctor_detail() -> tuple[str, str]:
    """Doctor line for Panako's JDK: ``PASS  JDK <version> at <path>`` when a JDK is resolvable."""

    resolution = resolve_java()
    if resolution is None:
        return "WARN", "JDK not found — Panako disabled"
    try:
        import asyncio

        line = asyncio.run(java_version_line(resolution.path))
    except (ProcessTimeout, OSError) as exc:  # preflight must never crash the doctor table
        return "WARN", f"java found at {resolution.path} but did not run: {exc}"
    version = _parse_version_number(line)
    return "PASS", f"JDK {version} at {resolution.path}"


# --------------------------------------------------------------------------------------------------
# Runtime configuration
# --------------------------------------------------------------------------------------------------
def render_config_properties(*, windows: bool | None = None) -> str:
    """Render the ``config.properties`` Panako reads from beside its jar.

    The decoder-pipe environment must be set here (not on the command line): Panako builds the
    TarsosDSP pipe decoder from the config *default* at construction time, before command-line
    ``KEY=VALUE`` overrides are applied, so a command-line ``DECODER_PIPE_ENVIRONMENT`` is ignored.
    On Windows the shell must be ``cmd.exe /C`` (the packaged default ``/bin/bash`` does not exist).
    """

    is_windows = os.name == "nt" if windows is None else windows
    if is_windows:
        environment, environment_arg = "cmd.exe", "/C"
    else:
        environment, environment_arg = "/bin/bash", "-c"
    lines = [
        "# Written by id-detector panako-setup; do not hand-edit.",
        "DECODER=PIPE",
        f"DECODER_PIPE_ENVIRONMENT={environment}",
        f"DECODER_PIPE_ENVIRONMENT_ARG={environment_arg}",
        "OLAF_STORAGE=LMDB",
        f"OLAF_SAMPLE_RATE={PANAKO_SAMPLE_RATE}",
        f"STRATEGY={PANAKO_STRATEGY}",
    ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class PanakoRuntime:
    """Everything needed to launch the pinned Panako jar with a resolved JDK."""

    java: Path
    jar: Path
    java_source: str = "install"

    @classmethod
    def resolve(cls, *, jar: Path, env: Mapping[str, str] | None = None) -> PanakoRuntime:
        if not jar.is_file():
            raise ProviderUnavailable(
                f"Panako jar not found at {jar}; run `id-detector panako-setup` first"
            )
        resolution = resolve_java(env=env)
        if resolution is None:
            raise ProviderUnavailable("JDK not found — Panako disabled")
        return cls(java=resolution.path, jar=jar, java_source=resolution.source)

    def base_args(self) -> list[str]:
        return [str(self.java), *JDK_MODULE_OPENS, "-jar", str(self.jar)]


@dataclass(frozen=True)
class PanakoIndexPaths:
    """On-disk layout for one reference-pool index (all git-ignored under ``data/local``)."""

    root: Path

    @property
    def lmdb_dir(self) -> Path:
        return self.root / "olaf_lmdb"

    @property
    def cache_dir(self) -> Path:
        return self.root / "olaf_cache"

    @property
    def manifest_path(self) -> Path:
        return self.root / "index.json"

    @property
    def data_file(self) -> Path:
        return self.lmdb_dir / "data.mdb"

    def config_args(self) -> list[str]:
        return [
            f"OLAF_LMDB_FOLDER={_panako_path(self.lmdb_dir)}",
            f"OLAF_CACHE_FOLDER={_panako_path(self.cache_dir)}",
            "OLAF_STORAGE=LMDB",
            f"STRATEGY={PANAKO_STRATEGY}",
        ]


def _panako_path(path: Path) -> str:
    """Plain absolute path for a Panako argument.

    Panako's config parser, LMDB and the ffmpeg decoder pipe all reject Win32's extended-length
    ``\\\\?\\`` prefix that :func:`id_detector.io.native_path` adds, so index roots and audio files
    are handed to Panako as ordinary resolved paths.  Index directories live under a short
    ``data/local`` root, well within ``MAX_PATH``.
    """

    return str(Path(path).resolve())


# --------------------------------------------------------------------------------------------------
# LMDB Windows preallocation
# --------------------------------------------------------------------------------------------------
def sparse_extend(data_file: Path, size: int = PANAKO_LMDB_MAPSIZE) -> None:
    """Mark ``data_file`` sparse and set its length to ``size`` without consuming disk (Windows).

    LMDB validates the file's existing meta pages, so this must run only on a file Panako already
    initialised (never on a zero/blank file, which LMDB rejects as ``MDB_INVALID``).
    """

    if os.name != "nt":  # POSIX LMDB grows its own sparse file; nothing to do
        return
    import win32file
    import winioctlcon

    handle = win32file.CreateFile(
        native_path(data_file),
        win32file.GENERIC_READ | win32file.GENERIC_WRITE,
        0,
        None,
        win32file.OPEN_EXISTING,
        0,
        None,
    )
    try:
        win32file.DeviceIoControl(handle, winioctlcon.FSCTL_SET_SPARSE, None, 0)
        win32file.SetFilePointer(handle, size, win32file.FILE_BEGIN)
        win32file.SetEndOfFile(handle)
    finally:
        handle.Close()


async def ensure_index_ready(
    runtime: PanakoRuntime, paths: PanakoIndexPaths, *, timeout: float = 120
) -> None:
    """Make the LMDB store openable: create dirs and (on Windows) sparse-preallocate ``data.mdb``.

    On a fresh index we prime the store with a lightweight ``stats`` call whose LMDB open writes
    valid meta pages but fails to grow the file to the 1 TiB map size; we then sparse-extend that
    valid file so every subsequent store/query maps it without consuming disk.
    """

    os.makedirs(native_path(paths.lmdb_dir), exist_ok=True)
    os.makedirs(native_path(paths.cache_dir), exist_ok=True)
    if os.name != "nt":
        return
    if paths.data_file.is_file() and paths.data_file.stat().st_size >= PANAKO_LMDB_MAPSIZE:
        return
    if not paths.data_file.is_file():
        # Prime: this open writes the initial meta pages, then fails at the 1 TiB grow. Expected.
        await run_process(
            [*runtime.base_args(), *paths.config_args(), "stats"],
            timeout=timeout,
            check=False,
        )
    if not paths.data_file.is_file():
        raise PanakoError(
            "Panako did not initialise its LMDB data file during priming; cannot preallocate"
        )
    sparse_extend(paths.data_file)


# --------------------------------------------------------------------------------------------------
# Store (index) output parsing
# --------------------------------------------------------------------------------------------------
_STORE_RE = re.compile(
    r"Stored\s+(?P<count>\d+)\s+fingerprints\s+for\s+'(?P<path>.+?)',\s+id:\s+(?P<id>\d+)"
)


@dataclass(frozen=True)
class StoredResource:
    resource_id: str
    source_path: str
    fingerprint_count: int


def parse_store_output(text: str) -> list[StoredResource]:
    """Extract every ``Stored N fingerprints for '...', id: X`` line Panako emits during a store."""

    resources: list[StoredResource] = []
    for match in _STORE_RE.finditer(text):
        resources.append(
            StoredResource(
                resource_id=match.group("id"),
                source_path=match.group("path"),
                fingerprint_count=int(match.group("count")),
            )
        )
    return resources


# --------------------------------------------------------------------------------------------------
# Query output parsing
# --------------------------------------------------------------------------------------------------
_QUERY_HEADER_TOKEN = "Query start"


@dataclass(frozen=True)
class PanakoMatch:
    index: int
    total: int
    query_path: str
    query_start_s: float
    query_stop_s: float
    ref_path: str | None
    ref_id: str | None
    ref_start_s: float
    ref_stop_s: float
    score: int
    time_factor: float
    frequency_factor: float
    seconds_with_match: float

    @property
    def matched(self) -> bool:
        return self.ref_id is not None and self.score > 0


def _to_float(value: str) -> float:
    return float(value.strip().rstrip("%").strip())


def parse_query_output(text: str) -> list[PanakoMatch]:
    """Parse Panako's ``;``-delimited query CSV into structured matches (no floats leak upward)."""

    matches: list[PanakoMatch] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or _QUERY_HEADER_TOKEN in stripped:
            continue
        parts = [part.strip() for part in stripped.split(";")]
        if len(parts) < 13 or not parts[0].isdigit():
            continue
        ref_path = None if parts[5] in {"", "null"} else parts[5]
        ref_id = None if parts[6] in {"", "null"} else parts[6]
        matches.append(
            PanakoMatch(
                index=int(parts[0]),
                total=int(parts[1]),
                query_path=parts[2],
                query_start_s=_to_float(parts[3]),
                query_stop_s=_to_float(parts[4]),
                ref_path=ref_path,
                ref_id=ref_id,
                ref_start_s=_to_float(parts[7]),
                ref_stop_s=_to_float(parts[8]),
                score=int(round(_to_float(parts[9]))),
                time_factor=_to_float(parts[10]),
                frequency_factor=_to_float(parts[11]),
                seconds_with_match=_to_float(parts[12]),
            )
        )
    return matches


# --------------------------------------------------------------------------------------------------
# Observation normalisation
# --------------------------------------------------------------------------------------------------
#: Documented Panako anchor conversion, registered with the scanner-fusion anchor table.
PANAKO_ANCHOR_METHOD = "panako_query_offset_to_reference_offset"

#: Fingerprint-step granularity of the OLAF anchor: OLAF_STEP_SIZE (128) / OLAF_SAMPLE_RATE.
_ANCHOR_UNCERTAINTY_MS = 8


def _seconds_to_ms(value: float) -> int:
    return int(round(value * 1_000))


def _factor_to_e4(value: float) -> int:
    return int(round(value * 10_000))


@dataclass(frozen=True)
class QueryWindow:
    """The mix-timeline placement and identity of the WAV window handed to Panako."""

    window_id: str
    start_ms: int
    wav_sha256: str
    chunk_index: int


def _reference_label(
    match: PanakoMatch, resource_titles: Mapping[str, RawLabel] | None
) -> RawLabel:
    if resource_titles is not None and match.ref_id is not None and match.ref_id in resource_titles:
        return resource_titles[match.ref_id]
    title = Path(match.ref_path).stem if match.ref_path else None
    return RawLabel(artist=None, title=title, album=None, label=None, release_date=None)


def normalise_matches(
    matches: Iterable[PanakoMatch],
    *,
    query: QueryRecord,
    media_key: str,
    window: QueryWindow,
    duration_ms: int,
    raw_response_ref: str,
    resource_labels: Mapping[str, RawLabel] | None = None,
) -> tuple[ObservationRecord, ...]:
    """Normalise Panako matches for one queried window into final observations.

    Mirrors the scanner path: ``transform = null``; ``logical_trial_id = sha1(provider ‖ chunk)``;
    ``mix_span_ms`` in the original mix timebase (window start + Panako's in-query offset); the
    anchor maps that mix time to the reference track's own offset; ``score_raw`` is the fingerprint
    match count.  A window with no Panako match yields a single ``no_match`` observation.
    """

    logical_trial_id = scanner_logical_trial_id(PROVIDER, window.chunk_index)
    observations: list[ObservationRecord] = []
    matched = [match for match in matches if match.matched]
    for native_index, match in enumerate(matched):
        mix_start = min(duration_ms, window.start_ms + _seconds_to_ms(match.query_start_s))
        mix_end = min(duration_ms, window.start_ms + _seconds_to_ms(match.query_stop_s))
        if mix_end < mix_start:
            mix_end = mix_start
        if mix_end == mix_start:
            mix_end = min(duration_ms, mix_start + 1)
        ref_anchor_ms = _seconds_to_ms(match.ref_start_s)
        anchor = Anchor(
            mix_anchor_ms=mix_start,
            ref_anchor_ms=ref_anchor_ms,
            uncertainty_ms=_ANCHOR_UNCERTAINTY_MS,
            reliable=True,
            method=PANAKO_ANCHOR_METHOD,
            bias_applied_ms=0,
        )
        label = _reference_label(match, resource_labels)
        native = {
            "panako_resource_id": match.ref_id,
            "panako_ref_path": match.ref_path,
            "query_start_ms": _seconds_to_ms(match.query_start_s),
            "query_stop_ms": _seconds_to_ms(match.query_stop_s),
            "ref_start_ms": ref_anchor_ms,
            "ref_stop_ms": _seconds_to_ms(match.ref_stop_s),
            "score": match.score,
            "time_factor_e4": _factor_to_e4(match.time_factor),
            "frequency_factor_e4": _factor_to_e4(match.frequency_factor),
            "seconds_with_match_e4": _factor_to_e4(match.seconds_with_match),
            "strategy": PANAKO_STRATEGY,
        }
        label_hash = sha256(canonical_json_bytes(label)).hexdigest()
        natural = {
            "query_id": query.id,
            "mix_span_ms": [mix_start, mix_end],
            "raw_label_hash": label_hash,
            "native_index": native_index,
            "transform": None,
        }
        observations.append(
            ObservationRecord(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                id=make_id(media_key, "observation", compose_natural_key("observation", natural)),
                generation=query.generation,
                query_id=query.id,
                provider=PROVIDER,
                capability=CAPABILITY_NAME,
                status="match",
                is_final=True,
                mix_span_ms=(mix_start, mix_end),
                support_ms=(mix_start, mix_end),
                transform=None,
                logical_trial_id=logical_trial_id,
                raw_label=label,
                provider_ids={"panako": match.ref_id},
                native=native,
                anchor=anchor,
                score_raw=match.score,
                quality=None,
                raw_response_ref=raw_response_ref,
                source_ids=[f"query:{query.id}"],
            )
        )

    if not matched:
        mix_start = window.start_ms
        mix_end = (
            min(duration_ms, window.start_ms + 1)
            if duration_ms > window.start_ms
            else (window.start_ms + 1)
        )
        empty_label = RawLabel(artist=None, title=None, album=None, label=None, release_date=None)
        label_hash = sha256(canonical_json_bytes(empty_label)).hexdigest()
        natural = {
            "query_id": query.id,
            "mix_span_ms": [mix_start, mix_end],
            "raw_label_hash": label_hash,
            "native_index": 0,
            "transform": None,
        }
        observations.append(
            ObservationRecord(
                schema_version=SCHEMA_VERSION,
                generated_by=GENERATED_BY,
                id=make_id(media_key, "observation", compose_natural_key("observation", natural)),
                generation=query.generation,
                query_id=query.id,
                provider=PROVIDER,
                capability=CAPABILITY_NAME,
                status="no_match",
                is_final=True,
                mix_span_ms=(mix_start, mix_end),
                support_ms=(mix_start, mix_end),
                transform=None,
                logical_trial_id=logical_trial_id,
                raw_label=empty_label,
                provider_ids={},
                native={"strategy": PANAKO_STRATEGY, "score": 0},
                anchor=None,
                score_raw=None,
                quality=None,
                raw_response_ref=raw_response_ref,
                source_ids=[f"query:{query.id}"],
            )
        )
    return tuple(sort_records(observations))


# --------------------------------------------------------------------------------------------------
# Provider adapter
# --------------------------------------------------------------------------------------------------
@dataclass
class PanakoProvider:
    """Drives the pinned Panako jar to build and query a local reference-pool index."""

    runtime: PanakoRuntime
    paths: PanakoIndexPaths
    store_timeout: float = 900
    query_timeout: float = 300
    capability: ProviderCapability = field(init=False)

    def __post_init__(self) -> None:
        self.capability = ProviderCapability(
            PROVIDER, CAPABILITY_NAME, True, f"Panako {PANAKO_VERSION} at {self.paths.root}"
        )

    async def store(self, audio_paths: Sequence[Path]) -> list[StoredResource]:
        """Fingerprint ``audio_paths`` into the index (one subprocess per file, bounded)."""

        await ensure_index_ready(self.runtime, self.paths)
        stored: list[StoredResource] = []
        for audio in audio_paths:
            result = await run_process(
                [
                    *self.runtime.base_args(),
                    *self.paths.config_args(),
                    "store",
                    _panako_path(Path(audio)),
                ],
                timeout=self.store_timeout,
                check=False,
            )
            resources = parse_store_output(result.stdout + result.stderr)
            if not resources:
                raise PanakoError(
                    f"Panako store produced no fingerprints for {audio}: {_diagnostic(result)}"
                )
            stored.extend(resources)
        return stored

    async def query_wav(self, wav_path: Path) -> tuple[ProcessResult, list[PanakoMatch]]:
        """Run one Panako query over a WAV window and return the raw result and parsed matches."""

        await ensure_index_ready(self.runtime, self.paths)
        result = await run_process(
            [
                *self.runtime.base_args(),
                *self.paths.config_args(),
                "query",
                _panako_path(Path(wav_path)),
            ],
            timeout=self.query_timeout,
            check=False,
        )
        return result, parse_query_output(result.stdout + result.stderr)


def _diagnostic(result: ProcessResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "no output"
    return detail[-800:]
