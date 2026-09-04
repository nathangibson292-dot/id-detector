"""Failure-injectable durable submission protocol used by adapter conformance tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from id_detector.jobs import AsyncJobStore, Job

Attempt = Callable[[], Awaitable[None]]
FailureHook = Callable[[str], None]


class DurableAdapter(Protocol):
    async def reconcile(self, cache_key: str, on_attempt: Attempt) -> str | None: ...

    async def submit(self, cache_key: str, on_attempt: Attempt) -> str: ...

    async def poll(self, remote_ref: str, on_attempt: Attempt) -> str: ...


async def execute_durable_submission(
    *,
    store: AsyncJobStore,
    job: Job,
    owner: str,
    cache_key: str,
    adapter: DurableAdapter,
    failure_hook: FailureHook | None = None,
) -> None:
    """Execute or resume one scanner-like job without ever blindly double-submitting it."""

    hook = failure_hook or (lambda _point: None)
    await store.submission_started(job.id, owner)
    remote_ref = job.remote_ref
    if remote_ref is None:
        hook("before_network")
        remote_ref = await adapter.reconcile(
            cache_key, lambda: store.begin_physical_attempt(job.id)
        )
    if remote_ref is None:
        try:
            remote_ref = await adapter.submit(
                cache_key, lambda: store.begin_physical_attempt(job.id)
            )
        except BaseException:
            hook("during_upload")
            raise
        hook("after_acceptance")
        await store.persist_remote_ref(job.id, remote_ref)
        hook("after_remote_id_persistence")
    await store.submitted(job.id, remote_ref)
    try:
        outcome = await adapter.poll(remote_ref, lambda: store.begin_physical_attempt(job.id))
    except BaseException:
        hook("during_polling")
        raise
    if outcome not in {"succeeded", "no_match"}:
        raise ValueError(f"unexpected durable-adapter outcome: {outcome}")
    await store.finish(job.id, outcome, result_path=f"remote:{remote_ref}")
