"""Event loop setup.

psycopg's async mode does not work on the Windows default (proactor) loop, so
entry points go through `run`, which uses a selector loop on Windows.
"""

import asyncio
import selectors
import sys
from collections.abc import Coroutine
from typing import Any


def loop_factory() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.new_event_loop()


def run[T](main: Coroutine[Any, Any, T]) -> T:
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        return runner.run(main)
