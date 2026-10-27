"""Logging setup with redaction of known secret values.

Every secret the orchestrator handles (GitHub tokens it mints or reads, the
real Claude token) is registered here. A filter on the root handler replaces
any occurrence in a log record with a placeholder, so a secret that reaches
a log call by mistake (for example inside a container's stderr) is not
written out.
"""

from __future__ import annotations

import logging
import sys
import threading


_lock = threading.Lock()
# Longest first, so a secret that contains another is replaced whole rather
# than leaving its remainder visible. A tuple, so it cannot be mutated: writers
# replace it whole and readers need no lock.
_secrets: tuple[str, ...] = ()

REDACTED = "[REDACTED]"


def register_secret(value: str | None) -> None:
    """Add a value that must never appear in log output."""
    global _secrets
    if not value or len(value) < 8:
        return
    with _lock:
        if value not in _secrets:
            _secrets = tuple(sorted((*_secrets, value), key=len, reverse=True))


class RedactingFilter(logging.Filter):
    """Redacts the message, the exception traceback and the stack.

    The formatter writes a traceback from record.exc_info only when
    record.exc_text is empty, so the filter formats and redacts the traceback
    here and stores it in exc_text. Stack text (stack_info=True) is redacted
    in place.
    """

    _formatter = logging.Formatter()

    @staticmethod
    def _redact(text: str) -> str:
        for s in _secrets:
            if s in text:
                text = text.replace(s, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format args; let logging report it
            message = None
        if message is not None:
            cleaned = self._redact(message)
            if cleaned != message:
                record.msg = cleaned
                record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = self._formatter.formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # urllib3 logs one line per HTTP request at DEBUG; keep that noise out.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
