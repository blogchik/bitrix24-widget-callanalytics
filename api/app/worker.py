"""Worker entrypoint: `python -m app.worker` (§2 — same image as the api, different CMD).

WHY it blocks on an `asyncio.Event` instead of exiting when there is nothing to do:
the container must stay up and healthy from milestone 1 so compose, restart policies
and log plumbing are proven before any job exists. §5.9's backend schedules `tick`
(every 15 s) and the daily purges on its own timer; this process only owns the
lifetime — build the backend, keep the loop alive, shut the pool down cleanly.

WHY the job layer is imported by name rather than at the top of the file: `app.jobs`
lands in milestone 4. Probing for it keeps today's container startable and means the
only change then is uncommenting the schedule calls below — the shape does not move.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import signal
from typing import Any

from app.config import settings
from app.db.engine import dispose_engine
from app.logging import get_logger, setup_logging

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


async def _build_backend() -> Any | None:
    """Return the §5.9 `JobBackend`, or None while `app/jobs/` does not exist yet."""
    try:
        if importlib.util.find_spec("app.jobs") is None:
            return None
    except ModuleNotFoundError:
        return None
    module = importlib.import_module("app.jobs")
    get_backend = getattr(module, "get_backend", None)
    if get_backend is None:
        return None
    backend = get_backend()
    if inspect.isawaitable(backend):
        backend = await backend
    return backend


async def run() -> None:
    setup_logging()
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    backend = await _build_backend()
    if backend is None:
        _log.info("job layer not built yet")
    else:
        _log.info("job backend ready", extra={"job_backend": settings.job_backend})
        # TODO(milestone 4): the only lines that change here. §5.9 schedules `tick`
        # every 15 s and the daily retention purges; nothing else is periodic.
        # backend.schedule_periodic("tick", 15)
        # backend.schedule_periodic("purge_rest_log", 86400)
        # backend.schedule_periodic("purge_crm_contexts", 86400)

    _log.info("worker started")
    try:
        await stop.wait()
    finally:
        _log.info("worker stopping")
        await dispose_engine()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
