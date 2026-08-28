"""Tests for fact_inventory.server.background_job.lock."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fact_inventory.infrastructure.db.models import BackgroundJobLock
from fact_inventory.infrastructure.db.repositories import BackgroundJobLockRepository
from fact_inventory.server.background_job import (
    BackgroundJobLeaseLostError,
    run_exclusive_background_job,
)
from fact_inventory.server.background_job.lock import _heartbeat
from tests.fixtures.app import get_sqlalchemy_config

DEFAULT_INTERVAL_SECONDS = 60


async def _get_lock(session: AsyncSession, name: str) -> BackgroundJobLock | None:
    """Fetch a lock row by name."""
    result = await session.execute(
        select(BackgroundJobLock).where(BackgroundJobLock.name == name)
    )
    return result.scalar_one_or_none()


async def assert_lock_count(session: AsyncSession, expected: int) -> None:
    """Assert the number of rows in background_job_lock matches expected."""
    result = await session.execute(select(func.count()).select_from(BackgroundJobLock))
    count = result.scalar()
    if count is None:
        count = 0
    assert count == expected, f"Expected {expected} locks, got {count}"


async def _release_all_locks(session: AsyncSession) -> None:
    """Release all locks by deleting all rows."""
    await session.execute(delete(BackgroundJobLock))
    await session.commit()


async def test_run_exclusive_background_job_runs_work_when_lock_free(
    test_db_session: AsyncSession,
    test_client,
) -> None:
    """The helper runs work and returns its result when the lock is free."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    async def work() -> int:
        return 42

    result = await run_exclusive_background_job(
        alchemy_config=alchemy_config,
        name="run-free-job",
        interval_seconds=DEFAULT_INTERVAL_SECONDS,
        work=work,
    )

    assert result == 42
    await assert_lock_count(test_db_session, 0)


async def test_run_exclusive_background_job_skips_when_lock_held(
    test_db_session: AsyncSession,
    test_client,
) -> None:
    """The helper returns 0 when another invocation holds the lock."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    acquired = await BackgroundJobLockRepository(session=test_db_session).acquire(
        "run-skip-job", DEFAULT_INTERVAL_SECONDS
    )
    assert acquired is not None

    async def work() -> int:
        return 99

    result = await run_exclusive_background_job(
        alchemy_config=alchemy_config,
        name="run-skip-job",
        interval_seconds=DEFAULT_INTERVAL_SECONDS,
        work=work,
    )

    assert result == 0
    await _release_all_locks(test_db_session)


async def test_run_exclusive_background_job_releases_lock_on_exception(
    test_db_session: AsyncSession,
    test_client,
) -> None:
    """The lock is released even when the work coroutine raises."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    async def failing_work() -> int:
        msg = "boom"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        await run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="run-exception-job",
            interval_seconds=DEFAULT_INTERVAL_SECONDS,
            work=failing_work,
        )

    await assert_lock_count(test_db_session, 0)


async def test_run_exclusive_background_job_heartbeat_refreshes_lock(
    test_db_session: AsyncSession,
    test_client,
) -> None:
    """The heartbeat refreshes acquired_at while work is still running."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    started = asyncio.Event()
    stop_work = asyncio.Event()

    async def long_work() -> int:
        started.set()
        await stop_work.wait()
        return 42

    task = asyncio.create_task(
        run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="heartbeat-job",
            interval_seconds=1,
            work=long_work,
        )
    )

    await asyncio.wait_for(started.wait(), timeout=2)
    lock_before = await _get_lock(test_db_session, "heartbeat-job")
    assert lock_before is not None
    original_acquired_at = lock_before.acquired_at

    await asyncio.sleep(0.7)
    await test_db_session.refresh(lock_before)

    assert lock_before.acquired_at > original_acquired_at

    stop_work.set()
    result = await asyncio.wait_for(task, timeout=2)
    assert result == 42
    await assert_lock_count(test_db_session, 0)


@pytest.mark.parametrize("bad_interval", [0, -1])
async def test_run_exclusive_rejects_non_positive_interval(
    test_db_session: AsyncSession,
    test_client,
    bad_interval: int,
) -> None:
    """A non-positive interval is rejected: it makes every lock immediately stale."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    async def work() -> int:
        return 7

    with pytest.raises(ValueError, match="interval_seconds must be positive"):
        await run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="bad-interval-job",
            interval_seconds=bad_interval,
            work=work,
        )

    await assert_lock_count(test_db_session, 0)


async def test_run_exclusive_recovers_when_heartbeat_join_times_out(
    test_db_session: AsyncSession,
    test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heartbeat that does not join within the timeout is tolerated.

    Simulates a heartbeat stuck in a database call: the heartbeat join times
    out, the timeout is suppressed, and the lock is still released.
    """
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    async def timed_out(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(
        "fact_inventory.server.background_job.lock.asyncio.wait_for", timed_out
    )

    async def work() -> int:
        return 9

    result = await run_exclusive_background_job(
        alchemy_config=alchemy_config,
        name="stuck-heartbeat-job",
        interval_seconds=DEFAULT_INTERVAL_SECONDS,
        work=work,
    )

    assert result == 9
    await assert_lock_count(test_db_session, 0)


async def test_run_exclusive_releases_lock_when_heartbeat_task_never_starts(
    test_db_session: AsyncSession,
    test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If creating the heartbeat task fails, the acquired lock is still released."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    real_create_task = asyncio.create_task

    def failing_create_task(coro, *args, **kwargs):
        # Fail only the heartbeat task; other create_task calls (e.g. the
        # advanced_alchemy after_commit listener on every DB commit) must
        # still work, or the acquire commit above would fail too.
        if str(kwargs.get("name", "")).endswith("-heartbeat"):
            coro.close()
            msg = "cannot create heartbeat task"
            raise RuntimeError(msg)
        return real_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(
        "fact_inventory.server.background_job.lock.asyncio.create_task",
        failing_create_task,
    )

    async def work() -> int:
        return 3

    with pytest.raises(RuntimeError, match="cannot create heartbeat task"):
        await run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="no-heartbeat-job",
            interval_seconds=DEFAULT_INTERVAL_SECONDS,
            work=work,
        )

    await assert_lock_count(test_db_session, 0)


async def test_run_exclusive_heartbeat_warns_when_lock_disappears(
    test_db_session: AsyncSession,
    test_client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The heartbeat cancels work and logs a warning if the lock row vanishes."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    started = asyncio.Event()
    stop_work = asyncio.Event()

    async def long_work() -> int:
        started.set()
        await stop_work.wait()
        return 0

    task = asyncio.create_task(
        run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="disappear-job",
            interval_seconds=1,
            work=long_work,
        )
    )

    await asyncio.wait_for(started.wait(), timeout=2)
    lock = await _get_lock(test_db_session, "disappear-job")
    assert lock is not None
    async with alchemy_config.get_session() as session:
        await BackgroundJobLockRepository(session=session).release(
            "disappear-job", lock.owner_token
        )

    with caplog.at_level("WARNING"), pytest.raises(BackgroundJobLeaseLostError):
        await asyncio.wait_for(task, timeout=2)

    assert "was lost" in caplog.text
    await assert_lock_count(test_db_session, 0)


async def test_run_exclusive_heartbeat_failure_past_stale_window_cancels_work(
    test_db_session: AsyncSession,
    test_client,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A heartbeat refresh outage past the stale window cancels work."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    started = asyncio.Event()

    async def failing_refresh(self, _name: str, _owner_token) -> None:
        msg = "heartbeat refresh failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        BackgroundJobLockRepository,
        "refresh",
        failing_refresh,
    )

    stop_work = asyncio.Event()

    async def long_work() -> int:
        started.set()
        await stop_work.wait()
        return 0

    task = asyncio.create_task(
        run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="failing-refresh-job",
            interval_seconds=1,
            work=long_work,
        )
    )

    await asyncio.wait_for(started.wait(), timeout=2)
    with caplog.at_level("WARNING"):
        await asyncio.sleep(2.1)

    assert "Heartbeat failed for background job" in caplog.text
    assert "treating as lease loss" in caplog.text

    with pytest.raises(BackgroundJobLeaseLostError):
        await asyncio.wait_for(task, timeout=2)


async def test_run_exclusive_release_failure_is_logged(
    test_db_session: AsyncSession,
    test_client,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure to release the lock is logged but does not hide the work result."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    async def failing_release(self, _name: str, _owner_token) -> None:
        msg = "release failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        BackgroundJobLockRepository,
        "release",
        failing_release,
    )

    async def work() -> int:
        return 5

    with caplog.at_level("ERROR"):
        result = await run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="failing-release-job",
            interval_seconds=DEFAULT_INTERVAL_SECONDS,
            work=work,
        )

    assert result == 5
    assert "Failed to release background job lock" in caplog.text


async def test_run_exclusive_cancels_work_when_lease_lost(
    test_db_session: AsyncSession,
    test_client,
) -> None:
    """Deleting the lock row while work runs cancels the work task."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    started = asyncio.Event()
    cancelled = asyncio.Event()
    stop_work = asyncio.Event()

    async def long_work() -> int:
        started.set()
        try:
            await stop_work.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return 0

    task = asyncio.create_task(
        run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="lease-lost-cancel-job",
            interval_seconds=1,
            work=long_work,
        )
    )

    await asyncio.wait_for(started.wait(), timeout=2)
    lock = await _get_lock(test_db_session, "lease-lost-cancel-job")
    assert lock is not None

    async with alchemy_config.get_session() as session:
        await BackgroundJobLockRepository(session=session).release(
            "lease-lost-cancel-job", lock.owner_token
        )

    with pytest.raises(BackgroundJobLeaseLostError):
        await asyncio.wait_for(task, timeout=2)

    assert cancelled.is_set()
    await assert_lock_count(test_db_session, 0)


async def test_run_exclusive_cancels_work_when_heartbeat_outage_exceeds_stale_window(
    test_db_session: AsyncSession,
    test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A heartbeat refresh outage past the stale window cancels work."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    started = asyncio.Event()
    cancelled = asyncio.Event()
    stop_work = asyncio.Event()

    async def failing_refresh(self, _name: str, _owner_token) -> None:
        msg = "heartbeat refresh failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        BackgroundJobLockRepository,
        "refresh",
        failing_refresh,
    )

    async def long_work() -> int:
        started.set()
        try:
            await stop_work.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return 0

    task = asyncio.create_task(
        run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="heartbeat-outage-cancel-job",
            interval_seconds=1,
            work=long_work,
        )
    )

    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(2.1)

    with pytest.raises(BackgroundJobLeaseLostError):
        await asyncio.wait_for(task, timeout=2)

    assert cancelled.is_set()
    await assert_lock_count(test_db_session, 0)


async def test_run_exclusive_two_worker_takeover_cancels_first_worker(
    test_db_session: AsyncSession,
    test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second worker taking over a stale lock cancels the first worker."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    await _release_all_locks(test_db_session)

    worker1_started = asyncio.Event()
    worker1_cancelled = asyncio.Event()
    stop_work = asyncio.Event()

    async def worker1_work() -> int:
        worker1_started.set()
        try:
            await stop_work.wait()
        except asyncio.CancelledError:
            worker1_cancelled.set()
            raise
        return 0

    # Start worker1 with a 1s interval (heartbeat every 0.5s, stale after 2s).
    worker1_task = asyncio.create_task(
        run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="takeover-job",
            interval_seconds=1,
            work=worker1_work,
        )
    )
    await asyncio.wait_for(worker1_started.wait(), timeout=2)

    lock = await _get_lock(test_db_session, "takeover-job")
    assert lock is not None
    worker1_token = lock.owner_token

    block_worker1 = asyncio.Event()
    real_refresh = BackgroundJobLockRepository.refresh

    async def patched_refresh(
        self, name: str, owner_token: UUID
    ) -> BackgroundJobLock | None:
        if owner_token == worker1_token and block_worker1.is_set():
            raise RuntimeError("worker1 heartbeat blocked")
        return await real_refresh(self, name, owner_token)

    monkeypatch.setattr(
        BackgroundJobLockRepository,
        "refresh",
        patched_refresh,
    )

    # Block worker1's heartbeat and wait past the stale window.
    block_worker1.set()
    await asyncio.sleep(2.1)

    async def worker2_work() -> int:
        return 99

    worker2_task = asyncio.create_task(
        run_exclusive_background_job(
            alchemy_config=alchemy_config,
            name="takeover-job",
            interval_seconds=1,
            work=worker2_work,
        )
    )

    result2 = await asyncio.wait_for(worker2_task, timeout=2)
    assert result2 == 99

    # Unblock worker1 so its next heartbeat detects the takeover.
    block_worker1.clear()

    with pytest.raises(BackgroundJobLeaseLostError):
        await asyncio.wait_for(worker1_task, timeout=2)

    assert worker1_cancelled.is_set()


async def test_heartbeat_exits_immediately_when_stop_event_already_set(
    test_client,
) -> None:
    """The heartbeat exits immediately when stop_event is already set."""
    alchemy_config = get_sqlalchemy_config(test_client.app)
    stop_event = asyncio.Event()
    lease_lost_event = asyncio.Event()
    stop_event.set()

    await _heartbeat(
        alchemy_config=alchemy_config,
        name="stopped-heartbeat-job",
        owner_token=uuid4(),
        interval_seconds=DEFAULT_INTERVAL_SECONDS,
        stop_event=stop_event,
        lease_lost_event=lease_lost_event,
    )

    assert not lease_lost_event.is_set()
