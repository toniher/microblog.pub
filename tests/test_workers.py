import asyncio
import time
from typing import cast

import pytest

from app.database import AsyncSession
from app.utils.workers import Worker


class _FakeWorker(Worker[int]):
    _idle_sleep = 0.02

    def __init__(self, batch_size: int) -> None:
        super().__init__()
        self.batch_size = batch_size
        self.queue: list[int] = []
        self.processed: list[list[int]] = []
        self.idle_sleep_calls = 0
        self.stop_when_drained = False

    async def get_next_messages(
        self, db_session: AsyncSession, limit: int
    ) -> list[int]:
        batch, self.queue = self.queue[:limit], self.queue[limit:]
        if not batch and self.stop_when_drained:
            self._stop_event.set()
        return batch

    async def process_messages(
        self, db_session: AsyncSession, messages: list[int]
    ) -> None:
        self.processed.append(messages)

    async def _sleep(self, seconds: float) -> None:
        self.idle_sleep_calls += 1
        await super()._sleep(seconds)


@pytest.mark.asyncio
async def test_main_loop_processes_full_batches_without_idling() -> None:
    worker = _FakeWorker(batch_size=3)
    worker.queue = list(range(6))
    worker.stop_when_drained = True

    started = time.monotonic()
    await asyncio.wait_for(worker._main_loop(cast(AsyncSession, None)), timeout=2)
    elapsed = time.monotonic() - started

    assert worker.processed == [[0, 1, 2], [3, 4, 5]]
    # Draining two full batches back-to-back must not incur a real
    # `_idle_sleep` wait; the only `_sleep` call is the already-interrupted
    # one made once the queue is empty.
    assert elapsed < worker._idle_sleep


@pytest.mark.asyncio
async def test_main_loop_idles_on_partial_batch() -> None:
    worker = _FakeWorker(batch_size=5)
    worker.queue = [0, 1]
    worker.stop_when_drained = True

    await asyncio.wait_for(worker._main_loop(cast(AsyncSession, None)), timeout=2)

    assert worker.processed == [[0, 1]]
    assert worker.idle_sleep_calls >= 1


@pytest.mark.asyncio
async def test_main_loop_stop_event_breaks_promptly() -> None:
    worker = _FakeWorker(batch_size=5)
    worker._idle_sleep = 5  # would hang the test if the stop event didn't interrupt it

    async def _stop_soon() -> None:
        await asyncio.sleep(0.05)
        worker._stop_event.set()

    stop_task = asyncio.create_task(_stop_soon())
    started = time.monotonic()
    await asyncio.wait_for(worker._main_loop(cast(AsyncSession, None)), timeout=2)
    elapsed = time.monotonic() - started
    await stop_task

    assert worker.processed == []
    assert elapsed < 1
