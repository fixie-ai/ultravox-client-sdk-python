import asyncio
import logging
from typing import Awaitable


async def async_close(*awaitables_or_none: Awaitable | None):
    coros = [coro for coro in awaitables_or_none if coro is not None]
    if coros:
        maybe_exceptions = await asyncio.shield(
            asyncio.gather(*coros, return_exceptions=True)
        )
        non_cancelled_exceptions = [
            exc
            for exc in maybe_exceptions
            if isinstance(exc, Exception)
            and not isinstance(exc, asyncio.CancelledError)
        ]
        if non_cancelled_exceptions:
            to_report = (
                non_cancelled_exceptions[0]
                if len(non_cancelled_exceptions) == 1
                else ExceptionGroup(
                    "Multiple failures during async_close", non_cancelled_exceptions
                )
            )
            logging.warning("Error during async_close", exc_info=to_report)


async def async_cancel(*tasks_or_none: asyncio.Task | None):
    tasks = [task for task in tasks_or_none if task is not None and task.cancel()]
    await async_close(*tasks)
