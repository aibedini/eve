"""Alembic environment for the EVE panel.

The database URL comes from DATABASE_URL (same resolution as app.py);
the metadata comes from panel.models via the shared panel.extensions db.

The engine is built with panel.core.db_pool so an upgrade reaches PostgreSQL with
the deployment's TLS policy, an identifiable application_name ('eve-migrate') and
the configured statement timeout -- a long migration must not look like an
anonymous idle backend in pg_stat_activity.
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from panel.core.db_pool import alembic_engine_options

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    url = (os.environ.get('DATABASE_URL') or '').strip()
    if url.startswith('postgres://'):
        url = 'postgresql://' + url[len('postgres://'):]
    if not url:
        instance_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'instance')
        os.makedirs(instance_path, exist_ok=True)
        url = f"sqlite:///{os.path.join(instance_path, 'servers.db')}"
    return url


config.set_main_option('sqlalchemy.url', _resolve_url())

from panel.extensions import db  # noqa: E402
import panel.models  # noqa: E402,F401 — registers every table on db.metadata

target_metadata = db.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option('sqlalchemy.url'),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
        render_as_batch=True,  # SQLite-safe ALTERs
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option('sqlalchemy.url')
    connectable = create_engine(
        url,
        poolclass=pool.NullPool,
        **alembic_engine_options(url),
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite-safe ALTERs
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
