"""The one interface a scheduler must satisfy (§5.9, decision 15).

WHY a `Protocol` and not a base class: §5.9's "Celery swap" is a *file*, not a
refactor - "add `jobs/celery_backend.py` where beat schedules the same periodic names
and a task wraps each definition; set `JOB_BACKEND=celery`. Job functions, tables,
cursors, lease and fencing are unchanged." A Protocol keeps that promise checkable: a
Celery backend cannot import an APScheduler base class, but it can structurally satisfy
these four methods, and `jobs/definitions.py` never learns which one is running.

WHY the surface is this small: decision 15 says the scheduler is *only a ticker*. It
owns no queue, no retry policy and no state - every unit of work is derived from
`portal_sync` / `portals` columns, so a scheduler that forgets everything on restart
loses nothing. Anything richer here (job payloads, result handling, retries) would be a
second, weaker copy of the durability model the database already provides, and the two
would drift.

`schedule_periodic` is deliberately synchronous: registering a timer touches nothing
that can block, and making it awaitable would invite a backend to do IO there - at
which point a failed schedule call would leave the worker running with half its jobs.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["JobBackend"]


@runtime_checkable
class JobBackend(Protocol):
    """A ticker that can run named jobs periodically and on demand.

    Names are the keys of `app.jobs.definitions.JOBS`. Passing a name rather than a
    callable is what lets a distributed backend (Celery) resolve the function in the
    worker process instead of pickling it out of this one.
    """

    def schedule_periodic(self, name: str, seconds: float) -> None:
        """Run `name` every `seconds`, at most one instance at a time.

        Overlap suppression is part of the contract, not a backend detail: `tick()`
        runs every 15 s while a sync visit can last minutes, and two concurrent ticks
        leasing the same portals would be pure waste (the lease makes it *safe*, never
        useful).
        """
        ...

    async def run_now(self, name: str, **kwargs: Any) -> None:
        """Run `name` once, immediately, awaiting it.

        Only ever called with primitive keyword arguments (`portal_id=42`), so the same
        call is expressible as a Celery `.delay()` without a serializer of our own.
        """
        ...

    async def start(self) -> None:
        """Begin firing the registered timers. Idempotent."""
        ...

    async def stop(self) -> None:
        """Stop firing and release the backend's resources. Idempotent.

        Must not wait for in-flight sync visits: a container stop has a SIGKILL behind
        it, and an interrupted visit is resumed from committed state by the next tick
        (§5.9 durability) - whereas a shutdown that blocks past the grace period turns
        a clean stop into a kill and leaves the lease to expire.
        """
        ...
