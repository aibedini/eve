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

# Worker recycling. Disabled by default (0), because it is a SAFETY NET and not a fix: a
# recycled worker drops the snapshot it had hydrated and warms it again, so on a small host
# it only helps if the measured trend (Settings -> Overview -> Memory) shows growth that a
# restart actually reclaims. Enable it deliberately once the attribution exists, and keep
# the value high enough that restarts are not the steady state; the jitter spreads them so
# two workers never recycle at the same moment.
#
#   GUNICORN_MAX_REQUESTS=800  GUNICORN_MAX_REQUESTS_JITTER=100
#
# See docs/performance/MEMORY.md ("Runtime configuration").
_max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", 0) or 0)
if _max_requests > 0:
    max_requests = _max_requests
    max_requests_jitter = max(0, int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", 0) or 0))
