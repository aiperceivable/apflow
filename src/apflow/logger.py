"""
Logging configuration for apflow

This module provides a simple wrapper around Python's standard logging,
without importing any apflow.core modules to keep imports fast.

Usage:
    from apflow.logger import get_logger
    logger = get_logger(__name__)

Or directly use standard library (recommended for maximum performance):
    import logging
    logger = logging.getLogger(__name__)
"""

import logging
import sys
import os
from typing import Optional


# Configure root logger for apflow namespace
def setup_logging(level: Optional[str] = None) -> None:
    """
    Configure logging for apflow namespace

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
               If None, uses APFLOW_LOG_LEVEL or falls back to LOG_LEVEL (defaults to INFO)
    """
    if level is None:
        # Priority: APFLOW_LOG_LEVEL > LOG_LEVEL > INFO (default)
        level = os.getenv("APFLOW_LOG_LEVEL") or os.getenv("LOG_LEVEL", "INFO")

    # Normalize regardless of source: getattr(logging, "DEBUG") resolves to the
    # int constant, whereas getattr(logging, "debug") resolves to the *function*
    # logging.debug, which basicConfig(level=...) rejects with TypeError. Callers
    # pass operator-supplied strings (e.g. `--log-level debug`), so uppercase here
    # rather than at each call site.
    level = level.upper()

    # Convert string level to logging constant
    numeric_level = getattr(logging, level, logging.INFO)

    # Configure basicConfig for general logging format
    # Note: basicConfig only works on first call, but we still call it for format setup
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
        force=True,  # Force reconfiguration even if basicConfig was called before
    )

    # Also explicitly set level for apflow namespace logger
    # This ensures our loggers work even if basicConfig was previously called
    apflow_logger = logging.getLogger("apflow")
    apflow_logger.setLevel(numeric_level)


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Get a logger instance

    Simple wrapper around logging.getLogger() for convenience.
    No custom configuration - uses standard Python logging.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Standard library logging.Logger instance
    """
    return logging.getLogger(name or "apflow")
