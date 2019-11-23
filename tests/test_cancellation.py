"""
Cancellation and error propagation
"""
import platform
from itertools import repeat

import pytest
import trio
import tractor

from conftest import tractor_test


async def assert_err(delay=0):
    await trio.sleep(delay)
    assert 0


async def sleep_forever():
    await trio.sleep(float('inf'))


async def do_nuthin():
    # just nick the scheduler
    await trio.sleep(0)


@pytest.mark.parametrize(
    'args_err',
    [
        # expected to be thrown in assert_err
        ({}, AssertionError),
        # argument mismatch raised in _invoke()
        ({'unexpected': 10}, TypeError)
    ],
    ids=['no_args', 'unexpected_args'],
)
def test_remote_error(arb_addr, args_err):
    """Verify an error raised in a subactor that is propagated
    to the parent nursery, contains the underlying boxed builtin
    error type info and causes cancellation and reraising all the
    way up the stack.
    """
    args, errtype = args_err

    async def main():
        async with tractor.open_nursery() as nursery:

            portal = await nursery.run_in_actor('errorer', assert_err, **args)

            # get result(s) from main task
            try:
                await portal.result()
            except tractor.RemoteActorError as err:
                assert err.type == errtype
                print("Look Maa that actor failed hard, hehh")
                raise

    with pytest.raises(tractor.RemoteActorError) as excinfo:
        tractor.run(main, arbiter_addr=arb_addr)

    # ensure boxed error is correct
    assert excinfo.value.type == errtype


def test_multierror(arb_addr):
    """Verify we raise a ``trio.MultiError`` out of a nursery where
    more then one actor errors.
    """
    async def main():
        async with tractor.open_nursery() as nursery:

            await nursery.run_in_actor('errorer1', assert_err)
            portal2 = await nursery.run_in_actor('errorer2', assert_err)

            # get result(s) from main task
            try:
                await portal2.result()
            except tractor.RemoteActorError as err:
                assert err.type == AssertionError
                print("Look Maa that first actor failed hard, hehh")
                raise

        # here we should get a `trio.MultiError` containing exceptions
        # from both subactors

    with pytest.raises(trio.MultiError):
        tractor.run(main, arbiter_addr=arb_addr)


@pytest.mark.parametrize('delay', (0, 0.5))
@pytest.mark.parametrize(
    'num_subactors', range(25, 26),
)
def test_multierror_fast_nursery(arb_addr, start_method, num_subactors, delay):
    """Verify we raise a ``trio.MultiError`` out of a nursery where
    more then one actor errors and also with a delay before failure
    to test failure during an ongoing spawning.
    """
    async def main():
        async with tractor.open_nursery() as nursery:
            for i in range(num_subactors):
                await nursery.run_in_actor(
                    f'errorer{i}', assert_err, delay=delay)

    with pytest.raises(trio.MultiError) as exc_info:
        tractor.run(main, arbiter_addr=arb_addr)

    assert exc_info.type == tractor.MultiError
    err = exc_info.value
    assert len(err.exceptions) == num_subactors
    for exc in err.exceptions:
        assert isinstance(exc, tractor.RemoteActorError)
        assert exc.type == AssertionError


def do_nothing():
    pass


def test_cancel_single_subactor(arb_addr):
    """Ensure a ``ActorNursery.start_actor()`` spawned subactor
    cancels when the nursery is cancelled.
    """
    async def spawn_actor():
        """Spawn an actor that blocks indefinitely.
        """
        async with tractor.open_nursery() as nursery:

            portal = await nursery.start_actor(
                'nothin', rpc_module_paths=[__name__],
            )
            assert (await portal.run(__name__, 'do_nothing')) is None

            # would hang otherwise
            await nursery.cancel()

    tractor.run(spawn_actor, arbiter_addr=arb_addr)


async def stream_forever():
    for i in repeat("I can see these little future bubble things"):
        # each yielded value is sent over the ``Channel`` to the
        # parent actor
        yield i
        await trio.sleep(0.01)


@tractor_test
async def test_cancel_infinite_streamer(start_method):

    # stream for at most 1 seconds
    with trio.move_on_after(1) as cancel_scope:
        async with tractor.open_nursery() as n:
            portal = await n.start_actor(
                f'donny',
                rpc_module_paths=[__name__],
            )

            # this async for loop streams values from the above
            # async generator running in a separate process
            async for letter in await portal.run(__name__, 'stream_forever'):
                print(letter)

    # we support trio's cancellation system
    assert cancel_scope.cancelled_caught
    assert n.cancelled


@pytest.mark.parametrize(
    'num_actors_and_errs',
    [
        # daemon actors sit idle while single task actors error out
        (1, tractor.RemoteActorError, AssertionError, (assert_err, {}), None),
        (2, tractor.MultiError, AssertionError, (assert_err, {}), None),
        (3, tractor.MultiError, AssertionError, (assert_err, {}), None),

        # 1 daemon actor errors out while single task actors sleep forever
        (3, tractor.RemoteActorError, AssertionError, (sleep_forever, {}),
         (assert_err, {}, True)),
        # daemon actors error out after brief delay while single task
        # actors complete quickly
        (3, tractor.RemoteActorError, AssertionError,
         (do_nuthin, {}), (assert_err, {'delay': 1}, True)),
        # daemon complete quickly delay while single task
        # actors error after brief delay
        (3, tractor.MultiError, AssertionError,
         (assert_err, {'delay': 1}), (do_nuthin, {}, False)),
    ],
    ids=[
        '1_run_in_actor_fails',
        '2_run_in_actors_fail',
        '3_run_in_actors_fail',
        '1_daemon_actors_fail',
        '1_daemon_actors_fail_all_run_in_actors_dun_quick',
        'no_daemon_actors_fail_all_run_in_actors_sleep_then_fail',
    ],
)
@tractor_test
async def test_some_cancels_all(num_actors_and_errs, start_method):
    """Verify a subset of failed subactors causes all others in
    the nursery to be cancelled just like the strategy in trio.

    This is the first and only supervisory strategy at the moment.
    """
    num_actors, first_err, err_type, ria_func, da_func = num_actors_and_errs
    try:
        async with tractor.open_nursery() as n:

            # spawn the same number of deamon actors which should be cancelled
            dactor_portals = []
            for i in range(num_actors):
                dactor_portals.append(await n.start_actor(
                    f'deamon_{i}',
                    rpc_module_paths=[__name__],
                ))

            func, kwargs = ria_func
            riactor_portals = []
            for i in range(num_actors):
                # start actor(s) that will fail immediately
                riactor_portals.append(
                    await n.run_in_actor(f'actor_{i}', func, **kwargs))

            if da_func:
                func, kwargs, expect_error = da_func
                for portal in dactor_portals:
                    # if this function fails then we should error here
                    # and the nursery should teardown all other actors
                    try:
                        await portal.run(__name__, func.__name__, **kwargs)
                    except tractor.RemoteActorError as err:
                        assert err.type == err_type
                        # we only expect this first error to propogate
                        # (all other daemons are cancelled before they
                        # can be scheduled)
                        num_actors = 1
                        # reraise so nursery teardown is triggered
                        raise
                    else:
                        if expect_error:
                            pytest.fail(
                                "Deamon call should fail at checkpoint?")

        # should error here with a ``RemoteActorError`` or ``MultiError``

    except first_err as err:
        if isinstance(err, tractor.MultiError):
            assert len(err.exceptions) == num_actors
            for exc in err.exceptions:
                if isinstance(exc, tractor.RemoteActorError):
                    assert exc.type == err_type
                else:
                    assert isinstance(exc, trio.Cancelled)
        elif isinstance(err, tractor.RemoteActorError):
            assert err.type == err_type

        assert n.cancelled is True
        assert not n._children
    else:
        pytest.fail("Should have gotten a remote assertion error?")


async def spawn_and_error(num) -> None:
    name = tractor.current_actor().name
    async with tractor.open_nursery() as nursery:
        for i in range(num):
            await nursery.run_in_actor(
                f'{name}_errorer_{i}', assert_err
            )


@tractor_test
async def test_nested_multierrors(loglevel, start_method):
    """Test that failed actor sets are wrapped in `trio.MultiError`s.
    This test goes only 2 nurseries deep but we should eventually have tests
    for arbitrary n-depth actor trees.
    """
    if platform.system() == 'Windows':
        # Windows CI seems to be partifcularly fragile on Python 3.8..
        num_subactors = 2
    else:
        # XXX: any more then this and the forkserver will
        # start bailing hard...gotta look into it
        num_subactors = 4

    try:
        async with tractor.open_nursery() as nursery:

            for i in range(num_subactors):
                await nursery.run_in_actor(
                    f'spawner_{i}',
                    spawn_and_error,
                    num=num_subactors,
                )
    except trio.MultiError as err:
        assert len(err.exceptions) == num_subactors
        for subexc in err.exceptions:
            assert isinstance(subexc, tractor.RemoteActorError)
            assert subexc.type is trio.MultiError
