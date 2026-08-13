import asyncio
import signal
from typing import Generic
from typing import TypeVar

from loguru import logger

from app import http_client
from app.database import AsyncSession
from app.database import async_session

T = TypeVar("T")


class Worker(Generic[T]):
    # How many messages to fetch per poll. Subclasses that want batched,
    # concurrent processing raise this and override `get_next_messages`/
    # `process_messages`; a class attr (rather than a constructor arg) so
    # tests can shrink `_idle_sleep` on a subclass without touching __init__.
    batch_size: int = 1
    _idle_sleep: float = 2.0

    def __init__(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._stop_event = asyncio.Event()

    async def process_message(self, db_session: AsyncSession, message: T) -> None:
        raise NotImplementedError

    async def get_next_message(self, db_session: AsyncSession) -> T | None:
        raise NotImplementedError

    async def get_next_messages(self, db_session: AsyncSession, limit: int) -> list[T]:
        next_message = await self.get_next_message(db_session)
        return [next_message] if next_message else []

    async def process_messages(
        self, db_session: AsyncSession, messages: list[T]
    ) -> None:
        for message in messages:
            await self.process_message(db_session, message)

    async def startup(self, db_session: AsyncSession) -> None:
        return None

    async def _sleep(self, seconds: float) -> None:
        # Interruptible: shutdown shouldn't have to wait out a full idle sleep.
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _main_loop(self, db_session: AsyncSession) -> None:
        while not self._stop_event.is_set():
            try:
                messages = await self.get_next_messages(db_session, self.batch_size)
                if messages:
                    await self.process_messages(db_session, messages)

                if messages and len(messages) >= self.batch_size:
                    # Batch was full: more work is likely already due, so
                    # don't idle-sleep, just yield the loop.
                    await asyncio.sleep(0)
                else:
                    await self._sleep(self._idle_sleep)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A poison message must never crash-loop the process under
                # supervisord's autorestart.
                logger.exception("Unexpected error in worker main loop")
                await db_session.rollback()
                await self._sleep(self._idle_sleep)

    async def _until_stopped(self) -> None:
        await self._stop_event.wait()

    async def run_forever(self) -> None:
        signals = (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)
        for s in signals:
            self._loop.add_signal_handler(
                s,
                lambda s=s: asyncio.create_task(self._shutdown(s)),  # type: ignore
            )

        async with async_session() as db_session:
            await self.startup(db_session)
            task = self._loop.create_task(self._main_loop(db_session))
            stop_task = self._loop.create_task(self._until_stopped())

            done, pending = await asyncio.wait(
                {task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            logger.info(f"Waiting for tasks to finish {done=}/{pending=}")
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            logger.info(f"Cancelling {len(tasks)} tasks")
            [t.cancel() for t in tasks]

            # Keep the session open while cancelled tasks unwind: any
            # salvage-on-shutdown cleanup (e.g. a rollback in `_main_loop`'s
            # except branch) still needs a live `db_session`.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=15,
                )
            except asyncio.TimeoutError:
                logger.info("Tasks failed to cancel")

        await http_client.aclose_all()
        logger.info("stopping loop")

    async def _shutdown(self, sig: signal.Signals) -> None:
        logger.info(f"Caught {sig=}")
        self._stop_event.set()
