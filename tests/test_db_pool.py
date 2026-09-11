"""Phase 16 tests: database connection pool policy, reuse and starvation."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError  # noqa: E402

from app import app, db  # noqa: E402
from panel.core import db_pool  # noqa: E402


class EngineOptionTests(unittest.TestCase):
    def setUp(self):
        for name in ("EVE_DB_POOL_SIZE", "EVE_DB_MAX_OVERFLOW", "EVE_DB_POOL_TIMEOUT",
                     "EVE_DB_POOL_RECYCLE", "EVE_DB_POOL_USE_LIFO",
                     "EVE_DB_STATEMENT_TIMEOUT_MS", "EVE_DB_APPLICATION_NAME",
                     "EVE_DB_MAX_CONNECTIONS", "GUNICORN_WORKERS", "WEB_CONCURRENCY"):
            os.environ.pop(name, None)

    def test_sqlite_defaults(self):
        options = db_pool.engine_options("sqlite:////tmp/eve.db")
        self.assertEqual(options["pool_size"], 5)
        self.assertEqual(options["max_overflow"], 5)
        self.assertEqual(options["pool_timeout"], 10)
        self.assertEqual(options["pool_recycle"], 1800)
        self.assertTrue(options["pool_pre_ping"])
        self.assertNotIn("connect_args", options)
        self.assertNotIn("pool_use_lifo", options)

    def test_postgres_defaults_and_connect_args(self):
        options = db_pool.engine_options("postgresql://u:p@db.local/eve")
        self.assertEqual(options["pool_size"], 10)
        self.assertEqual(options["max_overflow"], 10)
        self.assertEqual(options["connect_args"]["application_name"], "eve")
        self.assertNotIn("options", options["connect_args"])

    def test_postgres_statement_timeout_and_application_name(self):
        with mock.patch.dict(os.environ, {
                "EVE_DB_STATEMENT_TIMEOUT_MS": "5000",
                "EVE_DB_APPLICATION_NAME": "eve-prod"}):
            options = db_pool.engine_options("postgres://u:p@db.local/eve")
        self.assertEqual(options["connect_args"]["options"], "-c statement_timeout=5000")
        self.assertEqual(options["connect_args"]["application_name"], "eve-prod")

    def test_memory_sqlite_omits_pool_sizing(self):
        options = db_pool.engine_options("sqlite:///:memory:")
        self.assertEqual(options, {"pool_pre_ping": True})

    def test_environment_overrides_and_invalid_values(self):
        with mock.patch.dict(os.environ, {
                "EVE_DB_POOL_SIZE": "7", "EVE_DB_MAX_OVERFLOW": "2",
                "EVE_DB_POOL_TIMEOUT": "3.5", "EVE_DB_POOL_RECYCLE": "60",
                "EVE_DB_POOL_USE_LIFO": "1"}):
            options = db_pool.engine_options("sqlite:////tmp/eve.db")
        self.assertEqual(options["pool_size"], 7)
        self.assertEqual(options["max_overflow"], 2)
        self.assertEqual(options["pool_timeout"], 3.5)
        self.assertEqual(options["pool_recycle"], 60)
        self.assertTrue(options["pool_use_lifo"])

        with mock.patch.dict(os.environ, {
                "EVE_DB_POOL_SIZE": "not-a-number",
                "EVE_DB_MAX_OVERFLOW": "-4",
                "EVE_DB_POOL_TIMEOUT": "0"}):
            fallback = db_pool.engine_options("sqlite:////tmp/eve.db")
        self.assertEqual(fallback["pool_size"], 5)
        self.assertEqual(fallback["max_overflow"], 5)
        self.assertEqual(fallback["pool_timeout"], 10)

    def test_validate_rejects_nonsense(self):
        with self.assertRaises(ValueError):
            db_pool.validate({"pool_size": 0})
        with self.assertRaises(ValueError):
            db_pool.validate({"max_overflow": -1})
        with self.assertRaises(ValueError):
            db_pool.validate({"pool_timeout": 0})
        db_pool.validate({"pool_size": 5, "max_overflow": 0, "pool_timeout": 1})

    def test_audit_computes_worker_demand_and_warns_when_oversized(self):
        with mock.patch.dict(os.environ, {
                "GUNICORN_WORKERS": "3", "EVE_DB_POOL_SIZE": "10",
                "EVE_DB_MAX_OVERFLOW": "10", "EVE_DB_MAX_CONNECTIONS": "50"}):
            info = db_pool.audit("postgresql://u:p@db.local/eve")
        self.assertEqual(info["workers"], 3)
        self.assertEqual(info["expected_max_connections"], 60)
        self.assertIn("EVE_DB_MAX_CONNECTIONS", info["warning"] or "")

    def test_audit_without_a_cap_has_no_warning(self):
        with mock.patch.dict(os.environ, {"GUNICORN_WORKERS": "2"}):
            info = db_pool.audit("sqlite:////tmp/eve.db")
        self.assertEqual(info["expected_max_connections"], 20)
        self.assertIsNone(info["warning"])


class PoolRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_pool_summary_reports_the_live_pool(self):
        summary = db_pool.pool_summary(db.engine)
        self.assertIn("pool_class", summary)
        self.assertIsNotNone(summary["size"])
        self.assertIsNotNone(summary["checkedin"])
        self.assertIsNotNone(summary["checkedout"])

    def test_connections_are_reused_between_requests(self):
        connects = []

        def _on_connect(_dbapi_connection, _record):
            connects.append(1)

        event.listen(db.engine, "connect", _on_connect)
        try:
            db.session.execute(text("SELECT 1"))
            db.session.remove()
            for _ in range(25):
                db.session.execute(text("SELECT 1"))
                db.session.remove()
        finally:
            try:
                event.remove(db.engine, "connect", _on_connect)
            except Exception:
                pass
        # The pooled connection is reused instead of reconnecting per checkout.
        self.assertEqual(connects, [])

    def test_pool_exhaustion_maps_to_503_with_retry_after(self):
        with app.test_request_context('/api/anything', headers={'Accept': 'application/json'}):
            result = app.handle_user_exception(SQLAlchemyTimeoutError('pool exhausted'))
        response, status = (result if isinstance(result, tuple) else (result, result.status_code))
        self.assertEqual(status, 503)
        self.assertEqual(response.headers.get('Retry-After'), '2')
        self.assertFalse(response.get_json()['success'])


class DbPoolScriptTests(unittest.TestCase):
    def test_quick_script_writes_a_valid_result(self):
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "benchmark_db_pool.py")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "pool.json")
            result = subprocess.run(
                [sys.executable, script, "--quick", "--json", out],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with open(out, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertEqual(payload["pooled_connects"], 1)
        self.assertEqual(payload["unpooled_connects"], payload["iterations"])
        self.assertGreaterEqual(payload["pool_size"], 1)
        self.assertIn("worker_demand", payload)


if __name__ == "__main__":
    unittest.main()
