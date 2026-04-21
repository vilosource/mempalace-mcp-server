"""Identity-resolution + WAL + write-lock chokepoint.

Per TDD §6. Every mutating tool goes through `dispatch_write`; every
reading tool goes through `dispatch_read`. The chokepoint stamps
caller_id (never from client input), writes the WAL entry, and acquires
the single-writer lock for mutations.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable

import structlog

from server.auth import get_caller_id
from server.errors import MempalaceError
from server.wal import WalWriter

log = structlog.get_logger()

_write_lock = asyncio.Lock()


def new_request_id() -> str:
    """ULID-style request ID (sortable, unique enough for logs)."""
    return uuid.uuid4().hex


async def dispatch_write(
    operation: str,
    impl: Callable[..., Awaitable[dict]],
    args: dict[str, Any],
    *,
    wal: WalWriter,
) -> dict:
    """Serialize + WAL + stamp caller_id for a mutating tool call."""
    # Strip any client-supplied caller_id as a defense-in-depth measure
    # (the tool schemas should not declare it, but never trust input).
    args.pop("caller_id", None)
    caller_id = get_caller_id()
    rid = new_request_id()
    t0 = time.perf_counter()

    async with _write_lock:
        wal.log(operation, args, caller_id=caller_id, request_id=rid)
        try:
            result = await impl(caller_id=caller_id, **args)
        except MempalaceError:
            raise
        except Exception as e:
            log.error(
                "tool.error",
                tool=operation,
                caller_id=caller_id,
                request_id=rid,
                err_type=type(e).__name__,
                err=str(e),
            )
            raise

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    log.info(
        "tool.call",
        tool=operation,
        caller_id=caller_id,
        request_id=rid,
        duration_ms=duration_ms,
        status="ok",
    )
    return result


async def dispatch_read(
    operation: str,
    impl: Callable[..., Awaitable[dict]],
    args: dict[str, Any],
) -> dict:
    """Stamp caller_id into the handler context for a read-only tool."""
    args.pop("caller_id", None)
    caller_id = get_caller_id()
    rid = new_request_id()
    t0 = time.perf_counter()

    try:
        result = await impl(caller_id=caller_id, **args)
    except MempalaceError:
        raise
    except Exception as e:
        log.error(
            "tool.error",
            tool=operation,
            caller_id=caller_id,
            request_id=rid,
            err_type=type(e).__name__,
            err=str(e),
        )
        raise

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    log.info(
        "tool.call",
        tool=operation,
        caller_id=caller_id,
        request_id=rid,
        duration_ms=duration_ms,
        status="ok",
    )
    return result
