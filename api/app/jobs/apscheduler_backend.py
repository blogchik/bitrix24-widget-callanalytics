"""The v1 `JobBackend`: APScheduler as a bare ticker (§5.9, decision 15).

**This is the only file in the codebase that imports APScheduler.** That is the point
of the Protocol: `jobs/definitions.py` holds the work, this file holds a timer, and the
Celery swap is a sibling of this file rather than a rewrite of anything.

What is scheduled here is deliberately tiny - `tick` every 15 s and the daily
maintenance jobs. `sync_portal` is **never** an APScheduler job: every job id carries
`max_instances=1`, so four portals dispatched under one id would run one and log
"maximum number of running instances reached" for the other three, leaving them leased
and idle until their lease expired and each was charged a crash it never had (design
review, "jobs/apscheduler dispatch and lease-expiry accounting"). Dispatch belongs to
`definitions.tick`, which uses `asyncio.create_task` under a semaphore.

Three settings on every job, each answering a specific failure:

* `max_instances=1` - a tick that overlaps itself would lease portals the previous tick
  is still dispatching;
* `coalesce=True` - a worker that was paused (a slow shutdown, a stopped container, a
  laptop lid) must run ONE catch-up tick, not the two hundred it missed;
* `misfire_grace_time` - a tick that is late by more than half its interval is worth
  skipping, because the next one is already due; a daily purge that is an hour late is
  still worth running.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

# APScheduler 3.x ships no `py.typed`, so these two imports are untyped to mypy.
# Ignored here rather than globally: this is the ONLY file allowed to import them
# (§5.9), so the exemption cannot silently spread.
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore[import-untyped]

from app.jobs.definitions import JOBS
from app.logging import get_logger

__all__ = ["APSchedulerBackend"]

log = get_logger(__name__)

#: Floor and ceiling for the computed grace period, in seconds.
_MIN_GRACE: Final[int] = 10
_MAX_GRACE: Final[int] = 3_600


def _misfire_grace(seconds: float) -> int:
    """Half the interval, clamped - see the module docstring."""
    return int(max(_MIN_GRACE, min(seconds / 2.0, _MAX_GRACE)))


class APSchedulerBackend:
    """An `AsyncIOScheduler` that can only do what `JobBackend` describes."""

    def __init__(self, jobs: Mapping[str, Callable[..., Awaitable[None]]] | None = None) -> None:
        self._jobs: dict[str, Callable[..., Awaitable[None]]] = dict(jobs if jobs is not None else JOBS)
        # Explicit UTC rather than the container's local zone: the schedule is an
        # interval, and a DST-shifting local zone would make the daily jobs jump an
        # hour twice a year for no reason anybody could find in the logs.
        self._scheduler = AsyncIOScheduler(timezone=dt.UTC)
        self._started = False

    # ------------------------------------------------------------------ resolution

    def _resolve(self, name: str) -> Callable[..., Awaitable[None]]:
        job = self._jobs.get(name)
        if job is None:
            # A typo in a schedule call must not start a worker that silently never
            # syncs anything; §10's rule is that misconfiguration fails at startup.
            raise KeyError(f"unknown job {name!r}; known jobs: {sorted(self._jobs)}")
        return job

    def _guarded(self, name: str) -> Callable[[], Awaitable[None]]:
        """Wrap a job so a raise cannot unschedule it or reach APScheduler's logger.

        A periodic job that raises is still scheduled by APScheduler, but the traceback
        goes out through its logger - outside our JSON formatter and its redaction
        filter (§6). Catching here keeps every line on the one path that is guaranteed
        to be redacted, and keeps the timer's next fire unaffected.
        """
        job = self._resolve(name)

        async def run() -> None:
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job failed", extra={"job": name})

        run.__name__ = f"job_{name}"
        return run

    # -------------------------------------------------------------- JobBackend API

    def schedule_periodic(self, name: str, seconds: float) -> None:
        """Register (or replace) one periodic job. Resolves the name immediately."""
        self._scheduler.add_job(
            self._guarded(name),
            IntervalTrigger(seconds=seconds, timezone=dt.UTC),
            id=name,
            name=name,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_misfire_grace(seconds),
            replace_existing=True,
        )
        log.info("job scheduled", extra={"job": name, "interval_s": seconds})

    async def run_now(self, name: str, **kwargs: Any) -> None:
        """Run one job immediately in the caller's task, propagating its result.

        Not routed through the scheduler on purpose: `run_now` is used by tests and by
        an operator triggering a purge, and both want the exception rather than a line
        in a log file.
        """
        await self._resolve(name)(**kwargs)

    async def start(self) -> None:
        """Start the timers. Idempotent; must be called from inside a running loop."""
        if self._started:
            return
        self._scheduler.start()
        self._started = True
        log.info("scheduler started", extra={"jobs": sorted(self._jobs)})

    async def stop(self) -> None:
        """Stop the timers without waiting for in-flight work. Idempotent.

        `wait=False`: the executor's futures are this loop's own tasks, so waiting for
        them from inside that loop is at best pointless and at worst a deadlock past
        the container's grace period. Nothing is lost by not waiting - an interrupted
        visit committed every batch it finished, and the next tick resumes from those
        columns (§5.9 durability).
        """
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False
        log.info("scheduler stopped")
