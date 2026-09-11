"""Database connection pool and PostgreSQL runtime policy.

Engine options live here so they are configurable per deployment and auditable: a
pool that is too large multiplied by the gunicorn worker count is the classic way
to exhaust PostgreSQL max_connections, and a pool that is too small turns bursts
into pool timeouts.

The module also carries the PostgreSQL transport policy (sslmode and certificate
material, applied to every engine including Alembic), runtime facts for the doctor
endpoint, and the classifier that separates a transient connection loss from a
query bug.

Environment:

* EVE_DB_POOL_SIZE - connections per worker (default 5 for SQLite, 10 for PostgreSQL)
* EVE_DB_MAX_OVERFLOW - extra connections above the pool size (same defaults)
* EVE_DB_POOL_TIMEOUT - seconds to wait for a connection (default 10)
* EVE_DB_POOL_RECYCLE - recycle connections after this many seconds (default 1800)
* EVE_DB_POOL_USE_LIFO - reuse the most recently returned connection first (0/1)
* EVE_DB_STATEMENT_TIMEOUT_MS - PostgreSQL statement_timeout in ms (0 = server default)
* EVE_DB_APPLICATION_NAME - PostgreSQL application_name (default eve)
* EVE_DB_SSLMODE - PostgreSQL sslmode (disable/allow/prefer/require/verify-ca/verify-full)
* EVE_DB_SSLROOTCERT / EVE_DB_SSLCERT / EVE_DB_SSLKEY - TLS material paths
* EVE_DB_MIGRATION_APPLICATION_NAME - application_name during migrations
  (default eve-migrate, so a long upgrade is identifiable in pg_stat_activity)
* EVE_DB_MAX_CONNECTIONS - optional audit number for the expected worker demand
* GUNICORN_WORKERS / WEB_CONCURRENCY - used for the audit
"""
import os
import re

MEMORY_SQLITE_HINT = ':memory:'
DEFAULT_RECYCLE = 1800
DEFAULT_TIMEOUT = 10

# libpq sslmode values, strongest last. 'allow'/'prefer' can silently fall back
# to a plaintext connection, so a remote database should use require or better.
TLS_MODES = ('disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full')
TLS_MATERIAL = (
    ('EVE_DB_SSLROOTCERT', 'sslrootcert'),
    ('EVE_DB_SSLCERT', 'sslcert'),
    ('EVE_DB_SSLKEY', 'sslkey'),
)
LOCAL_HOSTS = ('', 'localhost', '127.0.0.1', '::1', '[::1]')


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


def host_of(url) -> str:
    """Host part of a database URL (empty for SQLite or an unparsable URL)."""
    match = re.search(r'@([^/?#]+)', str(url or ''))
    authority = match.group(1) if match else ''
    if authority.startswith('[') and ']' in authority:
        return authority[1:authority.index(']')]
    return authority.rsplit(':', 1)[0] if ':' in authority else authority


def is_local_host(url) -> bool:
    return host_of(url).strip().lower() in LOCAL_HOSTS


def ssl_mode() -> str:
    """Effective PostgreSQL sslmode ('' when the deployment does not set one)."""
    raw = (os.environ.get('EVE_DB_SSLMODE') or '').strip().lower()
    return raw if raw in TLS_MODES else ''


def tls_connect_args() -> dict:
    """sslmode plus any configured certificate material, for libpq."""
    args = {}
    mode = ssl_mode()
    if mode:
        args['sslmode'] = mode
    for env_name, arg_name in TLS_MATERIAL:
        value = (os.environ.get(env_name) or '').strip()
        if value:
            args[arg_name] = value
    return args


def _tls_warning(url, mode) -> str | None:
    """Flag a remote PostgreSQL database whose transport may be plaintext."""
    if is_local_host(url):
        return None
    if mode in ('', 'disable', 'allow'):
        return (
            'PostgreSQL at %s has no transport encryption configured '
            '(EVE_DB_SSLMODE=%s); set EVE_DB_SSLMODE=require (or verify-full with '
            'EVE_DB_SSLROOTCERT) so credentials and data are not sent in clear text.'
            % (host_of(url), mode or 'unset'))
    if mode == 'prefer':
        return (
            'PostgreSQL at %s uses sslmode=prefer, which silently falls back to a '
            'plaintext connection; use require or verify-full.' % host_of(url))
    return None


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
            connect_args.update(tls_connect_args())
            options['connect_args'] = connect_args
        return options
    finally:
        if env is not None:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def alembic_engine_options(url=None) -> dict:
    """Engine options for the short-lived Alembic connection (NullPool).

    Alembic connects once and exits, so pool sizing is dropped, but the migration
    should still reach PostgreSQL with the deployment's TLS policy, an
    identifiable application_name and the statement timeout.
    """
    options = engine_options(url)
    for key in ('pool_size', 'max_overflow', 'pool_timeout', 'pool_recycle',
                'pool_use_lifo'):
        options.pop(key, None)
    options['pool_pre_ping'] = True
    if is_postgres(url):
        connect_args = dict(options.get('connect_args') or {})
        connect_args['application_name'] = (
            os.environ.get('EVE_DB_MIGRATION_APPLICATION_NAME') or 'eve-migrate')[:63]
        options['connect_args'] = connect_args
    return options


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
    mode = ssl_mode()
    return {
        'dialect': 'postgresql' if is_postgres(url) else 'sqlite',
        'host': host_of(url) or None,
        'sslmode': mode or None,
        'tls_warning': _tls_warning(url, mode) if is_postgres(url) else None,
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


TRANSIENT_DB_MARKERS = (
    'server closed the connection unexpectedly',
    'ssl connection has been closed',
    'connection reset by peer',
    'connection refused',
    'could not connect',
    'could not receive data from server',
    'could not send data to server',
    'terminating connection',
    'no connection to the server',
    'connection already closed',
    'lost connection',
    'the database system is starting up',
    'the database system is shutting down',
    'the database system is in recovery mode',
    'too many clients',
    'remaining connection slots',
    'server does not support ssl',
    'operation timed out',
    'timeout expired',
    'database is locked',
    'disk i/o error',
)


def is_transient_disconnect(exc) -> bool:
    """True for connection-level failures that a retry can plausibly fix.

    Deliberately conservative: a malformed query or a missing table is a bug and
    must not be masked as a retryable 503.
    """
    text = str(exc or '').lower()
    return any(marker in text for marker in TRANSIENT_DB_MARKERS)


def pg_health(engine) -> dict:
    """PostgreSQL runtime facts for diagnostics. Never raises."""
    try:
        dialect = engine.dialect.name
    except Exception as exc:  # pragma: no cover - defensive
        return {'state': 'unknown', 'error': str(exc)[:200]}
    if dialect != 'postgresql':
        return {'state': 'skipped', 'dialect': dialect}

    queries = {
        'server_version': 'SHOW server_version',
        'max_connections': 'SHOW max_connections',
        'statement_timeout': 'SHOW statement_timeout',
        'application_name': 'SHOW application_name',
        'database_connections': (
            'SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()'),
        'session_encrypted': (
            'SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()'),
    }
    info = {'state': 'ok', 'dialect': dialect, 'sslmode': ssl_mode() or None}
    # Present every metric even when the connection cannot be opened, so callers
    # (and the doctor payload) have a stable shape.
    info.update({name: None for name in queries})
    try:
        from sqlalchemy import text as _sql_text
    except Exception:  # pragma: no cover - SQLAlchemy is a hard dependency
        return {'state': 'unknown', 'dialect': dialect, 'error': 'sqlalchemy unavailable'}
    try:
        with engine.connect() as connection:
            for name, statement in queries.items():
                try:
                    value = connection.execute(_sql_text(statement)).scalar()
                except Exception:
                    value = None
                if name in ('max_connections', 'database_connections'):
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        pass
                info[name] = value
    except Exception as exc:
        info['state'] = 'error'
        info['error'] = str(exc)[:200]
    return info


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
