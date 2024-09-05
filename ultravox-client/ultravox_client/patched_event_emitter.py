import inspect
import logging
from typing import Callable

import pyee.asyncio
import pyee.base


class PatchedAsyncIOEventEmitter(pyee.asyncio.AsyncIOEventEmitter):
    """A patched version of pyee's AsyncIOEventEmitter that swallows exceptions raised by error handlers."""

    def _add_event_handler(self, event: str, k: Callable, v: Callable) -> None:
        if event == "error":
            # In AsyncIOEventEmitter, exceptions raised by event handlers trigger the 'error' event,
            # _including exceptions raised from the error handler itself_. We change this behavior to instead
            # swallow (but log) any exceptions raised by an error handler.
            def wrapped_f(*args, **kwargs):
                try:
                    result = v(*args, **kwargs)
                    if inspect.isawaitable(result):

                        async def wrapped_result():
                            try:
                                return await result
                            except Exception:
                                logging.exception("Wrapped async error handler failed")

                        return wrapped_result()
                    else:
                        return result
                except Exception:
                    logging.exception("Wrapped error handler failed")

            return super()._add_event_handler(event, k, wrapped_f)

        return super()._add_event_handler(event, k, v)
