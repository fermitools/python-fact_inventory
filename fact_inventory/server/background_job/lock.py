"""Distributed lock helper for background jobs.

Provides ``run_exclusive_background_job``, which ensures that only one
instance of a given background job runs at a time across all workers and
servers that share the same database. The lock row is refreshed by a
heartbeat while the job is active and deleted when the job finishes. An owner
token ensures that an old worker cannot refresh or release a replacement
worker's lock.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

from advanced_alchemy.extensions.litestar import SQLAlchemyAsyncConfig

from fact_inventory.infrastructure.db.repositories.background_job_lock import (
    BackgroundJobLockRepository,
)

__all__ = ["BackgroundJobLeaseLostError", "run_exclusive_background_job"]

logger = logging.getLogger(__name__)


class BackgroundJobLeaseLostError(Exception):
    """Raised when a background job loses its distributed lock while running."""

    def __init__(self, name: str) -> None:
        """Initialize with the job name that lost its lock.

        Parameters
        ----------
        name : str
            Job name that lost the lock.
        """
        super().__init__(f"Background job {name} lost its exclusive lock")
        self.name = name


async def _heartbeat(  # noqa: PLR0913, PLR0917
    alchemy_config: SQLAlchemyAsyncConfig,
    name: str,
    owner_token: UUID,
    interval_seconds: int,
    stop_event: asyncio.Event,
    lease_lost_event: asyncio.Event,
) -> None:
    """Periodically refresh the lock row while the job is running.

    The heartbeat wakes every ``interval_seconds / 2`` and updates
    ``acquired_at`` to the current UTC time. Refreshes are conditional on
    the owner's token, preventing an old worker from changing a replacement
    worker's lock.

    If the lock is taken over by another worker, or if the heartbeat is
    unable to refresh for longer than the stale-lock window, the heartbeat
    sets ``lease_lost_event`` so the active work task can be cancelled.

    Parameters
    ----------
    alchemy_config : SQLAlchemyAsyncConfig
        Advanced Alchemy config used to open lock sessions.
    name : str
        Job name used as the lock key.
    interval_seconds : int
        Configured run interval for the job. The heartbeat fires at half
        this interval.
    stop_event : asyncio.Event
        Event set by the caller when the job finishes, signalling the
        heartbeat to exit.
    lease_lost_event : asyncio.Event
        Event set when this worker no longer owns the lock.
    """
    heartbeat_interval = interval_seconds / 2
    stale_threshold_seconds = 2 * interval_seconds
    last_successful_refresh = time.monotonic()

    while True:
        if stop_event.is_set():
            return

        try:
            async with asyncio.timeout(heartbeat_interval):
                await stop_event.wait()
        except TimeoutError:
            pass
        else:
            return

        try:
            async with alchemy_config.get_session() as session:
                refreshed = await BackgroundJobLockRepository(session=session).refresh(
                    name, owner_token
                )
                if refreshed is None:
                    logger.warning("Background job lock %s was lost", name)
                    lease_lost_event.set()
                    return
                last_successful_refresh = time.monotonic()
        except Exception:
            logger.exception("Heartbeat failed for background job %s", name)
            if time.monotonic() - last_successful_refresh > stale_threshold_seconds:
                logger.warning(
                    "Background job heartbeat for %s has been unable to refresh "
                    "for longer than the stale-lock window; treating as lease loss",
                    name,
                )
                lease_lost_event.set()
                return


async def run_exclusive_background_job(
    alchemy_config: SQLAlchemyAsyncConfig,
    name: str,
    interval_seconds: int,
    work: Callable[[], Coroutine[Any, Any, int]],
) -> int:
    """Run ``work`` if no other instance of ``name`` currently holds the lock.

    Acquires a database-backed lock, starts a heartbeat refresh task, runs
    the supplied coroutine, and releases its own lock in ``finally``. If the
    lock cannot be acquired because another invocation is running, the
    function returns ``0`` immediately.

    If the heartbeat detects that the lock was lost or that it has been
    unable to refresh for longer than the stale-lock window, the active work
    task is cancelled and :class:`BackgroundJobLeaseLostError` is raised.

    Parameters
    ----------
    alchemy_config : SQLAlchemyAsyncConfig
        Advanced Alchemy config used to open lock sessions.
    name : str
        Job name used as the lock key.
    interval_seconds : int
        Configured run interval for the job. Must be positive. Staleness is
        twice this value, and the heartbeat fires at half this value.
    work : Callable[[], Coroutine[Any, Any, int]]
        Coroutine that performs the actual job work and returns the number
        of records processed.

    Returns
    -------
    int
        The result of ``work`` when the lock is acquired, or ``0`` when the
        job is skipped because another instance is running.

    Raises
    ------
    ValueError
        If ``interval_seconds`` is not positive. A non-positive interval makes
        every existing lock immediately stale, defeating mutual exclusion.
    BackgroundJobLeaseLostError
        If the lock is lost while ``work`` is still running.

    Notes
    -----
    Staleness is ``2 * interval_seconds``. If a worker is killed without
    releasing the lock (for example a hard kill that prevents the ``finally``
    block from completing), the lock row blocks the job until it goes stale.
    With the default 20-hour interval that window is roughly 40 hours; choose
    the interval with that takeover delay in mind.
    """
    owner_token: UUID | None = None
    if interval_seconds <= 0:
        msg = f"interval_seconds must be positive, got {interval_seconds}"
        raise ValueError(msg)

    async with alchemy_config.get_session() as session:
        lock = await BackgroundJobLockRepository(session=session).acquire(
            name, interval_seconds
        )
        if lock is not None:
            owner_token = lock.owner_token

    if owner_token is None:
        logger.info("Background job %s already running, skipping", name)
        return 0

    stop_event = asyncio.Event()
    lease_lost_event = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    work_task: asyncio.Task[int] | None = None
    lease_lost_wait_task: asyncio.Task[bool] | None = None

    try:
        work_task = asyncio.create_task(work(), name=f"{name}-work")
        heartbeat_task = asyncio.create_task(
            _heartbeat(
                alchemy_config,
                name,
                owner_token,
                interval_seconds,
                stop_event,
                lease_lost_event,
            ),
            name=f"{name}-heartbeat",
        )
        lease_lost_wait_task = asyncio.create_task(
            lease_lost_event.wait(), name=f"{name}-lease-wait"
        )

        assert work_task is not None  # noqa: S101
        assert lease_lost_wait_task is not None  # noqa: S101
        done, _pending = await asyncio.wait(
            [work_task, lease_lost_wait_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if work_task in done:
            return work_task.result()

        # Lease was lost before work completed.
        work_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await work_task
        raise BackgroundJobLeaseLostError(name)
    finally:
        stop_event.set()
        if lease_lost_wait_task is not None and not lease_lost_wait_task.done():
            lease_lost_wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lease_lost_wait_task
        if heartbeat_task is not None:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(heartbeat_task, timeout=5)
        try:
            async with alchemy_config.get_session() as session:
                await BackgroundJobLockRepository(session=session).release(
                    name, owner_token
                )
        except Exception:
            logger.exception("Failed to release background job lock %s", name)
