"""Gunicorn logging & worker configuration.

Loaded via ``gunicorn --config gunicorn_config.py`` (or ``-c``).
Shares the central logging setup from ``panel.core.logging_config``.
"""

import logging
import os

from panel.core.logging_config import setup_logging

# ── Logging ──────────────────────────────────────────────────────────────
setup_logging()

loglevel = os.environ.get("LOG_LEVEL", "INFO").upper()

# Route Gunicorn's own loggers through the root logger we just configured.
logger_class = "gunicorn.glogging.Logger"

# Structured access-log format: a single JSON line per request.
_access_json = (
    '{"ts":"%(t)s","level":"INFO","logger":"gunicorn.access",'
    '"msg":"%(R)s %(r)s %(s)s %(b)s %(L)s","method":"%(m)s",'
    '"url":"%(U)s%(q)s","status":"%(s)s","bytes":"%(b)s",'
    '"response_time":"%(L)s","remote":"%(h)s"}'
)
access_log_format = _access_json if os.environ.get("FLASK_ENV") == "production" else None

# Send access/error logs to stderr (Docker captures it).
accesslog = "-"
errorlog = "-"

# ── Workers ──────────────────────────────────────────────────────────────
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", 4))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 120))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", 30))
