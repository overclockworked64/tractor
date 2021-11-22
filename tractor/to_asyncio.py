'''
Infection apis for ``asyncio`` loops running ``trio`` using guest mode.

'''
import asyncio
from asyncio.exceptions import CancelledError
from contextlib import asynccontextmanager as acm
from dataclasses import dataclass
import inspect
from typing import (
    Any,
    Callable,
    AsyncIterator,
    Awaitable,
    Optional,
)

import trio

from .log import get_logger
from ._state import current_actor

log = get_logger(__name__)


__all__ = ['run_task', 'run_as_asyncio_guest']


def _run_asyncio_task(
    func: Callable,
    *,
    qsize: int = 1,
    provide_channels: bool = False,
    **kwargs,

) -> Any:
    '''
    Run an ``asyncio`` async function or generator in a task, return
    or stream the result back to ``trio``.

    '''
    if not current_actor().is_infected_aio():
        raise RuntimeError("`infect_asyncio` mode is not enabled!?")

    # ITC (inter task comms), these channel/queue names are mostly from
    # ``asyncio``'s perspective.
    from_trio = asyncio.Queue(qsize)  # type: ignore
    to_trio, from_aio = trio.open_memory_channel(qsize)  # type: ignore

    from_aio._err = None

    args = tuple(inspect.getfullargspec(func).args)

    if getattr(func, '_tractor_steam_function', None):
        # the assumption is that the target async routine accepts the
        # send channel then it intends to yield more then one return
        # value otherwise it would just return ;P
        assert qsize > 1

    if provide_channels:
        assert 'to_trio' in args

    # allow target func to accept/stream results manually by name
    if 'to_trio' in args:
        kwargs['to_trio'] = to_trio

    if 'from_trio' in args:
        kwargs['from_trio'] = from_trio

    coro = func(**kwargs)

    cancel_scope = trio.CancelScope()
    aio_task_complete = trio.Event()
    aio_err: Optional[BaseException] = None

    async def wait_on_coro_final_result(

        to_trio: trio.MemorySendChannel,
        coro: Awaitable,
        aio_task_complete: trio.Event,

    ) -> None:
        '''
        Await ``coro`` and relay result back to ``trio``.

        '''
        nonlocal aio_err
        orig = result = id(coro)
        try:
            result = await coro
        except BaseException as err:
            aio_err = err
            from_aio._err = aio_err
            to_trio.close()
            from_aio.close()
            raise

        finally:
            if (
                result != orig and
                aio_err is None and

                # in the ``open_channel_from()`` case we don't
                # relay through the "return value".
                not provide_channels
            ):
                to_trio.send_nowait(result)

            # if the task was spawned using ``open_channel_from()``
            # then we close the channels on exit.
            if provide_channels:
                to_trio.close()
                from_aio.close()

            aio_task_complete.set()

    # start the asyncio task we submitted from trio
    if inspect.isawaitable(coro):
        task = asyncio.create_task(
            wait_on_coro_final_result(
                to_trio,
                coro,
                aio_task_complete
            )
        )

    else:
        raise TypeError(f"No support for invoking {coro}")

    def cancel_trio(task) -> None:
        '''
        Cancel the calling ``trio`` task on error.

        '''
        nonlocal aio_err
        try:
            aio_err = task.exception()
        except CancelledError as cerr:
            log.cancel("infected task was cancelled")
            from_aio._err = cerr
            from_aio.close()
            cancel_scope.cancel()
        else:
            if aio_err is not None:
                aio_err.with_traceback(aio_err.__traceback__)
                log.exception("infected task errorred:")
                from_aio._err = aio_err

                # NOTE: order is opposite here
                cancel_scope.cancel()
                from_aio.close()

    task.add_done_callback(cancel_trio)

    return task, from_aio, to_trio, from_trio, cancel_scope, aio_task_complete


@acm
async def translate_aio_errors(

    from_aio: trio.MemoryReceiveChannel,
    task: asyncio.Task,

) -> None:
    '''
    Error handling context around ``asyncio`` task spawns which
    appropriately translates errors and cancels into ``trio`` land.

    '''
    err: Optional[Exception] = None
    aio_err: Optional[Exception] = None

    def maybe_raise_aio_err(err: Exception):
        aio_err = from_aio._err
        if (
            aio_err is not None and
            type(aio_err) != CancelledError
        ):
            # always raise from any captured asyncio error
            raise aio_err from err

    try:
        yield
    except (
        Exception,
        CancelledError,
    ) as err:
        maybe_raise_aio_err(err)
        raise

    finally:
        if not task.done() and aio_err:
            task.cancel()

        maybe_raise_aio_err(err)
        # if task.cancelled():
        #     ... do what ..


async def run_task(
    func: Callable,
    *,

    qsize: int = 2**10,
    **kwargs,

) -> Any:
    '''
    Run an ``asyncio`` async function or generator in a task, return
    or stream the result back to ``trio``.

    '''
    # simple async func
    task, from_aio, to_trio, aio_q, cs, _ = _run_asyncio_task(
        func,
        qsize=1,
        **kwargs,
    )
    async with translate_aio_errors(from_aio, task):

        # return single value
        with cs:
            # naively expect the mem chan api to do the job
            # of handling cross-framework cancellations / errors
            return await from_aio.receive()

        if cs.cancelled_caught:
            aio_err = from_aio._err

            # always raise from any captured asyncio error
            if aio_err:
                raise aio_err


# TODO: explicitly api for the streaming case where
# we pull from the mem chan in an async generator?
# This ends up looking more like our ``Portal.open_stream_from()``
# NB: code below is untested.


@dataclass
class LinkedTaskChannel(trio.abc.Channel):
    '''
    A "linked task channel" which allows for two-way synchronized msg
    passing between a ``trio``-in-guest-mode task and an ``asyncio``
    task.

    '''
    _aio_task: asyncio.Task
    _to_aio: asyncio.Queue
    _from_aio: trio.MemoryReceiveChannel
    _aio_task_complete: trio.Event

    async def aclose(self) -> None:
        self._from_aio.close()

    async def receive(self) -> Any:
        async with translate_aio_errors(self._from_aio, self._aio_task):
            return await self._from_aio.receive()

    async def wait_ayncio_complete(self) -> None:
        await self._aio_task_complete.wait()

    def cancel_asyncio_task(self) -> None:
        self._aio_task.cancel()

    async def send(self, item: Any) -> None:
        '''
        Send a value through to the asyncio task presuming
        it defines a ``from_trio`` argument, if it does not
        this method will raise an error.

        '''
        self._to_aio.put_nowait(item)


@acm
async def open_channel_from(

    target: Callable[[Any, ...], Any],
    **kwargs,

) -> AsyncIterator[Any]:
    '''
    Open an inter-loop linked task channel for streaming between a target
    spawned ``asyncio`` task and ``trio``.

    '''
    task, from_aio, to_trio, aio_q, cs, aio_task_complete = _run_asyncio_task(
        target,
        qsize=2**8,
        provide_channels=True,
        **kwargs,
    )
    chan = LinkedTaskChannel(task, aio_q, from_aio, aio_task_complete)
    with cs:
        async with translate_aio_errors(from_aio, task):
            # sync to a "started()"-like first delivered value from the
            # ``asyncio`` task.
            first = await from_aio.receive()

            # stream values upward
            async with from_aio:
                yield first, chan


def run_as_asyncio_guest(

    trio_main: Callable,

) -> None:
    '''
    Entry for an "infected ``asyncio`` actor".

    Entrypoint for a Python process which starts the ``asyncio`` event
    loop and runs ``trio`` in guest mode resulting in a system where
    ``trio`` tasks can control ``asyncio`` tasks whilst maintaining
    SC semantics.

    '''
    # Uh, oh. :o

    # It looks like your event loop has caught a case of the ``trio``s.

    # :()

    # Don't worry, we've heard you'll barely notice. You might hallucinate
    # a few more propagating errors and feel like your digestion has
    # slowed but if anything get's too bad your parents will know about
    # it.

    # :)

    async def aio_main(trio_main):

        loop = asyncio.get_running_loop()
        trio_done_fut = asyncio.Future()

        def trio_done_callback(main_outcome):

            print(f"trio_main finished: {main_outcome!r}")
            trio_done_fut.set_result(main_outcome)

        # start the infection: run trio on the asyncio loop in "guest mode"
        log.info(f"Infecting asyncio process with {trio_main}")

        trio.lowlevel.start_guest_run(
            trio_main,
            run_sync_soon_threadsafe=loop.call_soon_threadsafe,
            done_callback=trio_done_callback,
        )
        return (await trio_done_fut).unwrap()

    # might as well if it's installed.
    try:
        import uvloop
        loop = uvloop.new_event_loop()
        asyncio.set_event_loop(loop)
    except ImportError:
        pass

    return asyncio.run(aio_main(trio_main))
