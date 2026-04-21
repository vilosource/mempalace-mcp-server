"""structlog setup.

JSON output per TDD §8. Required fields (ts, level, logger, event) are
supplied by structlog processors; contextual fields (request_id,
caller_id, tool, duration_ms, status) are attached at log-call time from
dispatch.
"""

from __future__ import annotations

import logging
import sys

import structlog

from server.config import LoggingConfig


def configure_logging(cfg: LoggingConfig) -> None:
    level = getattr(logging, cfg.level)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", key="ts", utc=True),
    ]

    if cfg.format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
