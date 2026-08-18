"""Central logging configuration for the Eve panel.

Environment variables
---------------------
LOG_LEVEL   – Python log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
              Default: INFO.
LOG_FORMAT  – ``text`` (human-readable) or ``json`` (structured).
              Default: ``text`` in development, ``json`` when
              ``FLASK_ENV=production``.
LOG_FILE    – Optional path for a rotating file handler.
LOG_MAX_BYTES – Max bytes before rotation (default 10 MB).
LOG_BACKUP_COUNT – Number of rotated files to keep (default 5).
"""

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone

_TEXT_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_TEXT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record):
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        if record.threadName:
            entry["thread"] = record.threadName
        if hasattr(record, "process_role"):
            entry["process_role"] = record.process_role
        return json.dumps(entry, ensure_ascii=False)


def _default_format():
    """Return 'json' in production, 'text' otherwise."""
    if os.environ.get("LOG_FORMAT"):
        return os.environ["LOG_FORMAT"].lower()
    return "json" if os.environ.get("FLASK_ENV") == "production" else "text"


def setup_logging(app=None):
    """Configure the root logger and (optionally) the Flask app logger.

    Safe to call more than once — subsequent calls are no-ops when the root
    logger already has handlers installed by this function.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = _default_format()
    log_file = os.environ.get("LOG_FILE")
    max_bytes = int(os.environ.get("LOG_MAX_BYTES", 10 * 1024 * 1024))
    backup_count = int(os.environ.get("LOG_BACKUP_COUNT", 5))

    root = logging.getLogger()

    # Avoid duplicate handlers on repeated calls (e.g. test reloads).
    if not root.handlers:
        root.setLevel(level)

        console = logging.StreamHandler(sys.stderr)
        console.setLevel(level)
        root.addHandler(console)

        if log_file:
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
            )
            fh.setLevel(level)
            root.addHandler(fh)

        _apply_formatter(root, fmt)

    # Quiet noisy third-party loggers regardless of re-entry.
    for name in ("werkzeug", "urllib3.connectionpool", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)

    if app is not None:
        _integrate_flask(app, fmt)

    return root


def _apply_formatter(logger, fmt):
    if fmt == "json":
        formatter = _JSONFormatter()
    else:
        formatter = logging.Formatter(_TEXT_FORMAT, datefmt=_TEXT_DATE_FORMAT)
    for h in logger.handlers:
        h.setFormatter(formatter)


def _integrate_flask(app, fmt):
    """Make ``app.logger`` share the root logger's configuration."""
    app.logger.handlers = []
    app.logger.propagate = True
    app.logger.setLevel(logging.NOTSET)

    # Stamp every record with the process role for structured logs.
    process_role = os.environ.get("EVE_PROCESS_ROLE", "web")

    class _RoleFilter(logging.Filter):
        def filter(self, record):
            record.process_role = process_role
            return True

    role_filter = _RoleFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(role_filter)


def get_logger(name):
    """Return a logger that participates in the central configuration."""
    return logging.getLogger(name)
