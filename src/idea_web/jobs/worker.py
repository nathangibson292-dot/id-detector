"""Framework-neutral SQLite queue, intake and analysis worker (plan cycle 4b-i)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol

from id_detector.attempts import AttemptJournal
from id_detector.compat import (
    LOCAL_OWNER_SCOPE,
    AnalysisInputs,
    hints_snapshot,
    index_identity,
    serves,
)
from id_detector.compat import (
    RunRequest as CompatibilityRequest,
)
from id_detector.contracts import ProviderAttemptEvent
from id_detector.decode import decode
from id_detector.hints.pipeline import run_hints
from id_detector.ingest import _load_cached, ingest
from id_detector.io import fsync_directory, native_path, path_is_file, read_text, sha256_file
from id_detector.jobs import JobStoreLocked, ProcessLock
from id_detector.journal import timestamp
from id_detector.present.bundles import read_bundle_manifest
from id_detector.providers.base import AppConfig
from id_detector.recipes import RECIPES, Recipe, get_recipe
from id_detector.service import (
    CHECKPOINT_PHASES,
    CheckpointPhase,
    LocalPath,
    PipelineOptions,
    PlatformUrl,
    RunRequest,
    RunResult,
    TargetRefused,
    UploadId,
    validate_platform_url,
    validate_upload_id,
)
from id_detector.shazam_breaker import BreakerConfig, ShazamBreaker
from idea_web.database import Database

HEARTBEAT_SECONDS = 10.0
DEFAULT_LEASE_SECONDS = 30.0
MAX_ATTEMPTS = 3
#: How long a job that is merely *waiting* (an open breaker, a busy source) stays unclaimable.
#: Waiting is not a failed attempt, so the attempt this claim consumed is given back; without a
#: cooldown the consumer would spin on the same row for as long as the provider stays down.
WAIT_COOLDOWN_SECONDS = 30.0
TERMINAL_STATES = frozenset(
    {
        "complete",
        "degraded",
        "partial",
        "provider_unavailable",
        "budget_exhausted",
        "source_changed",
        "quota_exceeded",
        "failed",
        "cancelled",
    }
)
ACTIVE_RUN_STATES = frozenset({"intake", "waiting", "analysis"})
_ACTIVE_SQL = "('intake','waiting','analysis')"
#: ``json_extract`` is NULL for a job whose progress has no ``attached`` key, and ``NOT (TRUE AND
#: NULL)`` is NULL, not TRUE -- a predicate written without COALESCE silently excluded every
#: breaker-waiting job from the queue forever.
_ATTACHED_SQL = "COALESCE(json_extract(progress, '$.attached'), 0) = 1"


class StaleClaim(RuntimeError):
    """A write arrived from a claim that is no longer the current one (plan §4.6).

    Raised instead of silently succeeding: a worker whose lease was reclaimed must not be able to
    overwrite its replacement's checkpoints, money or attempt ledger, and must find out.
    """


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fsync_artefact(path: Path) -> None:
    """Flush one artefact's bytes (and, on POSIX, its directory entry) to the device.

    §3.4's publication invariant is that artefact files are written *and fsynced* before any row
    points at them. Existence is not durability: the decoder, for instance, renames an ffmpeg
    temporary into place without flushing it, so a power cut can leave a committed checkpoint
    naming a PCM file whose bytes were never on the disk. The checkpoint boundary therefore
    flushes what it is about to reference rather than trusting every producer to have done it.
    """

    descriptor = os.open(native_path(Path(path)), os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(Path(path).parent)


def require_durable(artefacts: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
    """Return the artefacts, refusing any that is missing or cannot be made durable."""

    resolved: list[Path] = []
    for item in artefacts:
        path = Path(item)
        if not path_is_file(path):
            raise ValueError(f"checkpoint artefacts are not durable: {path}")
        try:
            fsync_artefact(path)
        except OSError as exc:
            raise ValueError(f"checkpoint artefact could not be flushed: {path} ({exc})") from exc
        resolved.append(path.resolve())
    return tuple(resolved)


def _target_document(
    target: PlatformUrl | UploadId | LocalPath, *, local_mode: bool
) -> dict[str, str]:
    if isinstance(target, PlatformUrl):
        return {"kind": "platform", "url": validate_platform_url(target.url)}
    if isinstance(target, UploadId):
        return {"kind": "upload", "upload_id": validate_upload_id(target.upload_id)}
    if isinstance(target, LocalPath):
        if not local_mode:
            raise TargetRefused("LocalPath is accepted only in local mode")
        return {"kind": "local", "path": str(target.path)}
    raise TypeError("target must be PlatformUrl, UploadId, or LocalPath")


def _read_target(document: str, *, local_mode: bool) -> PlatformUrl | UploadId | LocalPath:
    """Parse and validate an untrusted queue value every time it leaves SQLite."""

    try:
        value = json.loads(document)
    except (TypeError, json.JSONDecodeError) as exc:
        raise TargetRefused("job target is not valid JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        raise TargetRefused("job target has no valid kind")
    kind = value["kind"]
    if kind == "platform" and set(value) == {"kind", "url"}:
        return PlatformUrl(validate_platform_url(value.get("url")))
    if kind == "upload" and set(value) == {"kind", "upload_id"}:
        return UploadId(validate_upload_id(value.get("upload_id")))
    if kind == "local" and set(value) == {"kind", "path"}:
        if not local_mode:
            raise TargetRefused("LocalPath is accepted only in local mode")
        if not isinstance(value.get("path"), str) or not value["path"]:
            raise TargetRefused("local path is empty")
        return LocalPath(Path(value["path"]))
    raise TargetRefused("job target shape is invalid")


@dataclass(frozen=True)
class Job:
    id: str
    run_id: str | None
    target: PlatformUrl | UploadId | LocalPath
    recipe: Recipe
    state: str
    lease_owner: str | None
    lease_until: float | None
    heartbeat_at: float | None
    claim_token: str | None
    attempt: int
    max_attempts: int
    dead_letter_reason: str | None
    cancel_requested: bool
    progress: dict[str, Any]
    log_path: Path | None
    result_bundle_id: str | None
    tenant_scope: str
    created_at: float
    updated_at: float

    @property
    def token(self) -> str:
        if not self.claim_token:
            raise StaleClaim(f"job {self.id} is not claimed")
        return self.claim_token


@dataclass(frozen=True)
class PreparedIntake:
    """Durable media identity assembled before intake's one deciding transaction."""

    inputs: AnalysisInputs
    media_dir: Path
    duration_ms: int
    checkpoints: Mapping[str, Mapping[str, object]]

    @property
    def analysis_key(self) -> str:
        return self.inputs.analysis_key


class ReservationSeam(Protocol):
    """4d-i implements this inside the intake transaction; 4b-i does not mint credits."""

    def reserve_for_new_run(self, connection: Any, job: Job, intake: PreparedIntake) -> None: ...


class SQLiteCheckpointStore:
    """The service checkpoint protocol backed by ``analysis_runs.checkpoints``.

    Every write is fenced by the claim token of the job that owns the run, and no phase is
    committed until the artefacts it names have been flushed to the device.
    """

    mode: Literal["local", "hosted"]

    def __init__(
        self,
        database: Database,
        work_root: Path,
        *,
        mode: Literal["local", "hosted"] = "hosted",
        options: PipelineOptions | None = None,
        upload_root: Path | None = None,
        claim_token: str | None = None,
        before_commit: Callable[[str, CheckpointPhase], None] | None = None,
        after_commit: Callable[[str, CheckpointPhase], None] | None = None,
    ) -> None:
        self.database = database
        self.work_root = Path(work_root).resolve()
        self.mode = mode
        self.options = options or PipelineOptions()
        self.upload_root = (upload_root or self.work_root / ".uploads").resolve()
        self.claim_token = claim_token
        self.before_commit = before_commit
        self.after_commit = after_commit

    def _document(self, connection: Any, run_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT checkpoints FROM analysis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown analysis run: {run_id}")
        try:
            document = json.loads(row["checkpoints"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid checkpoints for run {run_id}") from exc
        if not isinstance(document, dict):
            raise ValueError(f"invalid checkpoints for run {run_id}")
        return document

    def completed_phases(self, run_id: str) -> frozenset[CheckpointPhase]:
        with self.database.read() as connection:
            known = set(CHECKPOINT_PHASES)
            phases = self._document(connection, run_id).keys() & known
            return frozenset(phases)  # type: ignore[return-value]

    def write(
        self,
        run_id: str,
        phase: CheckpointPhase,
        *,
        artefacts: tuple[Path, ...],
        state: dict[str, Any] | None = None,
    ) -> None:
        if phase not in CHECKPOINT_PHASES:
            raise ValueError(f"unknown checkpoint phase: {phase}")
        if self.claim_token is None:
            raise StaleClaim("checkpoint store has no claim token; refusing to write")
        durable = require_durable(artefacts)
        if self.before_commit is not None:
            self.before_commit(run_id, phase)
        with self.database.write() as connection:
            document = self._document(connection, run_id)
            document[phase] = {
                "artefacts": [str(path) for path in durable],
                "state": state or {},
            }
            if phase == "primary":
                phase_state = state or {}
                # Money and attempts are monotonic within a run (§2.3.2): a later write may report
                # more spend, never less, so a settlement can never erase recovered spend.
                changed = connection.execute(
                    "UPDATE analysis_runs SET checkpoints=?, "
                    "usd_e6_reserved=MAX(usd_e6_reserved, ?), "
                    "usd_e6_spent=MAX(usd_e6_spent, ?), attempts=MAX(attempts, ?) "
                    "WHERE run_id=? AND claim_token=?",
                    (
                        _json(document),
                        int(phase_state.get("usd_e6_reserved", 0)),
                        int(phase_state.get("usd_e6_spent", 0)),
                        int(phase_state.get("attempts", 0)),
                        run_id,
                        self.claim_token,
                    ),
                ).rowcount
            else:
                changed = connection.execute(
                    "UPDATE analysis_runs SET checkpoints = ? WHERE run_id = ? AND claim_token = ?",
                    (_json(document), run_id, self.claim_token),
                ).rowcount
            if changed != 1:
                raise StaleClaim(f"checkpoint claim is no longer current: {run_id}/{phase}")
        if self.after_commit is not None:
            self.after_commit(run_id, phase)

    def state(self, run_id: str, phase: CheckpointPhase) -> dict[str, Any]:
        with self.database.read() as connection:
            value = self._document(connection, run_id).get(phase, {})
        state = value.get("state", {}) if isinstance(value, dict) else {}
        return state if isinstance(state, dict) else {}

    def resolve_upload(self, upload_id: str) -> Path:
        candidate = (self.upload_root / validate_upload_id(upload_id)).resolve()
        if not candidate.is_relative_to(self.upload_root) or not candidate.is_file():
            raise TargetRefused(f"unknown upload id: {upload_id}")
        return candidate


def _http_status(outcome: str | None) -> int | None:
    return {
        "auth_error": 401,
        "quota_error": 402,
        "http_429": 429,
        "http_503": 503,
        "http_5xx": 500,
    }.get(outcome)


#: The ledger's own sequence numbers. The durable JSONL numbers the same three events from zero
#: (``contracts.ATTEMPT_EVENT_SEQ``), so a projection must MAP the event kind rather than copy the
#: file's number -- copying it wrote rows the ``seq BETWEEN 1 AND 3`` check rejected.
_EVENT_SEQ = {"prepared": 1, "dispatched": 2, "resolved": 3}


class _EventProjection:
    """Insert immutable ``provider_attempt_events`` rows, fenced by the run's claim token."""

    def __init__(self, database: Database, *, run_id: str, claim_token: str | None) -> None:
        self.database = database
        self.run_id = run_id
        self.claim_token = claim_token

    def fence(self, connection: Any) -> None:
        if self.claim_token is None:
            raise StaleClaim("attempt journal has no claim token; refusing to write")
        row = connection.execute(
            "SELECT 1 FROM analysis_runs WHERE run_id=? AND claim_token=?",
            (self.run_id, self.claim_token),
        ).fetchone()
        if row is None:
            raise StaleClaim(f"attempt claim is no longer current: {self.run_id}")

    def insert(
        self,
        connection: Any,
        *,
        attempt_id: str,
        provider: str,
        egress_id: str,
        query_id: str | None,
        parent_attempt_id: str | None,
        state: str,
        outcome: str | None,
        unit_usd_e6: int,
        at: str,
    ) -> None:
        # ``ON CONFLICT DO NOTHING``, not ``INSERT OR IGNORE``: replaying an event this run already
        # projected is expected, but a row that violates a CHECK must raise instead of vanishing.
        connection.execute(
            "INSERT INTO provider_attempt_events("
            "attempt_id, seq, run_id, provider, egress_id, query_id, parent_attempt_id, "
            "state, outcome, http_status, unit_usd_e6, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(attempt_id, seq) DO NOTHING",
            (
                attempt_id,
                _EVENT_SEQ[state],
                self.run_id,
                provider,
                egress_id,
                query_id,
                parent_attempt_id,
                state,
                outcome,
                _http_status(outcome),
                unit_usd_e6,
                at,
            ),
        )


class SQLiteAttemptJournal(AttemptJournal):
    """Attempt journal that also projects each immutable event into SQLite.

    The JSONL file stays authoritative -- it is what ``recover_paid_attempts`` reads, and it is
    fsynced before the request enters network I/O. The SQLite rows are a projection, so a crash
    between the two is repaired by :meth:`backfill` rather than losing an event.
    """

    def __init__(
        self,
        path: Path,
        *,
        database: Database,
        run_id: str,
        provider: str,
        unit_usd_e6: int,
        egress_id: str,
        claim_token: str | None = None,
    ) -> None:
        super().__init__(path, run_id=run_id, provider=provider, unit_usd_e6=unit_usd_e6)
        self.database = database
        self.egress_id = egress_id
        self.claim_token = claim_token
        self.projection = _EventProjection(database, run_id=run_id, claim_token=claim_token)

    def backfill(self) -> int:
        """Project any durable JSONL event this run is missing in SQLite.

        A crash between the journal append and the SQLite insert would otherwise leave the hosted
        ledger (the breaker denominator and 4b-ii's operations views) permanently short of an
        attempt the provider really saw.
        """

        if not path_is_file(self.path):
            return 0
        projected = 0
        with self.database.write() as connection:
            self.projection.fence(connection)
            for line in read_text(self.path).splitlines():
                if not line.strip():
                    continue
                try:
                    event = ProviderAttemptEvent.model_validate(json.loads(line))
                except ValueError:  # a torn trailing line from a crash mid-append
                    continue
                if event.run_id != self.run_id:
                    continue
                self.projection.insert(
                    connection,
                    attempt_id=event.attempt_id,
                    provider=event.provider,
                    egress_id=self.egress_id,
                    query_id=event.query_id,
                    parent_attempt_id=event.parent_attempt_id,
                    state=event.event,
                    outcome=event.outcome,
                    unit_usd_e6=event.unit_usd_e6,
                    at=event.at,
                )
                projected += 1
        return projected

    def _write(self, event: str, attempt_id: str, outcome: str | None) -> None:
        query_id, _window_id, _ordinal, parent_attempt_id = self._open[attempt_id]
        with self.database.write() as connection:
            # Fenced BEFORE the durable append: a worker that lost its lease must not be able to
            # journal -- and therefore must not be able to dispatch -- another paid request.
            self.projection.fence(connection)
            super()._write(event, attempt_id, outcome)  # type: ignore[arg-type]
            self.projection.insert(
                connection,
                attempt_id=attempt_id,
                provider=self.provider,
                egress_id=self.egress_id,
                query_id=query_id,
                parent_attempt_id=parent_attempt_id,
                state=event,
                outcome=outcome,
                unit_usd_e6=self.unit_usd_e6,
                at=timestamp(),
            )


class LedgerShazamBreaker(ShazamBreaker):
    """§2.3.5's per-process Shazam policy, projected into the hosted attempt ledger.

    4b-ii's service-wide breaker reads ``provider_attempt_events``: without these rows the table
    has an AudD-only history and no Shazam denominator, so free primaries and Deep secondaries
    would be invisible to it. The breaker is the only hosted seam the recognise path exposes; it
    knows the egress and the outcome but not the clip cache key, so ``query_id`` is NULL rather
    than invented. ``prepared`` is written when the attempt is admitted (before network I/O) and
    ``dispatched``/``resolved`` when it settles -- an unresolved Shazam attempt is free, so
    re-issuing it is correct and it is never counted as spend.
    """

    def __init__(
        self,
        config: BreakerConfig | None = None,
        *,
        database: Database,
        run_id: str,
        egress_id: str,
        claim_token: str | None,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(config, clock=clock)
        self.projection = _EventProjection(database, run_id=run_id, claim_token=claim_token)
        self.database = database
        self.egress_id = egress_id
        self._pending: deque[str] = deque()
        self._ledger_lock = threading.Lock()

    def _record(self, attempt_id: str, state: str, outcome: str | None) -> None:
        with self.database.write() as connection:
            self.projection.fence(connection)
            self.projection.insert(
                connection,
                attempt_id=attempt_id,
                provider="shazam",
                egress_id=self.egress_id,
                query_id=None,
                parent_attempt_id=None,
                state=state,
                outcome=outcome,
                unit_usd_e6=0,
                at=timestamp(),
            )

    def dispatch(self, *, running_free: bool) -> Any:
        day = super().dispatch(running_free=running_free)
        attempt_id = uuid.uuid4().hex
        self._record(attempt_id, "prepared", None)
        with self._ledger_lock:
            self._pending.append(attempt_id)
        return day

    def release_dispatch(self, dispatch_day: Any) -> None:
        super().release_dispatch(dispatch_day)
        with self._ledger_lock:
            if self._pending:
                # The admission was rolled back before network I/O; its ``prepared`` row stands
                # alone, which is exactly what "never sent" means in §2.3.3.
                self._pending.pop()

    def resolved(self, outcome: str) -> None:
        super().resolved(outcome)
        with self._ledger_lock:
            attempt_id = self._pending.popleft() if self._pending else uuid.uuid4().hex
        self._record(attempt_id, "dispatched", None)
        self._record(attempt_id, "resolved", outcome)


class JobQueue:
    """All job transitions, each fenced and committed in one immediate transaction."""

    def __init__(
        self,
        database: Database,
        *,
        local_mode: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.local_mode = local_mode
        self.clock = clock

    def enqueue(
        self,
        target: PlatformUrl | UploadId | LocalPath,
        recipe: Recipe,
        *,
        job_id: str | None = None,
        run_id: str | None = None,
        tenant_scope: str | None = None,
        log_path: Path | None = None,
        progress: Mapping[str, object] | None = None,
    ) -> str:
        document = _target_document(target, local_mode=self.local_mode)
        scope = tenant_scope or (
            LOCAL_OWNER_SCOPE if isinstance(target, (UploadId, LocalPath)) else "public"
        )
        if scope != "public" and not scope.startswith("user:"):
            raise ValueError("invalid tenant scope")
        identifier = job_id or uuid.uuid4().hex
        now = self.clock()
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO jobs(id, run_id, target, recipe_id, state, log_path, tenant_scope, "
                "progress, created_at, updated_at) VALUES (?, ?, ?, ?, 'intake', ?, ?, ?, ?, ?)",
                (
                    identifier,
                    run_id,
                    _json(document),
                    recipe.recipe_id,
                    str(Path(log_path).resolve()) if log_path is not None else None,
                    scope,
                    _json(dict(progress or {})),
                    now,
                    now,
                ),
            )
        return identifier

    def get(self, job_id: str) -> Job:
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row(row)

    def _row(self, row: Any) -> Job:
        known_recipes = (*RECIPES.values(), get_recipe("deep", primary_density=2))
        recipe = next(
            (candidate for candidate in known_recipes if candidate.recipe_id == row["recipe_id"]),
            None,
        )
        if recipe is None:
            raise ValueError(f"untrusted job has unknown recipe id: {row['recipe_id']}")
        try:
            progress = json.loads(row["progress"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("untrusted job has invalid progress JSON") from exc
        if not isinstance(progress, dict):
            raise ValueError("untrusted job has invalid progress JSON")
        return Job(
            id=row["id"],
            run_id=row["run_id"],
            target=_read_target(row["target"], local_mode=self.local_mode),
            recipe=recipe,
            state=row["state"],
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
            heartbeat_at=row["heartbeat_at"],
            claim_token=row["claim_token"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            dead_letter_reason=row["dead_letter_reason"],
            cancel_requested=bool(row["cancel_requested"]),
            progress=progress,
            log_path=Path(row["log_path"]) if row["log_path"] else None,
            result_bundle_id=row["result_bundle_id"],
            tenant_scope=row["tenant_scope"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _abandon_run(
        self, connection: Any, run_id: str | None, *, status: str, reason: str | None, now: float
    ) -> None:
        """Move a run to the same terminal fate as the job that was driving it.

        A dead letter that left its run ``analysis`` is an immortal run: nobody will ever execute
        it, yet the intake transaction would keep attaching new submissions to it. Accounting
        (attempts, reservation, spend) is deliberately untouched.
        """

        if run_id is None:
            return
        connection.execute(
            "UPDATE analysis_runs SET status=?, reason=COALESCE(reason, ?), "
            "finished_at=COALESCE(finished_at, ?), claim_token=NULL "
            f"WHERE run_id=? AND status IN {_ACTIVE_SQL}",
            (status, reason, now, run_id),
        )

    def _quarantine(self, connection: Any, row: Any, reason: str, now: float) -> None:
        """Retire a row this worker cannot even parse, inside the claim transaction.

        One corrupt row used to abort the claim transaction (rolling back its own attempt
        increment) and kill ``run_forever``; the queue must survive its worst row.
        """

        connection.execute(
            "UPDATE jobs SET state='dead_letter', dead_letter_reason=?, lease_owner=NULL, "
            "lease_until=NULL, heartbeat_at=NULL, claim_token=NULL, updated_at=? WHERE id=?",
            (reason, now, row["id"]),
        )
        self._abandon_run(connection, row["run_id"], status="dead_letter", reason=reason, now=now)

    def claim(self, worker_id: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> Job | None:
        """Fence the oldest claimable job to a fresh claim token and return it."""

        now = self.clock()
        with self.database.write() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE state IN {_ACTIVE_SQL} "
                f"AND NOT (state='waiting' AND {_ATTACHED_SQL}) "
                "AND (lease_until IS NULL OR lease_until <= ?) ORDER BY created_at, id",
                (now,),
            ).fetchall()
            for row in rows:
                if row["attempt"] >= row["max_attempts"]:
                    self._quarantine(connection, row, "maximum attempts exhausted", now)
                    continue
                token = uuid.uuid4().hex
                changed = connection.execute(
                    "UPDATE jobs SET lease_owner=?, lease_until=?, heartbeat_at=?, claim_token=?, "
                    "attempt=attempt+1, "
                    "updated_at=? WHERE id=? AND (lease_until IS NULL OR lease_until <= ?)",
                    (worker_id, now + lease_seconds, now, token, now, row["id"], now),
                ).rowcount
                if changed != 1:
                    continue
                claimed = connection.execute(
                    "SELECT * FROM jobs WHERE id=?", (row["id"],)
                ).fetchone()
                try:
                    job = self._row(claimed)
                except (ValueError, TargetRefused) as exc:
                    self._quarantine(
                        connection, claimed, f"unusable job row: {type(exc).__name__}: {exc}", now
                    )
                    continue
                if job.run_id is not None:
                    # Ownership of the run rotates with the claim, so the previous claim's
                    # checkpoint, money and journal writes are refused from this moment on --
                    # including on the cancel-before-start path, which never enters analysis.
                    connection.execute(
                        "UPDATE analysis_runs SET claim_token=? WHERE run_id=?",
                        (token, job.run_id),
                    )
                return job
        return None

    def begin_analysis(self, job_id: str, claim_token: str) -> bool:
        """Make a waiting retry visibly analysis before it enters the pipeline."""

        now = self.clock()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT run_id FROM jobs WHERE id=? AND claim_token=? AND lease_until > ? "
                "AND state IN ('waiting','analysis')",
                (job_id, claim_token, now),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE jobs SET state='analysis', updated_at=? WHERE id=?",
                (now, job_id),
            )
            connection.execute(
                "UPDATE analysis_runs SET status='analysis' WHERE run_id=? AND claim_token=?",
                (row["run_id"], claim_token),
            )
            return True

    def reconcile_attached(self) -> int:
        """Mirror a coalesced run's terminal result without ever running its pipeline twice."""

        now = self.clock()
        changed = 0
        with self.database.write() as connection:
            rows = connection.execute(
                "SELECT j.id, j.run_id, r.status FROM jobs j "
                "JOIN analysis_runs r ON r.run_id=j.run_id "
                "WHERE j.state='waiting' AND json_extract(j.progress, '$.attached')=1 "
                f"AND r.status NOT IN {_ACTIVE_SQL}"
            ).fetchall()
            for row in rows:
                bundle = connection.execute(
                    "SELECT bundle_id FROM result_bundles WHERE run_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (row["run_id"],),
                ).fetchone()
                connection.execute(
                    "UPDATE jobs SET state=?, result_bundle_id=?, updated_at=? WHERE id=?",
                    (
                        row["status"],
                        bundle["bundle_id"] if bundle is not None else None,
                        now,
                        row["id"],
                    ),
                )
                changed += 1
        return changed

    def heartbeat(self, job_id: str, claim_token: str, *, lease_seconds: float) -> bool:
        """Renew a lease that has NOT yet expired; an expired lease is the replacement's."""

        now = self.clock()
        with self.database.write() as connection:
            return (
                connection.execute(
                    "UPDATE jobs SET heartbeat_at=?, lease_until=?, updated_at=? "
                    "WHERE id=? AND claim_token=? AND lease_until > ? "
                    f"AND state IN {_ACTIVE_SQL}",
                    (now, now + lease_seconds, now, job_id, claim_token, now),
                ).rowcount
                == 1
            )

    def cancel_unclaimed(self, job_id: str, progress: Mapping[str, object]) -> bool:
        """Settle a job no worker holds as ``cancelled`` at once, in one fenced transaction.

        Only an intake job with no run and no claim qualifies: the moment a worker claims it the
        fence fails and the caller falls back to :meth:`request_cancel`, which that worker honours.
        """

        now = self.clock()
        with self.database.write() as connection:
            return (
                connection.execute(
                    "UPDATE jobs SET state='cancelled', cancel_requested=1, progress=?, "
                    "lease_owner=NULL, lease_until=NULL, heartbeat_at=NULL, updated_at=? "
                    "WHERE id=? AND state='intake' AND run_id IS NULL AND claim_token IS NULL",
                    (_json(dict(progress)), now, job_id),
                ).rowcount
                == 1
            )

    def dismiss(self, job_id: str) -> bool:
        """Hide one terminal job from the owner's activity list; running work is never touched."""

        now = self.clock()
        with self.database.write() as connection:
            return (
                connection.execute(
                    "UPDATE jobs SET progress=json_set(progress, '$.dismissed', 1), updated_at=? "
                    f"WHERE id=? AND state NOT IN {_ACTIVE_SQL}",
                    (now, job_id),
                ).rowcount
                == 1
            )

    def request_cancel(self, job_id: str) -> bool:
        now = self.clock()
        with self.database.write() as connection:
            return (
                connection.execute(
                    "UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=? "
                    f"AND state IN {_ACTIVE_SQL}",
                    (now, job_id),
                ).rowcount
                == 1
            )

    def cancel_requested(self, job_id: str, claim_token: str) -> bool:
        """True when this claim must stop: cancelled, gone, reclaimed, or lease expired."""

        now = self.clock()
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT cancel_requested, claim_token, lease_until FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if row is None or row["claim_token"] != claim_token:
            return True
        if row["lease_until"] is None or row["lease_until"] <= now:
            return True
        return bool(row["cancel_requested"])

    def update_progress(
        self, job_id: str, claim_token: str, progress: Mapping[str, object], *, lease_seconds: float
    ) -> bool:
        now = self.clock()
        with self.database.write() as connection:
            return (
                connection.execute(
                    "UPDATE jobs SET progress=?, heartbeat_at=?, lease_until=?, updated_at=? "
                    "WHERE id=? AND claim_token=? AND lease_until > ? "
                    f"AND state IN {_ACTIVE_SQL}",
                    (_json(progress), now, now + lease_seconds, now, job_id, claim_token, now),
                ).rowcount
                == 1
            )

    def fail(self, job: Job, claim_token: str, reason: str) -> None:
        now = self.clock()
        dead = job.attempt >= job.max_attempts
        state = "dead_letter" if dead else job.state
        dead_reason = reason if dead else None
        with self.database.write() as connection:
            changed = connection.execute(
                "UPDATE jobs SET state=?, dead_letter_reason=?, lease_owner=NULL, "
                "lease_until=NULL, "
                "heartbeat_at=NULL, claim_token=NULL, updated_at=? WHERE id=? AND claim_token=?",
                (state, dead_reason, now, job.id, claim_token),
            ).rowcount
            if changed != 1:
                return
            if dead:
                self._abandon_run(
                    connection, job.run_id, status="dead_letter", reason=reason, now=now
                )
            else:
                # Retryable: the run stays claimable, but this claim no longer owns it.
                connection.execute(
                    "UPDATE analysis_runs SET claim_token=NULL WHERE run_id=? AND claim_token=?",
                    (job.run_id, claim_token),
                )

    def terminal(
        self,
        job_id: str,
        claim_token: str,
        result: RunResult,
        *,
        bundle_path: Path | None,
        progress: Mapping[str, object] | None = None,
    ) -> bool:
        if result.status not in TERMINAL_STATES:
            raise ValueError(f"not a terminal status: {result.status}")
        now = self.clock()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT run_id FROM jobs WHERE id=? AND claim_token=?", (job_id, claim_token)
            ).fetchone()
            if row is None:
                return False
            if row["run_id"] is not None:
                # MAX, not assignment: money and attempts already recovered onto this run are
                # facts. A settlement that reported less (a cancel before restart, a refusal)
                # would erase spend the owner was really charged.
                connection.execute(
                    "UPDATE analysis_runs SET achieved=?, status=?, reason=?, "
                    "attempts=MAX(attempts, ?), "
                    "usd_e6_reserved=MAX(usd_e6_reserved, ?), usd_e6_spent=MAX(usd_e6_spent, ?), "
                    "finished_at=?, claim_token=NULL WHERE run_id=? AND claim_token=?",
                    (
                        _json(result.achieved) if result.achieved is not None else None,
                        result.status,
                        result.reason,
                        result.attempts,
                        result.usd_e6_reserved,
                        result.usd_e6_spent,
                        now,
                        row["run_id"],
                        claim_token,
                    ),
                )
            bundle_id = None
            if result.bundle_id is not None and bundle_path is not None:
                manifest = read_bundle_manifest(bundle_path)
                if manifest is None or bundle_path.name != result.bundle_id:
                    raise ValueError("result bundle is not durably published")
                connection.execute(
                    "INSERT OR IGNORE INTO result_bundles(bundle_id, run_id, presentation_version, "
                    "path, manifest_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        result.bundle_id,
                        row["run_id"],
                        manifest["presentation_version"],
                        str(bundle_path.resolve()),
                        sha256_file(bundle_path / "manifest.json"),
                        now,
                    ),
                )
                bundle_id = result.bundle_id
            changed = connection.execute(
                "UPDATE jobs SET state=?, result_bundle_id=?, lease_owner=NULL, lease_until=NULL, "
                "heartbeat_at=NULL, claim_token=NULL, progress=COALESCE(?, progress), updated_at=? "
                "WHERE id=? AND claim_token=?",
                (
                    result.status,
                    bundle_id,
                    _json(dict(progress)) if progress is not None else None,
                    now,
                    job_id,
                    claim_token,
                ),
            ).rowcount
            return changed == 1

    def wait(
        self,
        job_id: str,
        claim_token: str,
        reason: str | None,
        *,
        cooldown_seconds: float = WAIT_COOLDOWN_SECONDS,
        state: str = "waiting",
    ) -> bool:
        """Park a job that has not failed: give the attempt back and hold it for a cooldown.

        ``state`` is ``waiting`` for §4.6's provider wait. A job deferred *during intake* has no
        run yet and is parked back in ``intake``: parking it in ``waiting`` would send the next
        claim down the analysis path of a job that has nothing to resume.
        """

        now = self.clock()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT run_id FROM jobs WHERE id=? AND claim_token=?", (job_id, claim_token)
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE analysis_runs SET status='waiting', reason=?, claim_token=NULL "
                "WHERE run_id=? AND claim_token=?",
                (reason, row["run_id"], claim_token),
            )
            connection.execute(
                "UPDATE jobs SET state=?, progress=?, lease_owner=NULL, "
                "lease_until=?, heartbeat_at=NULL, claim_token=NULL, "
                "attempt=MAX(attempt - 1, 0), updated_at=? WHERE id=?",
                (
                    state,
                    _json({"attached": 0, "reason": reason}),
                    now + max(0.0, cooldown_seconds),
                    now,
                    job_id,
                ),
            )
            return True

    def expire_dead_letters(self) -> int:
        """After 24 hours a dead letter has the externally visible terminal state ``failed``."""

        now = self.clock()
        with self.database.write() as connection:
            rows = connection.execute(
                "SELECT id, run_id FROM jobs WHERE state='dead_letter' AND updated_at <= ?",
                (now - 24 * 60 * 60,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE jobs SET state='failed', updated_at=? WHERE id=?", (now, row["id"])
                )
                if row["run_id"] is not None:
                    connection.execute(
                        "UPDATE analysis_runs SET status='failed', "
                        "finished_at=COALESCE(finished_at, ?), claim_token=NULL "
                        "WHERE run_id=? AND status IN ('dead_letter', 'intake', 'waiting', "
                        "'analysis')",
                        (now, row["run_id"]),
                    )
            return len(rows)


IntakeResolver = Callable[["Job", SQLiteCheckpointStore], PreparedIntake]
ServiceRunner = Callable[[RunRequest], RunResult]


class Worker:
    """One queue consumer. It has no HTTP imports and serves no requests."""

    def __init__(
        self,
        database: Database,
        work_root: Path,
        *,
        worker_id: str | None = None,
        local_mode: bool = False,
        options: PipelineOptions | None = None,
        intake_resolver: IntakeResolver | None = None,
        service_runner: ServiceRunner | None = None,
        reservation_seam: ReservationSeam | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        wait_cooldown_seconds: float = WAIT_COOLDOWN_SECONDS,
        egress_id: str = "default",
        clock: Callable[[], float] = time.time,
        checkpoint_before_commit: Callable[[str, CheckpointPhase], None] | None = None,
        checkpoint_after_commit: Callable[[str, CheckpointPhase], None] | None = None,
    ) -> None:
        self.database = database
        self.work_root = Path(work_root).resolve()
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex}"
        self.local_mode = local_mode
        self.options = options or PipelineOptions()
        self.intake_resolver = intake_resolver or self._prepare_intake
        if service_runner is None:
            from id_detector.service import run

            service_runner = run
        self.service_runner = service_runner
        self.reservation_seam = reservation_seam
        self.heartbeat_seconds = heartbeat_seconds
        self.lease_seconds = lease_seconds
        self.wait_cooldown_seconds = wait_cooldown_seconds
        self.egress_id = egress_id
        self.checkpoint_before_commit = checkpoint_before_commit
        self.checkpoint_after_commit = checkpoint_after_commit
        self.queue = JobQueue(database, local_mode=local_mode, clock=clock)
        self._draining = threading.Event()

    def drain(self) -> None:
        """Stop claiming new work; an already-running job retains its heartbeat."""

        self._draining.set()

    def run_forever(
        self,
        *,
        poll_seconds: float = 0.5,
        stop: threading.Event | None = None,
    ) -> None:
        """Consume jobs until drained or stopped, without interrupting the current job."""

        external_stop = stop or threading.Event()
        while not self._draining.is_set() and not external_stop.is_set():
            try:
                self.queue.expire_dead_letters()
                claimed = self.run_once()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                # One job -- or one unreadable row -- must never end the consumer.
                external_stop.wait(poll_seconds)
                continue
            if claimed is None:
                external_stop.wait(poll_seconds)

    def run_once(self) -> Job | None:
        if self._draining.is_set():
            return None
        self.queue.reconcile_attached()
        job = self.queue.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return None
        token = job.token
        # The heartbeat covers the WHOLE claim, intake included: fetching, decoding and hints take
        # far longer than a lease, and an unrenewed intake is reclaimed while it is still running
        # -- the replacement then repeats the fetch and the pair can burn every attempt.
        with self._heartbeat(job.id, token):
            try:
                if job.cancel_requested:
                    self._cancel_before_start(job, token)
                    return self._current(job.id)
                if job.state == "intake" or job.run_id is None:
                    # No run yet, whatever the row says: there is nothing to resume, so this claim
                    # must go through intake rather than down the analysis path.
                    intake = self.intake_resolver(job, self._store(job))
                    decision = self._commit_intake(job, intake)
                    if decision != "analysis":
                        return self._current(job.id)
                    job = self.queue.get(job.id)
                else:
                    intake = self._intake_for_run(job)
                    if not self.queue.begin_analysis(job.id, token):
                        raise StaleClaim("job lease was lost before analysis")
                    job = self.queue.get(job.id)
                self._run_analysis(job, intake, token)
            except JobStoreLocked as exc:
                # Another process holds the source or media lock: not a failure, so the attempt is
                # returned and the job is retried after a cooldown.
                self.queue.wait(
                    job.id,
                    token,
                    f"source_busy: {exc}",
                    cooldown_seconds=self.wait_cooldown_seconds,
                    state=job.state if job.state == "intake" else "waiting",
                )
            except asyncio.CancelledError:
                self._cancel_before_start(job, token)
            except StaleClaim:
                pass  # the replacement owns this job now; write nothing
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                self.queue.fail(job, token, f"{type(exc).__name__}: {exc}")
        return self._current(job.id)

    def _current(self, job_id: str) -> Job | None:
        try:
            return self.queue.get(job_id)
        except (KeyError, ValueError, TargetRefused):
            return None

    @contextmanager
    def _heartbeat(self, job_id: str, claim_token: str) -> Iterator[None]:
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.heartbeat_seconds):
                if not self.queue.heartbeat(job_id, claim_token, lease_seconds=self.lease_seconds):
                    return

        thread = threading.Thread(target=beat, daemon=True, name=f"idea-heartbeat-{job_id}")
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))

    def _options_for(self, job: Job) -> PipelineOptions:
        """This job's pipeline options.

        ``tenant_scope`` must be the job's: intake derives the analysis key from it, and a pipeline
        that derived a different scope would compute a different compatibility identity and defeat
        serving the result back to its owner.
        """

        return replace(self.options, tenant_scope=job.tenant_scope)

    def _store(self, job: Job) -> SQLiteCheckpointStore:
        return SQLiteCheckpointStore(
            self.database,
            self.work_root,
            mode="local" if self.local_mode else "hosted",
            options=self._options_for(job),
            claim_token=job.claim_token,
            before_commit=self.checkpoint_before_commit,
            after_commit=self.checkpoint_after_commit,
        )

    def _prepare_intake(self, job: Job, store: SQLiteCheckpointStore) -> PreparedIntake:
        target_value, source_kind = self._target_value(job.target, store)
        options = self._options_for(job)
        # The same locks the pipeline takes, in the same order and with the same keys: intake
        # fetches and decodes into the shared media directory, so it cannot run beside another
        # process doing the same. They are released before the pipeline re-acquires them.
        source_lock = ProcessLock(
            self.work_root / ".locks" / f"{sha256(target_value.encode('utf-8')).hexdigest()}.lock"
        )
        media_lock: ProcessLock | None = None

        async def prepare() -> PreparedIntake:
            nonlocal media_lock
            retained = _load_cached(self.work_root, target_value)
            ingested = retained or await ingest(target_value, self.work_root)
            media_lock = ProcessLock(ingested.media_dir / ".media.lock")
            media_lock.acquire()
            # A retained result keeps its duration in the bundle manifest after retention has
            # pruned the PCM and the original. Decoding unconditionally here turned a servable
            # stored bundle into three failed attempts and a dead letter.
            retained_manifest = (
                read_bundle_manifest(ingested.source_path.parent) if retained else None
            )
            decoded = None
            if retained_manifest is not None:
                duration_ms = int(retained_manifest["duration_ms"])
            else:
                decoded = await decode(ingested)
                duration_ms = decoded.record.pcm.duration_ms
            hint_result = None
            if not options.no_hints:
                config = options.app_config or AppConfig()
                hint_result = await run_hints(
                    source=ingested.record,
                    duration_ms=duration_ms,
                    media_dir=ingested.media_dir,
                    source_path=ingested.source_path,
                    project_root=options.project_root,
                    manual_tracklist=None,
                    confirmed_mirrors=options.confirmed_mirrors,
                    refresh=options.refresh,
                    disabled_connectors=config.disabled_hint_connectors,
                )
            panako_id = index_identity(options.index_root, options.local_index_label)
            inputs = AnalysisInputs(
                media_key=ingested.record.media_key,
                recipe_id=job.recipe.recipe_id,
                source_kind=source_kind,  # type: ignore[arg-type]
                tenant_scope=job.tenant_scope,
                hints_snapshot_id=hints_snapshot(hint_result.hints if hint_result else ()),
                manual_tracklist_sha256="",
                panako_index_id=panako_id,
            )
            ingest_artefacts = [str(ingested.source_path.resolve())]
            if path_is_file(ingested.original_path):
                ingest_artefacts.append(str(ingested.original_path.resolve()))
            checkpoints: dict[str, Mapping[str, object]] = {
                "ingest": {"artefacts": ingest_artefacts, "state": {}}
            }
            if decoded is not None:
                checkpoints["decode"] = {
                    "artefacts": [
                        str(decoded.pcm_path.resolve()),
                        str(decoded.record_path.resolve()),
                    ],
                    "state": {},
                }
            return PreparedIntake(
                inputs=inputs,
                media_dir=ingested.media_dir.resolve(),
                duration_ms=duration_ms,
                checkpoints=checkpoints,
            )

        source_lock.acquire()
        try:
            return asyncio.run(prepare())
        finally:
            if media_lock is not None:
                media_lock.release()
            source_lock.release()

    def _target_value(
        self, target: PlatformUrl | UploadId | LocalPath, store: SQLiteCheckpointStore
    ) -> tuple[str, str]:
        if isinstance(target, PlatformUrl):
            return validate_platform_url(target.url), "platform"
        if isinstance(target, UploadId):
            return str(store.resolve_upload(validate_upload_id(target.upload_id))), "upload"
        if isinstance(target, LocalPath) and self.local_mode:
            return str(target.path), "local"
        raise TargetRefused("LocalPath is accepted only in local mode")

    def _compatible_bundle(
        self, connection: Any, intake: PreparedIntake, recipe: Recipe
    ) -> tuple[str, str] | None:
        request = CompatibilityRequest(
            intake.inputs, recipe, accept_degraded=False, local=self.local_mode
        )
        rows = connection.execute(
            "SELECT b.bundle_id, b.path, r.run_id FROM result_bundles b "
            "JOIN analysis_runs r ON r.run_id=b.run_id WHERE r.media_key=? "
            "ORDER BY b.created_at DESC",
            (intake.inputs.media_key,),
        ).fetchall()
        config = self.options.app_config or AppConfig()
        for row in rows:
            path = Path(row["path"]).resolve()
            if not path.is_relative_to(self.work_root):
                continue
            manifest = read_bundle_manifest(path)
            if manifest is None:
                continue
            stored = {
                **(manifest.get("compatibility") or {}),
                "status": manifest["status"],
                "achieved": manifest.get("achieved"),
            }
            if serves(stored, request, serve_free_from_deep=config.serve_free_from_deep):
                return row["run_id"], row["bundle_id"]
        return None

    def _commit_intake(self, job: Job, intake: PreparedIntake) -> str:
        now = self.queue.clock()
        token = job.token
        checkpoints = self._validated_checkpoints(intake.checkpoints)
        with self.database.write() as connection:
            owned = connection.execute(
                "SELECT * FROM jobs WHERE id=? AND claim_token=? AND state='intake'",
                (job.id, token),
            ).fetchone()
            if owned is None:
                raise StaleClaim("job lease was lost during intake")
            connection.execute(
                "INSERT OR IGNORE INTO media(media_key, duration_ms, first_seen) VALUES (?, ?, ?)",
                (intake.inputs.media_key, intake.duration_ms, now),
            )
            compatible = self._compatible_bundle(connection, intake, job.recipe)
            if compatible is not None:
                run_id, bundle_id = compatible
                connection.execute(
                    "UPDATE jobs SET run_id=?, state='complete', result_bundle_id=?, "
                    "lease_owner=NULL, lease_until=NULL, heartbeat_at=NULL, claim_token=NULL, "
                    "progress=?, updated_at=? WHERE id=? AND claim_token=?",
                    (
                        run_id,
                        bundle_id,
                        _json({"phase": "intake", "served": True}),
                        now,
                        job.id,
                        token,
                    ),
                )
                return "complete"
            # Attach only to a run somebody is still driving. A run whose job dead-lettered is
            # abandoned: attaching to it would leave the new submission waiting forever.
            attached = connection.execute(
                "SELECT r.run_id FROM analysis_runs r JOIN jobs j ON j.run_id=r.run_id "
                f"WHERE r.analysis_key=? AND r.status IN {_ACTIVE_SQL} "
                f"AND j.state IN {_ACTIVE_SQL} AND j.id<>? "
                "ORDER BY r.started_at LIMIT 1",
                (intake.analysis_key, job.id),
            ).fetchone()
            if attached is not None:
                connection.execute(
                    "UPDATE jobs SET run_id=?, state='waiting', lease_owner=NULL, "
                    "lease_until=NULL, heartbeat_at=NULL, claim_token=NULL, progress=?, "
                    "updated_at=? WHERE id=? AND claim_token=?",
                    (
                        attached["run_id"],
                        _json({"phase": "intake", "attached": True}),
                        now,
                        job.id,
                        token,
                    ),
                )
                return "waiting"
            run_id = job.run_id or uuid.uuid4().hex
            if self.reservation_seam is not None:
                self.reservation_seam.reserve_for_new_run(connection, job, intake)
            connection.execute(
                "INSERT INTO analysis_runs(run_id, analysis_key, media_key, requested_recipe_id, "
                "algorithm_version, adapter_versions, status, tenant_scope, checkpoints, "
                "started_at, analysis_inputs, non_recipe_key, claim_token) "
                "VALUES (?, ?, ?, ?, ?, ?, 'analysis', ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    intake.analysis_key,
                    intake.inputs.media_key,
                    job.recipe.recipe_id,
                    job.recipe.algorithm_version,
                    _json(dict(job.recipe.adapter_versions)),
                    job.tenant_scope,
                    _json(checkpoints),
                    now,
                    _json(vars(intake.inputs)),
                    intake.inputs.non_recipe_key,
                    token,
                ),
            )
            changed = connection.execute(
                "UPDATE jobs SET run_id=?, state='analysis', progress=?, updated_at=? "
                "WHERE id=? AND claim_token=?",
                (
                    run_id,
                    _json({"phase": "intake", "done": 1, "total": 1}),
                    now,
                    job.id,
                    token,
                ),
            ).rowcount
            if changed != 1:
                raise StaleClaim("job lease was lost while committing intake")
        return "analysis"

    @staticmethod
    def _validated_checkpoints(
        checkpoints: Mapping[str, Mapping[str, object]],
    ) -> dict[str, Mapping[str, object]]:
        result: dict[str, Mapping[str, object]] = {}
        for phase, checkpoint in checkpoints.items():
            if phase not in CHECKPOINT_PHASES:
                raise ValueError(f"unknown intake checkpoint phase: {phase}")
            artefacts = checkpoint.get("artefacts", ())
            if not isinstance(artefacts, (list, tuple)) or not all(
                isinstance(path, str) for path in artefacts
            ):
                raise ValueError(f"checkpoint artefacts are not durable: {phase}")
            # Same boundary as the service's checkpoints: contents flushed before the row that
            # names them is committed (§3.4), never merely "the file exists".
            durable = require_durable([Path(path) for path in artefacts])
            state = checkpoint.get("state", {})
            if not isinstance(state, dict):
                raise ValueError(f"checkpoint state is invalid: {phase}")
            result[phase] = {"artefacts": [str(path) for path in durable], "state": state}
        return result

    def _intake_for_run(self, job: Job) -> PreparedIntake:
        if job.run_id is None:
            raise ValueError("analysis job has no run id")
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_runs WHERE run_id=?", (job.run_id,)
            ).fetchone()
        if row is None:
            raise ValueError("analysis job names an unknown run")
        inputs = AnalysisInputs(**json.loads(row["analysis_inputs"]))
        media_dirs = list(self.work_root.glob(f"*/{inputs.media_key}"))
        if not media_dirs:
            raise ValueError("analysis media directory is missing")
        return PreparedIntake(
            inputs=inputs,
            media_dir=media_dirs[0],
            duration_ms=self._duration(inputs.media_key),
            checkpoints=json.loads(row["checkpoints"]),
        )

    def _duration(self, media_key: str) -> int:
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT duration_ms FROM media WHERE media_key=?", (media_key,)
            ).fetchone()
        if row is None:
            raise ValueError("analysis media row is missing")
        return int(row["duration_ms"])

    def _run_analysis(self, job: Job, intake: PreparedIntake, claim_token: str) -> None:
        if job.run_id is None:
            raise ValueError("analysis job has no run id")

        class QueueCancelToken:
            def is_set(inner_self) -> bool:
                del inner_self
                return self.queue.cancel_requested(job.id, claim_token)

        def progress(phase: str, done: int, total: int, message: str) -> None:
            updated = self.queue.update_progress(
                job.id,
                claim_token,
                {"phase": phase, "done": done, "total": total, "message": message},
                lease_seconds=self.lease_seconds,
            )
            if not updated or self.queue.cancel_requested(job.id, claim_token):
                raise asyncio.CancelledError("job cancelled or lease lost")

        options = self._options_for(job)
        app_config = options.app_config or AppConfig()
        journal = SQLiteAttemptJournal(
            self.work_root / ".attempts" / f"{job.run_id}.jsonl",
            database=self.database,
            run_id=job.run_id,
            provider="audd",
            unit_usd_e6=app_config.audd_usd_e6_per_request,
            egress_id=self.egress_id,
            claim_token=claim_token,
        )
        # Repair the projection before trusting it: an event may be on disk in the durable JSONL
        # and missing from SQLite if the previous pass died between the two.
        journal.backfill()
        if options.shazam_breaker is None:
            options = replace(
                options,
                shazam_breaker=LedgerShazamBreaker(
                    getattr(app_config, "shazam_breaker", None),
                    database=self.database,
                    run_id=job.run_id,
                    egress_id=self.egress_id,
                    claim_token=claim_token,
                ),
            )
        store = SQLiteCheckpointStore(
            self.database,
            self.work_root,
            mode="local" if self.local_mode else "hosted",
            options=options,
            claim_token=claim_token,
            before_commit=self.checkpoint_before_commit,
            after_commit=self.checkpoint_after_commit,
        )
        request = RunRequest(
            run_id=job.run_id,
            analysis_key=intake.analysis_key,
            target=job.target,
            recipe=job.recipe,
            accept_degraded=False,
            hints_snapshot_policy="frozen",
            manual_tracklist=None,
            checkpoint_store=store,
            attempt_journal=journal,
            usd_admitter=None,
            progress=progress,
            cancel_token=QueueCancelToken(),
        )
        result = self.service_runner(request)
        if result.status == "waiting":
            self.queue.wait(
                job.id, claim_token, result.reason, cooldown_seconds=self.wait_cooldown_seconds
            )
            return
        bundle_path = None
        if result.bundle_id is not None:
            candidates = list(
                self.work_root.glob(
                    f"*/{intake.inputs.media_key}/present/bundles/{result.bundle_id}"
                )
            )
            if candidates:
                bundle_path = candidates[0]
        self.queue.terminal(job.id, claim_token, result, bundle_path=bundle_path)

    def _recovered_money(self, run_id: str | None) -> tuple[int, int, int]:
        """This run's already-recorded reservation, spend and attempts."""

        if run_id is None:
            return 0, 0, 0
        with self.database.read() as connection:
            row = connection.execute(
                "SELECT usd_e6_reserved, usd_e6_spent, attempts FROM analysis_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return 0, 0, 0
        reserved = int(row["usd_e6_reserved"])
        spent = int(row["usd_e6_spent"])
        attempts = int(row["attempts"])
        state = SQLiteCheckpointStore(self.database, self.work_root).state(run_id, "primary")
        return (
            max(reserved, int(state.get("usd_e6_reserved", 0))),
            max(spent, int(state.get("usd_e6_spent", 0))),
            max(attempts, int(state.get("attempts", 0))),
        )

    def _cancel_before_start(self, job: Job, claim_token: str) -> None:
        """Cancel without erasing what an earlier pass of this run already spent.

        A job killed after a paid primary checkpoint carries a real reservation, real spend and
        real attempts. Settling it with zeros -- because *this* pass did nothing -- would tell the
        owner their money was never charged.
        """

        reserved, spent, attempts = self._recovered_money(job.run_id)
        result = RunResult(
            job.run_id or "", "cancelled", None, None, None, reserved, spent, attempts
        )
        self.queue.terminal(job.id, claim_token, result, bundle_path=None)
