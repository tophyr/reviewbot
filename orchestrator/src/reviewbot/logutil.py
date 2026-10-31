"""Logging setup with redaction of known secret values.

Every secret the orchestrator handles (GitHub tokens it mints or reads, the
real Claude token) is registered here. A filter on the root handler replaces
any occurrence in a log record with a placeholder, so a secret that reaches
a log call by mistake (for example inside a container's stderr) is not
written out.
"""

from __future__ import annotations

import logging
import os
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


class JournalFormatter(logging.Formatter):
    """The message alone, every line prefixed with its syslog priority.

    journald adds the time and the process to each line, and with the unit's
    default SyslogLevelPrefix=yes it stores a line's "<N>" prefix as that
    entry's priority. Each line of a multi-line message is its own entry, so
    each gets the prefix; journald strips only the first one, so a line that
    already began with a prefix (in a container's output) keeps ours.
    """

    PRIORITIES = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4,
                  logging.ERROR: 3, logging.CRITICAL: 2}

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{self.PRIORITIES.get(record.levelno, 6)}>"
        return "\n".join(prefix + line for line in super().format(record).split("\n"))


def stream_is_journal(stream) -> bool:
    """True when stream is connected to the journal. systemd sets
    JOURNAL_STREAM to that connection's device and inode, and the variable
    alone is not enough: a process can inherit it with another stderr."""
    expected = os.environ.get("JOURNAL_STREAM", "")
    try:
        st = os.fstat(stream.fileno())
    except (OSError, ValueError):
        return False
    return bool(expected) and expected == f"{st.st_dev}:{st.st_ino}"


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    if stream_is_journal(sys.stderr):
        handler.setFormatter(JournalFormatter("%(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # urllib3 logs one line per HTTP request at DEBUG; keep that noise out.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
