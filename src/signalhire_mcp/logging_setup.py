"""Correlation IDs and structured logging.

Every SignalHire reveal gets a correlation id that follows it from the outbound
request, through the durable inbox, into each delivery attempt. That is what
makes the observability requirement answerable: given a candidate who never got
enriched, you can say which request produced them, whether the callback landed,
which adapter ran, and what the last error was.

stderr, not stdout. Under the stdio transport stdout carries the MCP protocol
itself, so anything written there corrupts the stream. The previous
implementation called `print()` at module scope and throughout its lifespan —
eight call sites — which garbles the protocol for any stdio client and, on a
Windows console, raises `UnicodeEncodeError` outright on the emoji it printed.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from contextvars import ContextVar

#: Set per inbound MCP tool call and per callback delivery, so every log line
#: emitted while handling one logical operation shares an id.
_run_id: ContextVar[str | None] = ContextVar("signalhire_mcp_run_id", default=None)


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def set_run_id(run_id: str | None = None) -> str:
    value = run_id or new_correlation_id()
    _run_id.set(value)
    return value


def get_run_id() -> str | None:
    return _run_id.get()


class _RunIdFilter(logging.Filter):
    """Attach the current correlation id to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = get_run_id() or "-"
        return True


def configure_logging(level: str | None = None) -> logging.Logger:
    """Configure structured logging on stderr. Idempotent."""
    resolved = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    logger = logging.getLogger("signalhire_mcp")
    if logger.handlers:
        logger.setLevel(resolved)
        return logger

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(run_id)s] %(name)s %(message)s")
    )
    handler.addFilter(_RunIdFilter())
    logger.addHandler(handler)
    logger.setLevel(resolved)
    # Do not also emit through the root logger's handlers.
    logger.propagate = False
    return logger


def get_logger(name: str = "signalhire_mcp") -> logging.Logger:
    return logging.getLogger(name)
