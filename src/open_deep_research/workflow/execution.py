"""Own parallel tasks until every child has returned or completed its cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from typing import Any


async def gather_owned(calls: Mapping[str, Coroutine[Any, Any, Any]]) -> dict[str, Any]:
    """Cancel siblings on failure and shield their drain against repeated cancellation.

    There is no retry here. SDK/tool timeouts and bounded settlement remain owned
    by those layers. Preserve all outcomes on the original exception for exact
    accounting, including simultaneous failures and successful siblings.
    """
    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}

    async def collect(name: str, call: Coroutine[Any, Any, Any]) -> None:
        try:
            results[name] = await call
        except BaseException as exc:
            errors[name] = exc

    tasks = [
        asyncio.create_task(collect(name, call), name=name)
        for name, call in calls.items()
    ]
    cancellation = None
    pending = set(tasks)
    try:
        while pending and not errors:
            _, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
    except asyncio.CancelledError as exc:
        cancellation = exc
    finally:
        # Issue cancellation once. Repeated parent cancellation must not interrupt
        # the child's already-running SDK exit settlement.
        for task in pending:
            if not task.done():
                task.cancel()
        drain = asyncio.gather(*tasks, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
        drain.result()

    ordered_results = {name: results[name] for name in calls if name in results}
    ordered_errors = {name: errors[name] for name in calls if name in errors}
    if cancellation is not None or ordered_errors:
        # A sibling cancelled because another step failed must not hide the
        # initiating failure merely because that sibling comes first by name.
        if cancellation is not None:
            failure: BaseException = cancellation
        else:
            failure = next(
                (
                    error
                    for error in ordered_errors.values()
                    if not isinstance(error, asyncio.CancelledError)
                ),
                next(iter(ordered_errors.values())),
            )
        setattr(failure, "parallel_results", ordered_results)
        setattr(failure, "parallel_errors", ordered_errors)
        raise failure
    return ordered_results
