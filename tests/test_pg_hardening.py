"""Phase 19 tests: PostgreSQL runtime hardening (TLS, migrations, transient errors)."""
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from sqlalchemy.exc import OperationalError  # noqa: E402

from app import Admin, app, db  # noqa: E402
from panel.core import db_pool  # noqa: E402

TLS_ENV = ("EVE_DB_SSLMODE", "EVE_DB_SSLROOTCERT", "EVE_DB_SSLCERT",
           "EVE_DB_SSLKEY", "EVE_DB_MIGRATION_APPLICATION_NAME")
REMOTE = "postgresql://eve:secret@db.example.com:5432/eve"


class TlsOptionTests(unittest.TestCase):
    def setUp(self):
        for name in TLS_ENV:
            os.environ.pop(name, None)

    def test_sslmode_reaches_libpq(self):
        with mock.patch.dict(os.environ, {"EVE_DB_SSLMODE": "verify-full"}):
            options = db_pool.engine_options(REMOTE)
        self.assertEqual(options["connect_args"]["sslmode"], "verify-full")
        self.assertEqual(options["connect_args"]["application_name"], "eve")

    def test_certificate_material_paths_are_passed_through(self):
        with mock.patch.dict(os.environ, {
                "EVE_DB_SSLMODE": "verify-ca",
                "EVE_DB_SSLROOTCERT": "/etc/ssl/eve-root.crt",
                "EVE_DB_SSLCERT": "/etc/ssl/eve.crt",
                "EVE_DB_SSLKEY": "/etc/ssl/eve.key"}):
            connect_args = db_pool.engine_options(REMOTE)["connect_args"]
        self.assertEqual(connect_args["sslmode"], "verify-ca")
        self.assertEqual(connect_args["sslrootcert"], "/etc/ssl/eve-root.crt")
        self.assertEqual(connect_args["sslcert"], "/etc/ssl/eve.crt")
        self.assertEqual(connect_args["sslkey"], "/etc/ssl/eve.key")

    def test_unknown_sslmode_is_ignored(self):
        with mock.patch.dict(os.environ, {"EVE_DB_SSLMODE": "sort-of-maybe"}):
            connect_args = db_pool.engine_options(REMOTE)["connect_args"]
        self.assertNotIn("sslmode", connect_args)

    def test_sqlite_never_gets_tls_connect_args(self):
        with mock.patch.dict(os.environ, {"EVE_DB_SSLMODE": "require"}):
            options = db_pool.engine_options("sqlite:////tmp/eve.db")
        self.assertNotIn("connect_args", options)


class TlsAuditTests(unittest.TestCase):
    def setUp(self):
        for name in TLS_ENV:
            os.environ.pop(name, None)

    def test_remote_postgres_without_tls_is_flagged(self):
        info = db_pool.audit(REMOTE)
        self.assertEqual(info["host"], "db.example.com")
        self.assertIsNone(info["sslmode"])
        self.assertIn("transport encryption", info["tls_warning"])

    def test_prefer_is_flagged_as_a_silent_fallback(self):
        with mock.patch.dict(os.environ, {"EVE_DB_SSLMODE": "prefer"}):
            info = db_pool.audit(REMOTE)
        self.assertIn("falls back", info["tls_warning"])

    def test_require_and_verify_full_are_accepted(self):
        for mode in ("require", "verify-ca", "verify-full"):
            with mock.patch.dict(os.environ, {"EVE_DB_SSLMODE": mode}):
                info = db_pool.audit(REMOTE)
            self.assertIsNone(info["tls_warning"], mode)
            self.assertEqual(info["sslmode"], mode)

    def test_local_postgres_and_sqlite_are_not_flagged(self):
        self.assertIsNone(db_pool.audit("postgresql://eve:secret@localhost/eve")["tls_warning"])
        self.assertIsNone(db_pool.audit("postgresql://eve:secret@127.0.0.1/eve")["tls_warning"])
        sqlite = db_pool.audit("sqlite:////tmp/eve.db")
        self.assertIsNone(sqlite["tls_warning"])
        self.assertEqual(sqlite["dialect"], "sqlite")

    def test_host_parsing_covers_ports_and_ipv6(self):
        self.assertEqual(db_pool.host_of("postgresql://u:p@db.internal:6432/eve"), "db.internal")
        self.assertEqual(db_pool.host_of("postgresql://u:p@[::1]:5432/eve"), "::1")
        self.assertEqual(db_pool.host_of("sqlite:////tmp/eve.db"), "")
        self.assertTrue(db_pool.is_local_host("postgresql://u:p@[::1]:5432/eve"))
        self.assertFalse(db_pool.is_local_host(REMOTE))


class AlembicOptionTests(unittest.TestCase):
    def setUp(self):
        for name in TLS_ENV:
            os.environ.pop(name, None)

    def test_pool_sizing_is_dropped_but_tls_and_name_survive(self):
        with mock.patch.dict(os.environ, {"EVE_DB_SSLMODE": "require"}):
            options = db_pool.alembic_engine_options(REMOTE)
        for key in ("pool_size", "max_overflow", "pool_timeout", "pool_recycle",
                    "pool_use_lifo"):
            self.assertNotIn(key, options)
        self.assertTrue(options["pool_pre_ping"])
        self.assertEqual(options["connect_args"]["sslmode"], "require")
        self.assertEqual(options["connect_args"]["application_name"], "eve-migrate")

    def test_migration_application_name_is_configurable(self):
        with mock.patch.dict(os.environ, {"EVE_DB_MIGRATION_APPLICATION_NAME": "eve-upgrade"}):
            options = db_pool.alembic_engine_options(REMOTE)
        self.assertEqual(options["connect_args"]["application_name"], "eve-upgrade")

    def test_sqlite_migration_options_are_minimal(self):
        self.assertEqual(db_pool.alembic_engine_options("sqlite:////tmp/eve.db"),
                         {"pool_pre_ping": True})

    def test_alembic_env_builds_the_engine_from_db_pool(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "alembic", "env.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("alembic_engine_options", source)
        self.assertIn("create_engine", source)


class PgHealthTests(unittest.TestCase):
    class _BrokenEngine:
        class dialect:
            name = "postgresql"

        def connect(self):
            raise RuntimeError("the database system is starting up")

    class _NoDialect:
        @property
        def dialect(self):
            raise RuntimeError("engine is gone")

    def test_non_postgres_is_skipped(self):
        from sqlalchemy import create_engine
        engine = create_engine("sqlite:///:memory:")
        try:
            self.assertEqual(db_pool.pg_health(engine),
                             {"state": "skipped", "dialect": "sqlite"})
        finally:
            engine.dispose()

    def test_broken_connection_is_reported_not_raised(self):
        info = db_pool.pg_health(self._BrokenEngine())
        self.assertEqual(info["state"], "error")
        self.assertIn("starting up", info["error"])
        self.assertIsNone(info["max_connections"])

    def test_engine_without_a_dialect_is_unknown(self):
        self.assertEqual(db_pool.pg_health(self._NoDialect())["state"], "unknown")


class TransientErrorTests(unittest.TestCase):
    def test_connection_level_failures_are_transient(self):
        for message in ("server closed the connection unexpectedly",
                        "could not connect to server: Connection refused",
                        "FATAL:  too many clients already",
                        "SSL connection has been closed unexpectedly",
                        "sqlite3.OperationalError: database is locked",
                        "terminating connection due to administrator command"):
            self.assertTrue(db_pool.is_transient_disconnect(Exception(message)), message)

    def test_query_level_failures_are_not_transient(self):
        for message in ("no such table: admins",
                        "syntax error at or near SELET",
                        "UNIQUE constraint failed: admins.username",
                        ""):
            self.assertFalse(db_pool.is_transient_disconnect(Exception(message)), message)

    def test_operational_error_is_classified_by_its_original_error(self):
        transient = OperationalError("SELECT 1", {}, Exception("connection refused"))
        self.assertTrue(db_pool.is_transient_disconnect(transient))
        bug = OperationalError("SELET 1", {}, Exception("syntax error at or near SELET"))
        self.assertFalse(db_pool.is_transient_disconnect(bug))


class DatabaseErrorHandlerTests(unittest.TestCase):
    def test_transient_disconnect_becomes_a_retryable_503(self):
        error = OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))
        with app.test_request_context("/api/anything",
                                      headers={"Accept": "application/json"}):
            result = app.handle_user_exception(error)
        response, status = (result if isinstance(result, tuple) else (result, result.status_code))
        self.assertEqual(status, 503)
        self.assertEqual(response.headers.get("Retry-After"), "2")
        self.assertFalse(response.get_json()["success"])

    def test_query_bug_is_not_masked_as_backpressure(self):
        error = OperationalError("SELET 1", {}, Exception("syntax error at or near SELET"))
        with app.test_request_context("/api/anything",
                                      headers={"Accept": "application/json"}):
            result = app.handle_user_exception(error)
        response, status = (result if isinstance(result, tuple) else (result, result.status_code))
        self.assertEqual(status, 500)
        self.assertIsNone(response.headers.get("Retry-After"))


class DoctorPostgresCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.admin = Admin(username="pg-doctor", role="admin", enabled=True)
        cls.admin.set_password("CorrectHorseBattery1!")
        db.session.add(cls.admin)
        db.session.commit()
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def _login(self):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = self.admin.id
            sess["role"] = "admin"
            sess["is_superadmin"] = False

    def test_doctor_reports_the_database_tls_posture(self):
        self._login()
        response = self.client.get("/api/doctor")
        self.assertEqual(response.status_code, 200, response.data)
        check = response.get_json()["checks"]["postgres"]
        self.assertEqual(check["state"], "ok")
        self.assertEqual(check["detail"], "sqlite deployment")


if __name__ == "__main__":
    unittest.main()
