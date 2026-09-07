"""Job-layer entry point: pick a backend, register the §5.9 schedule, start it.

`get_backend()` is where `JOB_BACKEND` becomes a decision, and it is the ONLY place
that names a backend module - which is what keeps decision 15's promise concrete: the
Celery swap is `jobs/celery_backend.py` plus one branch here, with the job functions,
the tables, the cursors, the lease and the fencing untouched.

The schedule itself lives here rather than in `worker.py` because §5.9 fixes it ("only
`tick` every 15 s and the daily purges") and because two processes may install it: the
`worker` container normally, or the api process when `SCHEDULER_INLINE=1` collapses the
deployment into a single container (§11 assumption 13). One definition means the two
cannot drift.

Imports are lazy on purpose: importing `app.jobs` must not pull APScheduler into the
api image's request path, and a Celery deployment must not need APScheduler installed
at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

from app.config import settings
from app.jobs.definitions import JOBS
from app.jobs.protocol import JobBackend

__all__ = [
    "DEFAULT_SCHEDULE",
    "JOBS",
    "JobBackend",
    "get_backend",
    "job_layer",
    "start_backend",
]

#: §5.9: the tick is a ticker, not a queue - 15 s is short enough that a portal whose
#: `next_run_at` just came due waits seconds, and long enough that an idle deployment
#: costs four small queries a minute.
TICK_SECONDS: Final[float] = 15.0
DAILY_SECONDS: Final[float] = 86_400.0

#: Everything the scheduler knows. `sync_portal` and `purge_portal` are deliberately
#: absent: they are dispatched by `tick` from database state, never by a timer.
DEFAULT_SCHEDULE: Final[tuple[tuple[str, float], ...]] = (
    ("tick", TICK_SECONDS),
    ("purge_rest_log", DAILY_SECONDS),
    ("purge_crm_contexts", DAILY_SECONDS),
    # §5.8's fallback uninstall. Daily like the retention jobs, and for the same
    # reason: it is a sweep over committed state that nothing else triggers.
    ("sweep_inferred_uninstalls", DAILY_SECONDS),
)


def get_backend() -> JobBackend:
    """Build the backend named by `JOB_BACKEND` (§5.9).

    `config.py` already restricts the value to the two known names, so the failure here
    is not "bad configuration" but "the file that name promises does not exist yet" -
    raised loudly rather than silently degrading to APScheduler, which would leave a
    Celery deployment running a second, unexpected scheduler.
    """
    if settings.job_backend == "apscheduler":
        from app.jobs.apscheduler_backend import APSchedulerBackend

        return APSchedulerBackend()
    raise NotImplementedError(
        f"JOB_BACKEND={settings.job_backend!r} has no backend module yet; §5.9 expects "
        "jobs/celery_backend.py to provide one"
    )


async def start_backend(
    schedule: Sequence[tuple[str, float]] = DEFAULT_SCHEDULE,
) -> JobBackend:
    """Build the backend, register `schedule`, and start the timers."""
    backend = get_backend()
    for name, seconds in schedule:
        backend.schedule_periodic(name, seconds)
    await backend.start()
    return backend


@asynccontextmanager
async def job_layer(
    schedule: Sequence[tuple[str, float]] = DEFAULT_SCHEDULE,
) -> AsyncIterator[JobBackend]:
    """`start_backend` with a guaranteed `stop()` - for `worker.py` and, when
    `SCHEDULER_INLINE=1`, for the api's lifespan."""
    backend = await start_backend(schedule)
    try:
        yield backend
    finally:
        await backend.stop()
