"""Flask extensions, constructed unbound and attached via ``init_app`` in app.py.

This lets every module (models, workers, services) import ``db``/``limiter``
without importing the Flask app object itself.
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sqlalchemy import SQLAlchemy

from panel.core.redis_client import REDIS_URL, redis_enabled

db = SQLAlchemy()

# Use Redis for rate-limit storage when available so limits are shared across
# gunicorn workers (and the "in-memory storage" warning goes away). Falls back
# to in-memory if Redis isn't configured/reachable.
_LIMITER_STORAGE_URI = REDIS_URL if (REDIS_URL and redis_enabled()) else "memory://"
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["5000 per day", "500 per hour"],
    storage_uri=_LIMITER_STORAGE_URI,
)
