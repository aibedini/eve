"""Phase 29 tests: the tamper-evident audit trail."""
import os
import tempfile
import unittest

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from flask import g  # noqa: E402

from app import Admin, app, db, _log_audit  # noqa: E402
from panel.models import AuditLog  # noqa: E402
from panel.services import audit  # noqa: E402


class AuditChainTests(unittest.TestCase):
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

    def setUp(self):
        AuditLog.query.delete()
        db.session.commit()
        self.admin = Admin(username="audit-admin-%d" % id(self), role="admin",
                           enabled=True)
        self.admin.set_password("CorrectHorseBattery1!")
        db.session.add(self.admin)
        db.session.commit()
        self.admin_id = self.admin.id

    def _write(self, action, **kwargs):
        audit.record(action, actor=self.admin, **kwargs)
        db.session.commit()

    def test_rows_are_chained_from_genesis(self):
        self._write("test.one", meta={"a": 1})
        self._write("test.two")
        rows = AuditLog.query.order_by(AuditLog.id.asc()).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].prev_hash, audit.GENESIS_HASH)
        self.assertEqual(rows[1].prev_hash, rows[0].entry_hash)
        self.assertEqual(len(rows[0].entry_hash), 64)
        result = audit.verify_chain()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["tip"], rows[1].entry_hash)

    def test_editing_a_row_breaks_the_chain(self):
        self._write("test.one", meta={"a": 1})
        self._write("test.two")
        row = AuditLog.query.order_by(AuditLog.id.asc()).all()[1]
        row.meta_json = '{"tampered": true}'
        db.session.commit()
        result = audit.verify_chain()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "content_mismatch")
        self.assertEqual(result["broken_at"], row.id)

    def test_deleting_a_row_breaks_the_chain(self):
        self._write("test.one")
        self._write("test.two")
        self._write("test.three")
        middle = AuditLog.query.order_by(AuditLog.id.asc()).all()[1]
        AuditLog.query.filter_by(id=middle.id).delete()
        db.session.commit()
        result = audit.verify_chain()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "chain_link")

    def test_legacy_rows_are_counted_and_skipped(self):
        db.session.add(AuditLog(actor_type="system", action="legacy.row",
                                meta_json=None))
        db.session.commit()
        self._write("test.after_legacy")
        result = audit.verify_chain()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["legacy"], 1)
        self.assertEqual(result["checked"], 1)

    def test_request_context_is_captured(self):
        with app.test_request_context("/", headers={"User-Agent": "eve-tests/1.0",
                                                   "X-Request-ID": "rid-42"}):
            g.request_id = "rid-42"
            audit.record("test.request", actor=self.admin)
            db.session.commit()
        row = AuditLog.query.filter_by(action="test.request").one()
        self.assertEqual(row.request_id, "rid-42")
        self.assertEqual(row.user_agent, "eve-tests/1.0")
        self.assertIsInstance(row.source_ip, str)
        self.assertTrue(audit.verify_chain()["ok"])

    def test_the_log_audit_helper_still_writes_chained_rows(self):
        _log_audit("test.helper", ("Thing", 7), actor=self.admin, meta={"n": 7})
        db.session.commit()
        row = AuditLog.query.filter_by(action="test.helper").one()
        self.assertEqual(row.actor_admin_id, self.admin_id)
        self.assertEqual(row.target_type, "Thing")
        self.assertEqual(row.target_id, "7")
        self.assertTrue(row.entry_hash)
        self.assertTrue(audit.verify_chain()["ok"])


class AuditLoginIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.admin = Admin(username="audit-login", role="admin", enabled=True)
        cls.admin.set_password("CorrectHorseBattery1!")
        db.session.add(cls.admin)
        db.session.commit()
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_a_failed_login_is_audited_with_the_request_id(self):
        AuditLog.query.delete()
        db.session.commit()
        response = self.client.post("/login", data={"username": "audit-login",
                                                    "password": "wrong"})
        request_id = response.headers.get("X-Request-ID")
        self.assertTrue(request_id, response.status_code)
        row = AuditLog.query.filter_by(action="auth.login.failed").first()
        self.assertIsNotNone(row, "failed login was not audited")
        self.assertEqual(row.request_id, request_id)
        self.assertTrue(row.source_ip, "the client address was not captured")
        self.assertTrue(audit.verify_chain()["ok"])


class AuditApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.admin = Admin(username="audit-api", role="admin", enabled=True)
        cls.admin.set_password("CorrectHorseBattery1!")
        cls.reseller = Admin(username="audit-reseller", role="reseller", enabled=True,
                             allowed_servers="[]")
        cls.reseller.set_password("CorrectHorseBattery1!")
        db.session.add_all([cls.admin, cls.reseller])
        db.session.commit()
        cls.admin_id = cls.admin.id
        cls.reseller_id = cls.reseller.id
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        AuditLog.query.delete()
        db.session.commit()
        # Re-fetch the actor in this session: the class-level instance can be
        # detached by an earlier request teardown.
        actor = db.session.get(Admin, self.admin_id)
        for index in range(5):
            audit.record("api.entry.%d" % index, actor=actor)
        db.session.commit()

    def _login(self, admin_id, role):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = admin_id
            sess["role"] = role
            sess["is_superadmin"] = False

    def test_the_trail_is_paginated_newest_first(self):
        self._login(self.admin_id, "admin")
        response = self.client.get("/api/audit-log?limit=2")
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_json()
        self.assertEqual(len(body["entries"]), 2)
        self.assertEqual(body["total"], 5)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["entries"][0]["action"], "api.entry.4")
        self.assertGreater(body["entries"][0]["id"], body["entries"][1]["id"])
        self.assertTrue(body["entries"][0]["entry_hash"])

    def test_filters_and_invalid_timestamps(self):
        self._login(self.admin_id, "admin")
        filtered = self.client.get("/api/audit-log?action=api.entry.0").get_json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["entries"][0]["action"], "api.entry.0")
        by_actor = self.client.get(
            "/api/audit-log?actor_admin_id=%d" % self.admin_id).get_json()
        self.assertEqual(by_actor["total"], 5)
        bad = self.client.get("/api/audit-log?since=not-a-date")
        # API business errors are served as HTTP 200 for the CDN with the real
        # status in X-Eve-Status, so the body is the contract.
        self.assertEqual(bad.get_json()["success"], False)
        self.assertIn("Invalid since timestamp", bad.get_json()["error"])

    def test_a_reseller_cannot_read_the_trail(self):
        self._login(self.reseller_id, "reseller")
        self.assertEqual(self.client.get("/api/audit-log").status_code, 403)

    def test_doctor_reports_the_chain(self):
        self._login(self.admin_id, "admin")
        response = self.client.get("/api/doctor")
        self.assertEqual(response.status_code, 200, response.data)
        check = response.get_json()["checks"]["audit_chain"]
        self.assertEqual(check["state"], "ok")
        self.assertTrue(check["ok"])
        self.assertGreaterEqual(check["checked"], 5)


if __name__ == "__main__":
    unittest.main()
