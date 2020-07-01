"""
Infection apis for ``asyncio`` loops running ``trio`` using guest mode.
"""
import asyncio
import inspect
from typing import (
    Any,
    Callable,
    AsyncGenerator,
    Awaitable,
    Union,
)

import trio

from .log import get_logger
from ._state import current_actor

log = get_logger(__name__)


__all__ = ['run_task']


async def _invoke(
    from_trio: trio.abc.ReceiveChannel,
    to_trio: asyncio.Queue,
    coro: Awaitable,
) -> Union[AsyncGenerator, Awaitable]:
    """Await or stream awaiable object based on type into
    ``trio`` memory channel.
    """
    async def stream_from_gen(c):
        async for item in c:
            to_trio.send_nowait(item)

    async def just_return(c):
        to_trio.send_nowait(await c)

    if inspect.isasyncgen(coro):
        return await stream_from_gen(coro)
    elif inspect.iscoroutine(coro):
        return await coro


async def run_task(
    func: Callable,
    qsize: int = 2**10,
    **kwargs,
) -> Any:
    """Run an ``asyncio`` async function or generator in a task, return
    or stream the result back to ``trio``.
    """
    assert current_actor()._infected_aio

    # ITC (inter task comms)
    from_trio = asyncio.Queue(qsize)
    to_trio, from_aio = trio.open_memory_channel(qsize)

    # allow target func to accept/stream results manually
    kwargs['to_trio'] = to_trio
    kwargs['from_trio'] = to_trio

    coro = func(**kwargs)

    cancel_scope = trio.CancelScope()

    # start the asyncio task we submitted from trio
    # TODO: try out ``anyio`` asyncio based tg here
    task = asyncio.create_task(_invoke(from_trio, to_trio, coro))
    err = None

    # XXX: I'm not sure this actually does anything...
    def cancel_trio(task):
        """Cancel the calling ``trio`` task on error.
        """
        nonlocal err
        err = task.exception()
        cancel_scope.cancel()

    task.add_done_callback(cancel_trio)

    # determine return type async func vs. gen
    if inspect.isasyncgen(coro):
        # simple async func
        async def result():
            with cancel_scope:
                return await from_aio.get()
            if cancel_scope.cancelled_caught and err:
                raise err

    elif inspect.iscoroutine(coro):
        # asycn gen
        async def result():
            with cancel_scope:
                async with from_aio:
                    async for item in from_aio:
                        yield item
            if cancel_scope.cancelled_caught and err:
                raise err

    return result()


def run_as_asyncio_guest(
    trio_main: Awaitable,
) -> None:
    """Entry for an "infected ``asyncio`` actor".

    Uh, oh. :o

    It looks like your event loop has caught a case of the ``trio``s.

    :()

    Don't worry, we've heard you'll barely notice. You might hallucinate
    a few more propagating errors and feel like your digestion has
    slowed but if anything get's too bad your parents will know about
    it.

    :)
    """
    async def aio_main(trio_main):
        loop = asyncio.get_running_loop()

        trio_done_fut = asyncio.Future()

        def trio_done_callback(main_outcome):
            log.info(f"trio_main finished: {main_outcome!r}")
            trio_done_fut.set_result(main_outcome)

        # start the infection: run trio on the asyncio loop in "guest mode"
        log.info(f"Infecting asyncio process with {trio_main}")
        trio.lowlevel.start_guest_run(
            trio_main,
            run_sync_soon_threadsafe=loop.call_soon_threadsafe,
            done_callback=trio_done_callback,
        )

        (await trio_done_fut).unwrap()

    # might as well if it's installed.
    try:
        import uvloop
        loop = uvloop.new_event_loop()
        asyncio.set_event_loop(loop)
    except ImportError:
        pass

    asyncio.run(aio_main(trio_main))