"""Database connection pool policy.

Engine options live here so they are configurable per deployment and auditable: a
pool that is too large multiplied by the gunicorn worker count is the classic way
to exhaust PostgreSQL max_connections, and a pool that is too small turns bursts
into pool timeouts.

Environment:

* EVE_DB_POOL_SIZE - connections per worker (default 5 for SQLite, 10 for PostgreSQL)
* EVE_DB_MAX_OVERFLOW - extra connections above the pool size (same defaults)
* EVE_DB_POOL_TIMEOUT - seconds to wait for a connection (default 10)
* EVE_DB_POOL_RECYCLE - recycle connections after this many seconds (default 1800)
* EVE_DB_POOL_USE_LIFO - reuse the most recently returned connection first (0/1)
* EVE_DB_STATEMENT_TIMEOUT_MS - PostgreSQL statement_timeout in ms (0 = server default)
* EVE_DB_APPLICATION_NAME - PostgreSQL application_name (default eve)
* EVE_DB_MAX_CONNECTIONS - optional audit number for the expected worker demand
* GUNICORN_WORKERS / WEB_CONCURRENCY - used for the audit
"""
import os

MEMORY_SQLITE_HINT = ':memory:'
DEFAULT_RECYCLE = 1800
DEFAULT_TIMEOUT = 10


def _env_int(name, default, minimum=None):
    raw = (os.environ.get(name) or '').strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _env_float(name, default, minimum=None):
    raw = (os.environ.get(name) or '').strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _env_flag(name) -> bool:
    return (os.environ.get(name) or '').strip().lower() in ('1', 'true', 'yes', 'on')


def is_postgres(url) -> bool:
    return str(url or '').lower().startswith(('postgresql', 'postgres'))


def is_memory_sqlite(url) -> bool:
    text = str(url or '').lower()
    return text.startswith('sqlite') and MEMORY_SQLITE_HINT in text


def defaults_for(url):
    """(pool_size, max_overflow) defaults for the dialect."""
    if is_postgres(url):
        return 10, 10
    return 5, 5


def worker_count() -> int:
    for name in ('GUNICORN_WORKERS', 'WEB_CONCURRENCY'):
        value = _env_int(name, 0)
        if value > 0:
            return value
    return 1


def engine_options(url=None, *, env=None) -> dict:
    """Return SQLAlchemy engine options for this deployment.

    An in-memory SQLite database keeps its own pool implementation, so pool sizing
    arguments are omitted there instead of raising at engine creation.
    """
    if env is not None:
        previous = {key: os.environ.get(key) for key in env}
        os.environ.update({key: str(value) for key, value in env.items()})
    try:
        if is_memory_sqlite(url):
            return {'pool_pre_ping': True}
        size_default, overflow_default = defaults_for(url)
        options = {
            'pool_pre_ping': True,
            'pool_recycle': _env_int('EVE_DB_POOL_RECYCLE', DEFAULT_RECYCLE, minimum=1),
            'pool_size': _env_int('EVE_DB_POOL_SIZE', size_default, minimum=1),
            'max_overflow': _env_int('EVE_DB_MAX_OVERFLOW', overflow_default, minimum=0),
            'pool_timeout': _env_float('EVE_DB_POOL_TIMEOUT', DEFAULT_TIMEOUT, minimum=0.1),
        }
        if _env_flag('EVE_DB_POOL_USE_LIFO'):
            options['pool_use_lifo'] = True
        if is_postgres(url):
            connect_args = {
                'application_name': (os.environ.get('EVE_DB_APPLICATION_NAME') or 'eve')[:63],
            }
            statement_timeout = _env_int('EVE_DB_STATEMENT_TIMEOUT_MS', 0, minimum=0)
            if statement_timeout:
                connect_args['options'] = '-c statement_timeout=%d' % statement_timeout
            options['connect_args'] = connect_args
        return options
    finally:
        if env is not None:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def validate(options) -> None:
    """Raise ValueError for a nonsensical pool configuration."""
    size = options.get('pool_size')
    overflow = options.get('max_overflow')
    timeout = options.get('pool_timeout')
    if size is not None and int(size) < 1:
        raise ValueError('pool_size must be at least 1')
    if overflow is not None and int(overflow) < 0:
        raise ValueError('max_overflow cannot be negative')
    if timeout is not None and float(timeout) <= 0:
        raise ValueError('pool_timeout must be positive')


def expected_max_connections(options=None, *, workers=None) -> int:
    options = options or engine_options()
    size = int(options.get('pool_size') or 0)
    overflow = int(options.get('max_overflow') or 0)
    return int(workers if workers is not None else worker_count()) * (size + overflow)


def audit(url=None, *, options=None) -> dict:
    """Describe the effective pool and flag an oversized worker demand."""
    options = options or engine_options(url)
    workers = worker_count()
    expected = expected_max_connections(options, workers=workers)
    configured_max = _env_int('EVE_DB_MAX_CONNECTIONS', 0, minimum=0)
    warning = None
    if configured_max and expected > configured_max:
        warning = (
            'Database pool demand is %d connections (%d workers x (%s + %s)) but '
            'EVE_DB_MAX_CONNECTIONS is %d; lower the pool size or the worker count.'
            % (expected, workers, options.get('pool_size'), options.get('max_overflow'),
               configured_max)
        )
    return {
        'dialect': 'postgresql' if is_postgres(url) else 'sqlite',
        'pool_size': options.get('pool_size'),
        'max_overflow': options.get('max_overflow'),
        'pool_timeout': options.get('pool_timeout'),
        'pool_recycle': options.get('pool_recycle'),
        'pool_pre_ping': bool(options.get('pool_pre_ping')),
        'pool_use_lifo': bool(options.get('pool_use_lifo')),
        'workers': workers,
        'expected_max_connections': expected,
        'configured_max_connections': configured_max or None,
        'warning': warning,
    }


def pool_summary(engine) -> dict:
    """Runtime pool state for diagnostics (never raises)."""
    try:
        pool = engine.pool
    except Exception as exc:  # pragma: no cover - defensive
        return {'error': str(exc)[:200]}

    def _call(name):
        try:
            value = getattr(pool, name)
            return value() if callable(value) else value
        except Exception:
            return None

    return {
        'pool_class': type(pool).__name__,
        'size': _call('size'),
        'checkedin': _call('checkedin'),
        'checkedout': _call('checkedout'),
        'overflow': _call('overflow'),
        'pool_timeout': getattr(pool, '_timeout', None),
        'pool_recycle': getattr(pool, '_recycle', None),
    }
