"""
tractor: An actor model micro-framework built on
         ``trio`` and ``multiprocessing``.
"""
from trio import MultiError

from ._ipc import Channel
from ._streaming import (
    Context,
    ReceiveMsgStream,
    MsgStream,
    stream,
    context,
)
from ._discovery import get_arbiter, find_actor, wait_for_actor
from ._trionics import open_nursery
from ._state import current_actor, is_root_process
from ._exceptions import (
    RemoteActorError,
    ModuleNotExposed,
    ContextCancelled,
)
from ._debug import breakpoint, post_mortem
from . import msg
from ._root import run, run_daemon, open_root_actor
from ._portal import Portal


__all__ = [
    'Channel',
    'Context',
    'ModuleNotExposed',
    'MultiError',
    'RemoteActorError',
    'ContextCancelled',
    'breakpoint',
    'current_actor',
    'find_actor',
    'get_arbiter',
    'is_root_process',
    'msg',
    'open_nursery',
    'open_root_actor',
    'Portal',
    'post_mortem',
    'run',
    'run_daemon',
    'stream',
    'context',
    'ReceiveMsgStream',
    'MsgStream',
    'to_asyncio',
    'wait_for_actor',
]
