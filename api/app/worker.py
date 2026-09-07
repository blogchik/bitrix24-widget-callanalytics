"""Worker entrypoint: `python -m app.worker` (§2 - same image as the api, different CMD).

This process owns a lifetime and nothing else. §5.9 puts the schedule in `app.jobs`
(`tick` every 15 s plus the daily maintenance) and the work in `app.jobs.definitions`;
what is left here is: bring logging up, build and start the backend, block until the
container is asked to stop, then shut the scheduler and the connection pool down.

WHY it blocks on an `asyncio.Event` rather than on the scheduler: APScheduler's
`AsyncIOScheduler` runs on the loop it was started in and never blocks, so something
has to hold the loop open. An `Event` set from a signal handler is the smallest thing
that also gives SIGTERM - how Docker asks for a stop - a clean path: without it the
runtime is SIGKILLed after the grace period, and every pooled connection shows up in
the Postgres log as a reset.

WHY nothing is awaited before `stop.wait()`: an interrupted sync visit loses nothing.
Every batch commits its rows and its cursor together (§5.3) and the lease expires on
its own, so the correct shutdown is a fast one - the next tick, in this container or
its replacement, resumes from committed state (§5.9 durability).

WHY `SCHEDULER_INLINE` makes this process idle instead of exiting: §11 assumption 13
allows the deployment to collapse the worker into the api container. In that mode the
api's lifespan owns the schedule (`app.jobs.job_layer`), and a worker container that
also scheduled `tick` would double every timer. Exiting would fight the restart policy,
so it stays up, healthy and deliberately empty, and says so in the log.
"""

from __future__ import annotations

import asyncio
import signal

from app.config import settings
from app.db.engine import dispose_engine
from app.jobs import DEFAULT_SCHEDULE, job_layer
from app.jobs.protocol import JobBackend
from app.logging import get_logger, setup_logging
from app.sync.lease import WORKER_ID

_log = get_logger("app.worker")


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM is how Docker asks for a stop; without this the runtime is SIGKILLed."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows dev machines: no loop signal handlers. `call_soon_threadsafe`
            # because a C-level handler does not run inside the loop.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def run() -> None:
    """Start the job layer (unless the api owns it) and block until asked to stop."""
    setup_logging()
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    # `WORKER_ID` (host:pid) is what `portal_sync.lease_owner` records, so it is the
    # first thing to log: it is how a support engineer maps a stuck lease to a container.
    _log.info(
        "worker starting",
        extra={
            "worker_id": WORKER_ID,
            "job_backend": settings.job_backend,
            "scheduler_inline": settings.scheduler_inline,
            "portal_concurrency": settings.global_portal_concurrency,
        },
    )

    if settings.scheduler_inline:
        _log.warning(
            "SCHEDULER_INLINE is set: the api process owns the schedule, so this "
            "worker stays idle (running it too would double every timer)",
            extra={"worker_id": WORKER_ID},
        )
        try:
            await stop.wait()
        finally:
            await dispose_engine()
        return

    try:
        async with job_layer() as backend:
            _log.info(
                "worker started",
                extra={
                    "worker_id": WORKER_ID,
                    "schedule": [name for name, _ in DEFAULT_SCHEDULE],
                },
            )
            await stop.wait()
            _log.info("worker stopping", extra={"worker_id": WORKER_ID})
            _ = backend  # the context manager stops it on the way out
    finally:
        await dispose_engine()
        _log.info("worker stopped", extra={"worker_id": WORKER_ID})


async def scheduler_backend() -> JobBackend:
    """The inline hook for `SCHEDULER_INLINE=1` (§11 assumption 13).

    Exposed so the api's lifespan can adopt the same schedule with one `async with
    job_layer():` instead of re-deriving it; there is exactly one definition of what
    runs periodically, in `app.jobs.DEFAULT_SCHEDULE`.
    """
    from app.jobs import start_backend

    return await start_backend()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
