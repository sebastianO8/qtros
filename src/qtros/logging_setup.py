"""structlog configuration (mandate §2: zero ``print`` statements).

Called once by the CLI. Library code never configures logging; it only emits.
"""

from __future__ import annotations

import logging
import sys
from typing import Final

import structlog

_LEVELS: Final[dict[str, int]] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def configure_logging(level: str = "INFO", log_format: str = "console") -> None:
    """Configure structlog for a process.

    Args:
        level: ``DEBUG``, ``INFO``, ``WARNING``, or ``ERROR``.
        log_format: ``json`` for machine-readable lines, ``console`` for humans.

    Raises:
        ValueError: On an unknown level or format.
    """
    if level not in _LEVELS:
        raise ValueError(f"unknown log level {level!r}")
    if log_format not in {"json", "console"}:
        raise ValueError(f"unknown log format {log_format!r}")

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LEVELS[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
