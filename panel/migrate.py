"""Schema migration runner: single-process, file-locked, Alembic-tracked.

History: schema changes used to run as an unguarded swarm of ALTERs at import
time in every process (web workers, workers, tests), which raced and once left
tables half-migrated (see CHANGELOG 2.5.1). Now:

- ``run_migrations()`` is the ONLY entry point. A cross-process file lock
  serializes concurrent starters (gunicorn workers, web+background containers).
- ``db.create_all()`` + the legacy per-column ALTER catch-up stay idempotent
  and run first, so pre-Alembic databases reach the current schema.
- Afterwards the Alembic ledger is stamped to the baseline, and any NEWER
  revisions in ``alembic/versions/`` are applied via ``upgrade head``.
  All future schema changes must be Alembic revisions, not runtime ALTERs.
- Initial seed data (admin, panel APIs, sub-app configs, system config) runs
  last and is likewise idempotent.

Skip via ``EVE_SKIP_IMPORT_MIGRATIONS=1`` (set by the Docker entrypoint for
gunicorn/background processes after it has run ``python -m panel.migrate``).
"""
import os
import secrets
import tempfile
from contextlib import contextmanager

from sqlalchemy import inspect, text

from panel.extensions import db
from panel.models import Admin, PanelAPI, SubAppConfig, SystemConfig, SystemSetting

MIGRATION_LOCK_PATH = os.path.join(tempfile.gettempdir(), 'eve_schema_migrate.lock')
ALEMBIC_INI = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'alembic.ini')


@contextmanager
def _migration_lock():
    """Cross-process exclusive lock (fcntl on POSIX, msvcrt on Windows)."""
    f = open(MIGRATION_LOCK_PATH, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == 'nt':
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def _migrate_add_columns(table_name, columns):
    """Idempotently add columns to an existing table, ONE guard per column.

    Each ALTER gets its own try/except: a single failure (two gunicorn workers
    racing the same startup migration, a transient lock, a bad type on one
    dialect) must never skip the remaining columns — a partially applied
    migration is worse than a loud one.
    """
    try:
        inspector = inspect(db.engine)
        if table_name not in set(inspector.get_table_names()):
            return
        existing = {c['name'] for c in inspector.get_columns(table_name)}
    except Exception as exc:
        print(f"Migration error ({table_name} inspect): {exc}")
        return
    for col_name, col_def in columns:
        if col_name in existing:
            continue
        try:
            with db.engine.connect() as conn:
                conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}'))
                conn.commit()
        except Exception as exc:
            print(f"Migration error ({table_name}.{col_name}): {exc}")


def _legacy_column_catchup():
    db.create_all()

    # Ensure expected columns exist on admins table (older DBs)
    try:
        inspector = inspect(db.engine)
        columns = [c['name'] for c in inspector.get_columns('admins')]
        print(f"Current columns in admins: {columns}")

        _is_pg = db.engine.dialect.name == 'postgresql'
        admin_missing_cols = [
            ('telegram_id',           'VARCHAR(100)'),
            ('support_telegram',      'VARCHAR(100)'),
            ('support_whatsapp',      'VARCHAR(64)'),
            ('channel_telegram',      'TEXT'),
            ('channel_whatsapp',      'TEXT'),
            ('allow_negative_credit', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
            ('negative_credit_limit', 'INTEGER DEFAULT 0'),
            ('allow_free_creation', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
            ('whatsapp_automation_enabled', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
            ('sub_shown_package_ids', "TEXT DEFAULT '[]'"),
            ('support_sms', 'VARCHAR(64)'),
        ]

        for col_name, col_type in admin_missing_cols:
            if col_name in columns:
                continue
            print(f"{col_name} column missing on admins, attempting to add...")
            try:
                with db.engine.connect() as conn:
                    conn.execute(text(f'ALTER TABLE admins ADD COLUMN {col_name} {col_type}'))
                    conn.commit()
                print(f"Added {col_name} column to admins table")
            except Exception as _col_err:
                print(f"Migration error ({col_name}): {_col_err}")
    except Exception as e:
        print(f"Migration error: {e}")

    # Delivery tracking returned by GMweb /send and /send/status/:requestId.
    # Existing installations already have sms_send_log, so add columns in place.
    try:
        inspector = inspect(db.engine)
        if 'sms_send_log' in set(inspector.get_table_names()):
            _sms_cols = {c['name'] for c in inspector.get_columns('sms_send_log')}
            _sms_new_cols = (
                ('request_id', 'VARCHAR(128)'),
                ('gateway_job_id', 'VARCHAR(64)'),
                ('status_url', 'VARCHAR(512)'),
                ('gateway_state', 'VARCHAR(32)'),
                ('stage', 'VARCHAR(64)'),
                ('terminal', 'BOOLEAN'),
                ('successful', 'BOOLEAN'),
                ('gateway_current_at', 'VARCHAR(64)'),
                ('gateway_sent_at', 'VARCHAR(64)'),
                ('segment_count', 'INTEGER'),
                ('message_encoding', 'VARCHAR(16)'),
                ('unit_count', 'INTEGER'),
                ('character_count', 'INTEGER'),
                ('updated_at', 'TIMESTAMP'),
            )
            for _cn, _ct in _sms_new_cols:
                if _cn not in _sms_cols:
                    with db.engine.connect() as conn:
                        conn.execute(text(f'ALTER TABLE sms_send_log ADD COLUMN {_cn} {_ct}'))
                        conn.commit()
                    print(f'Migration: added sms_send_log.{_cn}')
            with db.engine.connect() as conn:
                conn.execute(text(
                    'CREATE INDEX IF NOT EXISTS ix_sms_send_log_request_id '
                    'ON sms_send_log (request_id)'
                ))
                conn.commit()
    except Exception as _sms_migration_error:
        print(f'Migration error (sms_send_log delivery tracking): {_sms_migration_error}')

    # Durable Telegram support attachments for existing installations.
    try:
        inspector = inspect(db.engine)
        if 'telegram_service_request_messages' in set(inspector.get_table_names()):
            _support_message_cols = {
                c['name'] for c in inspector.get_columns('telegram_service_request_messages')
            }
            _support_message_new_cols = (
                ('attachment_kind', 'VARCHAR(24)'),
                ('attachment_file_id', 'TEXT'),
                ('attachment_file_unique_id', 'VARCHAR(255)'),
                ('attachment_name', 'VARCHAR(255)'),
                ('attachment_mime', 'VARCHAR(127)'),
                ('attachment_size', 'BIGINT'),
                ('source_chat_id', 'BIGINT'),
                ('source_message_id', 'BIGINT'),
            )
            for _cn, _ct in _support_message_new_cols:
                if _cn not in _support_message_cols:
                    with db.engine.connect() as conn:
                        conn.execute(text(
                            f'ALTER TABLE telegram_service_request_messages ADD COLUMN {_cn} {_ct}'
                        ))
                        conn.commit()
                    print(f'Migration: added telegram_service_request_messages.{_cn}')
    except Exception as _support_message_migration_error:
        print(f'Migration error (Telegram support attachments): {_support_message_migration_error}')

    # Optional Telegram support group and per-ticket forum topic routing.
    try:
        inspector = inspect(db.engine)
        _telegram_tables = set(inspector.get_table_names())
        _support_routing_tables = {
            'telegram_bot_instances': (
                ('support_group_enabled', 'BOOLEAN DEFAULT FALSE'),
                ('support_group_chat_id', 'BIGINT'),
                ('support_group_topics', 'BOOLEAN DEFAULT TRUE'),
                ('support_sla_minutes', 'INTEGER DEFAULT 60'),
                ('support_sla_warning_percent', 'INTEGER DEFAULT 80'),
                ('support_escalation_minutes', 'INTEGER DEFAULT 30'),
            ),
            'telegram_service_requests': (
                ('support_group_chat_id', 'BIGINT'),
                ('support_message_thread_id', 'BIGINT'),
                ('support_group_message_id', 'BIGINT'),
                ('assigned_admin_id', 'INTEGER'),
                ('support_priority', "VARCHAR(16) DEFAULT 'normal'"),
                ('first_response_at', 'TIMESTAMP'),
                ('sla_warning_message_id', 'INTEGER'),
                ('sla_escalated_message_id', 'INTEGER'),
                ('sla_warning_at', 'TIMESTAMP'),
                ('sla_escalated_at', 'TIMESTAMP'),
            ),
        }
        for _table, _columns in _support_routing_tables.items():
            if _table not in _telegram_tables:
                continue
            _existing = {c['name'] for c in inspector.get_columns(_table)}
            for _column, _column_type in _columns:
                if _column in _existing:
                    continue
                with db.engine.connect() as conn:
                    conn.execute(text(f'ALTER TABLE {_table} ADD COLUMN {_column} {_column_type}'))
                    conn.commit()
                print(f'Migration: added {_table}.{_column}')
        with db.engine.connect() as conn:
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_telegram_service_requests_assigned_admin_id '
                'ON telegram_service_requests (assigned_admin_id)'
            ))
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_telegram_service_requests_support_priority '
                'ON telegram_service_requests (support_priority)'
            ))
            conn.commit()
    except Exception as _support_routing_migration_error:
        print(f'Migration error (Telegram support group routing): {_support_routing_migration_error}')

    # Customer wallet and card-to-card renewal/top-up payment metadata.
    _migrate_add_columns('customer_accounts', [
        ('credit', 'INTEGER DEFAULT 0'),
    ])
    _migrate_add_columns('telegram_service_requests', [
        ('bank_card_id', 'INTEGER'),
        ('receipt_file_id', 'TEXT'),
        ('receipt_file_kind', 'VARCHAR(16)'),
        ('receipt_file_unique_id', 'VARCHAR(160)'),
        ('duplicate_receipt', 'BOOLEAN DEFAULT FALSE'),
        ('payment_method', "VARCHAR(16) DEFAULT 'card'"),
    ])
    _migrate_add_columns('telegram_purchase_requests', [
        ('payment_method', "VARCHAR(16) DEFAULT 'card'"),
    ])
    _migrate_add_columns('pulse_runs', [
        ('params_json', 'TEXT'),
    ])
    _migrate_add_columns('pulse_templates', [
        ('download_bytes', 'INTEGER DEFAULT 10000000'),
        ('upload_bytes', 'INTEGER DEFAULT 2000000'),
    ])

    # Ensure announcements columns exist — each in its own try so one failure
    # doesn't prevent the others from running.
    def _ensure_ann_col(col_name, col_type):
        try:
            inspector = inspect(db.engine)
            if 'announcements' not in set(inspector.get_table_names()):
                return
            cols = [c['name'] for c in inspector.get_columns('announcements')]
            if col_name in cols:
                return
            with db.engine.connect() as conn:
                conn.execute(text(f'ALTER TABLE announcements ADD COLUMN {col_name} {col_type}'))
                conn.commit()
            print(f"Migration: added announcements.{col_name}")
        except Exception as _e:
            print(f"Migration error (announcements.{col_name}): {_e}")

    _is_pg = db.engine.dialect.name == 'postgresql'
    _bool_def = 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'
    _ensure_ann_col('hide_from_resellers', _bool_def)
    _ensure_ann_col('is_popup',            _bool_def)
    _ensure_ann_col('button_text',         'VARCHAR(120)')

    # Ensure owner_id exists on notification_templates (per-reseller templates)
    try:
        inspector = inspect(db.engine)
        nt_cols = [c['name'] for c in inspector.get_columns('notification_templates')]
        if 'owner_id' not in nt_cols:
            print("owner_id column missing on notification_templates, adding...")
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE notification_templates ADD COLUMN owner_id INTEGER REFERENCES admins(id)'))
                conn.commit()
            print("Added owner_id to notification_templates")
    except Exception as e:
        print(f"Migration error (notification_templates.owner_id): {e}")

    # Ensure sender_name exists on transactions table (older DBs)
    try:
        inspector = inspect(db.engine)
        tx_columns = [c['name'] for c in inspector.get_columns('transactions')]
        if 'sender_name' not in tx_columns:
            print("sender_name column missing on transactions, attempting to add...")
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE transactions ADD COLUMN sender_name VARCHAR(120)'))
                conn.commit()
            print("Added sender_name column to transactions table")
    except Exception as e:
        print(f"Migration error (transactions.sender_name): {e}")

    # Ensure reseller-statement columns exist on transactions (older DBs)
    try:
        inspector = inspect(db.engine)
        tx_columns = [c['name'] for c in inspector.get_columns('transactions')]
        for _cn, _cd in (('package_name', 'VARCHAR(120)'), ('volume_gb', 'INTEGER'), ('days', 'INTEGER')):
            if _cn not in tx_columns:
                with db.engine.connect() as conn:
                    conn.execute(text(f'ALTER TABLE transactions ADD COLUMN {_cn} {_cd}'))
                    conn.commit()
                print(f"Added {_cn} column to transactions table")
    except Exception as e:
        print(f"Migration error (transactions.package_name/volume_gb/days): {e}")

    # Ensure servers.hidden column exists (added for server hide/show feature)
    try:
        inspector = inspect(db.engine)
        srv_cols = [c['name'] for c in inspector.get_columns('servers')]
        if 'hidden' not in srv_cols:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE servers ADD COLUMN hidden BOOLEAN DEFAULT FALSE'))
                conn.commit()
            print("Added hidden column to servers table")
        if 'api_token' not in srv_cols:
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE servers ADD COLUMN api_token VARCHAR(255)'))
                conn.commit()
            print("Added api_token column to servers table")
    except Exception as e:
        print(f"Migration error (servers.hidden/api_token): {e}")

    # Ensure system_configs.value can store long URLs (PostgreSQL only)
    try:
        if db.engine.dialect.name == 'postgresql':
            with db.engine.connect() as conn:
                conn.execute(text('ALTER TABLE system_configs ALTER COLUMN value TYPE TEXT'))
                conn.commit()
            print("Ensured system_configs.value is TEXT")
    except Exception as e:
        print(f"Migration error (system_configs.value TEXT): {e}")

    # Widen phone columns for international numbers (up to 15 E.164 digits).
    # PostgreSQL only; on SQLite VARCHAR length is not enforced (type affinity),
    # so this is a documented no-op there.
    try:
        if db.engine.dialect.name == 'postgresql':
            with db.engine.connect() as conn:
                for _table, _column in (
                        ('telegram_identities', 'phone_normalized'),
                        ('customer_accounts', 'primary_phone'),
                        ('telegram_trial_grants', 'phone_normalized'),
                        ('ownership_claims', 'verified_phone')):
                    conn.execute(text(
                        f'ALTER TABLE {_table} ALTER COLUMN {_column} TYPE VARCHAR(20)'))
                conn.commit()
            print("Ensured phone columns are VARCHAR(20)")
    except Exception as e:
        print(f"Migration error (phone columns VARCHAR(20)): {e}")

    # Ensure announcements.targets exists (SQLite old DBs)
    try:
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names() or [])
        if 'announcements' in tables:
            ann_columns = [c['name'] for c in inspector.get_columns('announcements')]
            if 'targets' not in ann_columns:
                print("announcements.targets column missing, attempting to add...")
                with db.engine.connect() as conn:
                    conn.execute(text('ALTER TABLE announcements ADD COLUMN targets TEXT'))
                    conn.commit()
                print("Added targets column to announcements table")
    except Exception as e:
        print(f"Migration error (announcements.targets): {e}")

    # Auto-detect SSL paths at startup if DB is empty (handles case where setup.sh ran but Flask hadn't started)
    try:
        from app import _autodetect_ssl_paths  # deferred: app-level helper, avoids circular import
        _c = db.session.get(SystemSetting, 'ssl_cert_path')
        _k = db.session.get(SystemSetting, 'ssl_key_path')
        if not (_c and _c.value) and not (_k and _k.value):
            _det_cert, _det_key = _autodetect_ssl_paths()
            if _det_cert and _det_key:
                db.session.merge(SystemSetting(key='ssl_cert_path', value=_det_cert))
                db.session.merge(SystemSetting(key='ssl_key_path', value=_det_key))
                db.session.commit()
                print(f"[startup] Auto-detected SSL paths: {_det_cert}")
    except Exception as _ssl_e:
        print(f"[startup] SSL auto-detect error: {_ssl_e}")

    # Ensure packages table has extended columns (scope, assigned_reseller_ids, etc.)
    # TIMESTAMP works in both PostgreSQL and SQLite; DATETIME is SQLite-only
    _ts_type = 'TIMESTAMP' if db.engine.dialect.name == 'postgresql' else 'DATETIME'
    _migrate_add_columns('packages', [
        ('scope', "VARCHAR(20) DEFAULT 'global'"),
        ('assigned_reseller_ids', "TEXT DEFAULT '[]'"),
        ('created_by', 'INTEGER'),
        ('display_order', 'INTEGER DEFAULT 0'),
        ('show_on_sub', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
        ('is_trial', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
        ('created_at', _ts_type),
        ('updated_at', _ts_type),
    ])

    # Ensure price_tiers supports assigning one dynamic rule to multiple resellers.
    _migrate_add_columns('price_tiers', [
        ('assigned_reseller_ids', "TEXT DEFAULT '[]'"),
    ])

    # Ensure bank_cards supports reseller ownership (reseller_id, assigned_reseller_ids)
    _migrate_add_columns('bank_cards', [
        ('reseller_id', 'INTEGER'),
        ('assigned_reseller_ids', "TEXT DEFAULT '[]'"),
    ])

    # Ensure telegram_bot_instances supports soft-archive lifecycle
    _migrate_add_columns('telegram_bot_instances', [
        ('archived_at', 'TIMESTAMP' if db.engine.dialect.name == 'postgresql' else 'DATETIME'),
        ('archived_by_admin_id', 'INTEGER'),
        ('copy_overrides_json', "TEXT DEFAULT ''"),
        ('required_channels_json', "TEXT DEFAULT '[]'"),
        ('require_membership_on_start', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
        ('require_membership_on_delivery', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
        ('phone_allow_international', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
    ])

    # Ensure telegram_purchase_policies supports trial and emergency access
    _migrate_add_columns('telegram_purchase_policies', [
        ('trial_enabled', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
        ('trial_package_id', 'INTEGER'),
        ('trial_requires_channel_membership', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
        ('trial_channel_chat_id', 'BIGINT' if _is_pg else 'INTEGER'),
        ('trial_channel_list_json', "TEXT DEFAULT ''"),
        ('emergency_enabled', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
        ('emergency_days', 'INTEGER DEFAULT 1'),
        ('emergency_volume_gb', 'INTEGER DEFAULT 1'),
        ('emergency_cooldown_days', 'INTEGER DEFAULT 30'),
    ])

    # Ensure telegram purchase tables support quoted-amount freeze and receipt fraud flags
    _migrate_add_columns('telegram_purchase_sessions', [
        ('quoted_amount', 'INTEGER'),
        ('promo_id', 'INTEGER'),
        ('promo_code', 'VARCHAR(64)'),
        ('discount_amount', 'INTEGER'),
        ('promo_discounts_json', 'TEXT'),
    ])
    _migrate_add_columns('telegram_purchase_requests', [
        ('duplicate_receipt', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
        ('original_amount', 'BIGINT' if _is_pg else 'INTEGER'),
        ('discount_amount', 'BIGINT' if _is_pg else 'INTEGER'),
        ('promo_code', 'VARCHAR(64)'),
    ])
    _migrate_add_columns('telegram_service_requests', [
        ('original_amount', 'BIGINT' if _is_pg else 'INTEGER'),
        ('discount_amount', 'BIGINT' if _is_pg else 'INTEGER'),
        ('promo_code', 'VARCHAR(64)'),
    ])

    # Add icon_url and is_recommended to sub_app_configs (older DBs)
    try:
        inspector = inspect(db.engine)
        if 'sub_app_configs' in set(inspector.get_table_names()):
            _sac_cols = [c['name'] for c in inspector.get_columns('sub_app_configs')]
            _is_pg = db.engine.dialect.name == 'postgresql'
            _sac_new = [
                ('icon_url', 'VARCHAR(500)'),
                ('is_recommended', 'BOOLEAN DEFAULT FALSE' if _is_pg else 'BOOLEAN DEFAULT 0'),
                ('display_order', 'INTEGER DEFAULT 0'),
            ]
            for _cn, _cd in _sac_new:
                if _cn not in _sac_cols:
                    with db.engine.connect() as _conn:
                        _conn.execute(text(f'ALTER TABLE sub_app_configs ADD COLUMN {_cn} {_cd}'))
                        _conn.commit()
                    print(f"Added {_cn} column to sub_app_configs table")
    except Exception as _sac_e:
        print(f"Migration error (sub_app_configs new cols): {_sac_e}")


def _seed_initial_data():
    # Initialize PanelAPI data
    if not PanelAPI.query.first():
        panel_apis = [
            PanelAPI(
                panel_type='sanaei',
                display_name='3X-UI (Sanaei)',
                login_endpoint='/login',
                inbounds_list='/panel/api/inbounds/list',
                inbounds_get='/panel/api/inbounds/get/:id',
                inbounds_add='/panel/api/inbounds/add',
                inbounds_update='/panel/api/inbounds/update/:id',
                inbounds_delete='/panel/api/inbounds/del/:id',
                client_add='/panel/api/inbounds/addClient',
                client_update='/panel/api/inbounds/updateClient/:clientId',
                client_delete='/panel/api/inbounds/:id/delClient/:clientId',
                client_reset_traffic='/panel/api/inbounds/:id/resetClientTraffic/:email',
                client_get_traffic='/panel/api/inbounds/getClientTraffics/:email',
                server_status='/panel/api/server/status',
                server_restart='/panel/api/server/restartXrayService',
                server_stop='/panel/api/server/stopXrayService'
            ),
            PanelAPI(
                panel_type='alireza',
                display_name='X-UI (Alireza)',
                login_endpoint='/login',
                inbounds_list='/xui/API/inbounds/',
                inbounds_get='/xui/API/inbounds/get/:id',
                inbounds_add='/xui/API/inbounds/add',
                inbounds_update='/xui/API/inbounds/update/:id',
                inbounds_delete='/xui/API/inbounds/del/:id',
                client_add='/xui/API/inbounds/addClient/',
                client_update='/xui/API/inbounds/updateClient/:clientId',
                client_delete='/xui/API/inbounds/:id/delClient/:clientId',
                client_reset_traffic='/xui/API/inbounds/:id/resetClientTraffic/:email',
                client_get_traffic='/xui/API/inbounds/getClientTraffics/:email',
                server_status='/xui/API/server/status',
                server_restart='/xui/API/server/restartXrayService',
                server_stop='/xui/API/server/stopXrayService'
            )
        ]
        db.session.add_all(panel_apis)

    if Admin.query.count() == 0:
        initial_username = os.environ.get("INITIAL_ADMIN_USERNAME", "admin")
        default_admin = Admin(
            username=initial_username,
            is_superadmin=True,
            role='superadmin',
            enabled=True,
            allowed_servers='*'
        )
        initial_password = os.environ.get("INITIAL_ADMIN_PASSWORD")
        if not initial_password:
            initial_password = secrets.token_urlsafe(12)
            print("\n" + "!"*60)
            print("  CRITICAL SECURITY NOTICE")
            print(f"  Initial admin created with username: {initial_username}")
            print(f"  Generated secure password: {initial_password}")
            print("  PLEASE SAVE THIS PASSWORD IMMEDIATELY!")
            print("!"*60 + "\n")
        
        default_admin.set_password(initial_password)
        db.session.add(default_admin)
    
        if not SubAppConfig.query.first():
            apps_list = [
                # Android
                SubAppConfig(app_code='v2rayng', name='v2rayNG', os_type='android', title_fa='راهنمای v2rayNG', description_fa='۱. برنامه را دانلود کنید.\n۲. لینک سابسکریپشن را کپی کنید.\n۳. در برنامه روی + بزنید و Import from clipboard را انتخاب کنید.', title_en='v2rayNG Guide', description_en='1. Download the app.\n2. Copy the subscription link.\n3. Tap + then "Import from clipboard".', download_link='https://github.com/2dust/v2rayNG/releases/latest', store_link='https://play.google.com/store/apps/details?id=com.v2ray.ang'),
                SubAppConfig(app_code='nekobox', name='NekoBox', os_type='android', title_fa='راهنمای NekoBox', description_fa='۱. برنامه را نصب کنید.\n۲. از منو Profiles را انتخاب کنید.\n۳. روی + بزنید و Add from URL را انتخاب کنید.', title_en='NekoBox Guide', description_en='1. Install the app.\n2. Open Profiles from the menu.\n3. Tap + and select "Add from URL".', download_link='https://github.com/MatsuriDayo/NekoBoxForAndroid/releases/latest'),
                SubAppConfig(app_code='hiddify', name='Hiddify', os_type='android', title_fa='راهنمای Hiddify', description_fa='۱. برنامه را نصب کنید.\n۲. روی Add Profile بزنید.\n۳. لینک سابسکریپشن را وارد کنید.', title_en='Hiddify Guide', description_en='1. Install the app.\n2. Tap Add Profile.\n3. Enter the subscription link.', store_link='https://play.google.com/store/apps/details?id=app.hiddify.com', download_link='https://github.com/hiddify/hiddify-app/releases/latest'),
                SubAppConfig(app_code='v2raytun', name='V2RayTun', os_type='android', title_fa='راهنمای V2RayTun', description_fa='۱. برنامه را نصب کنید.\n۲. لینک ساب را اضافه کنید.', title_en='V2RayTun Guide', description_en='1. Install the app.\n2. Add the subscription link.', store_link='https://play.google.com/store/apps/details?id=com.v2raytun.android'),
                SubAppConfig(app_code='matsuri', name='Matsuri', os_type='android', title_fa='راهنمای Matsuri', description_fa='۱. برنامه را نصب کنید.\n۲. لینک ساب را اضافه کنید.', title_en='Matsuri Guide', description_en='1. Install the app.\n2. Add the subscription link.', download_link='https://github.com/MatsuriDayo/Matsuri/releases/latest'),
                SubAppConfig(app_code='surfboard', name='Surfboard', os_type='android', title_fa='راهنمای Surfboard', description_fa='۱. برنامه را نصب کنید.\n۲. Config را Import کنید.', title_en='Surfboard Guide', description_en='1. Install the app.\n2. Import config.', store_link='https://play.google.com/store/apps/details?id=com.getsurfboard'),
                # iOS
                SubAppConfig(app_code='streisand', name='Streisand', os_type='ios', title_fa='راهنمای Streisand', description_fa='۱. از App Store نصب کنید.\n۲. روی + بزنید و URL را وارد کنید.', title_en='Streisand Guide', description_en='1. Install from App Store.\n2. Tap + and enter the subscription URL.', store_link='https://apps.apple.com/us/app/streisand/id6450534064'),
                SubAppConfig(app_code='shadowrocket', name='Shadowrocket', os_type='ios', title_fa='راهنمای Shadowrocket', description_fa='۱. از App Store نصب کنید (نیاز به اکانت خارجی).\n۲. روی + بزنید، Type را Subscribe انتخاب کنید و URL را وارد کنید.', title_en='Shadowrocket Guide', description_en='1. Install from App Store (requires foreign account).\n2. Tap +, select Type: Subscribe, enter the URL.', store_link='https://apps.apple.com/us/app/shadowrocket/id932747118'),
                SubAppConfig(app_code='foxray', name='FoXray', os_type='ios', title_fa='راهنمای FoXray', description_fa='۱. از App Store نصب کنید.\n۲. لینک ساب را اضافه کنید.', title_en='FoXray Guide', description_en='1. Install from App Store.\n2. Add the subscription link.', store_link='https://apps.apple.com/us/app/foxray/id6448898396'),
                SubAppConfig(app_code='v2box', name='v2Box', os_type='ios', title_fa='راهنمای v2Box', description_fa='۱. از App Store نصب کنید.\n۲. لینک ساب را اضافه کنید.', title_en='v2Box Guide', description_en='1. Install from App Store.\n2. Add the subscription link.', store_link='https://apps.apple.com/us/app/v2box-v2ray-client/id6446814690'),
                SubAppConfig(app_code='hiddify-ios', name='Hiddify (iOS)', os_type='ios', title_fa='راهنمای Hiddify آیفون', description_fa='۱. از App Store نصب کنید.\n۲. روی Add Profile بزنید و لینک را وارد کنید.', title_en='Hiddify iOS Guide', description_en='1. Install from App Store.\n2. Tap Add Profile and enter the link.', store_link='https://apps.apple.com/us/app/hiddify-proxy-vpn/id6596777532'),
                # Windows
                SubAppConfig(app_code='v2rayn', name='v2rayN', os_type='windows', title_fa='راهنمای v2rayN', description_fa='۱. از گیتهاب دانلود کنید.\n۲. برنامه را اجرا کنید.\n۳. از منو Servers > Add subscription server را انتخاب کنید و URL را وارد کنید.', title_en='v2rayN Guide', description_en='1. Download from GitHub.\n2. Run the app.\n3. Go to Servers > Add subscription server and enter the URL.', download_link='https://github.com/2dust/v2rayN/releases/latest'),
                SubAppConfig(app_code='nekoray', name='Nekoray', os_type='windows', title_fa='راهنمای Nekoray', description_fa='۱. از گیتهاب دانلود کنید.\n۲. از منو Program > Add profile from URL استفاده کنید.', title_en='Nekoray Guide', description_en='1. Download from GitHub.\n2. Use Program > Add profile from URL.', download_link='https://github.com/MatsuriDayo/nekoray/releases/latest'),
                SubAppConfig(app_code='clashverge', name='Clash Verge Rev', os_type='windows', title_fa='راهنمای Clash Verge', description_fa='۱. دانلود و نصب کنید.\n۲. روی Profiles بزنید و URL را وارد کنید.', title_en='Clash Verge Guide', description_en='1. Download and install.\n2. Click Profiles and enter the URL.', download_link='https://github.com/clash-verge-rev/clash-verge-rev/releases/latest'),
                SubAppConfig(app_code='flclash', name='FlClash', os_type='windows', title_fa='راهنمای FlClash', description_fa='۱. دانلود و نصب کنید.\n۲. پروفایل را اضافه کنید.', title_en='FlClash Guide', description_en='1. Download and install.\n2. Add profile.', download_link='https://github.com/chen08209/FlClash/releases/latest'),
                # Desktop (multi-platform)
                SubAppConfig(app_code='hiddify-desktop', name='Hiddify Desktop', os_type='desktop', title_fa='راهنمای Hiddify دسکتاپ', description_fa='۱. دانلود و نصب کنید.\n۲. روی Add Profile بزنید و لینک ساب را وارد کنید.', title_en='Hiddify Desktop Guide', description_en='1. Download and install.\n2. Tap Add Profile and enter the subscription link.', download_link='https://github.com/hiddify/hiddify-app/releases/latest'),
                SubAppConfig(app_code='v2raya', name='v2rayA', os_type='desktop', title_fa='راهنمای v2rayA', description_fa='رابط وب‌محور. پس از نصب از مرورگر باز کنید.', title_en='v2rayA Guide', description_en='Web-based UI. Open in browser after installation.', download_link='https://github.com/v2rayA/v2rayA/releases/latest'),
                SubAppConfig(app_code='sing-box', name='sing-box', os_type='desktop', title_fa='راهنمای sing-box', description_fa='کلاینت چندپلتفرمی با پشتیبانی گسترده از پروتکل‌ها.', title_en='sing-box Guide', description_en='Multi-platform client with broad protocol support.', download_link='https://github.com/SagerNet/sing-box/releases/latest'),
            ]
            db.session.add_all(apps_list)
    
        if not SystemConfig.query.filter_by(key='cost_per_gb').first():
            db.session.add(SystemConfig(key='cost_per_gb', value='2000'))
        if not SystemConfig.query.filter_by(key='cost_per_day').first():
            db.session.add(SystemConfig(key='cost_per_day', value='500'))
    
        db.session.commit()

def _ensure_alembic_current():
    """Stamp pre-Alembic databases at the baseline, then apply new revisions."""
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext

    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option('sqlalchemy.url', str(db.engine.url))
    with db.engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current is None:
        # Schema is already current via create_all + legacy catch-up above —
        # adopt the Alembic ledger without replaying the baseline.
        command.stamp(cfg, 'head')
    else:
        command.upgrade(cfg, 'head')


def run_migrations():
    """Idempotent, process-safe schema setup. Call inside an app context."""
    with _migration_lock():
        db.create_all()
        _legacy_column_catchup()
        _ensure_alembic_current()
        _seed_initial_data()


if __name__ == '__main__':
    os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'
    from app import app as _flask_app
    with _flask_app.app_context():
        run_migrations()
    print('[migrate] schema is up to date.')
