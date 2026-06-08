"""Logging utilities for Kanka Slurp."""

import logging
import sys
from tqdm import tqdm


class TqdmLoggingHandler(logging.Handler):
    """Logging handler that writes to stderr using tqdm.write to avoid conflicts with tqdm progress bars."""

    def emit(self, record):
        """Emit a log record using tqdm.write for compatibility."""
        try:
            msg = self.format(record)
            tqdm.write(msg, file=sys.stderr)
        except Exception:
            self.handleError(record)


def setup_logging(verbose: bool) -> logging.Logger:
    """Setup logging with tqdm-compatible handler.

    Args:
        verbose: If True, sets log level to DEBUG; otherwise INFO.

    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(__name__)

    # Remove any existing handlers
    logger.handlers.clear()

    # Set level
    log_level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(log_level)

    # Add custom handler that works with tqdm
    handler = TqdmLoggingHandler()
    formatter = logging.Formatter("### %(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger
