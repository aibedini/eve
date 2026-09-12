"""Phase 30 tests: bounded, resumable data retention."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import Admin, app, db  # noqa: E402
from panel.models import (  # noqa: E402
    AdminSession, BnqoAgent, BnqoJob, HealthLog, SystemMigration, SystemSetting,
)
from panel.services import retention  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RetentionTests(unittest.TestCase):
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
        for model in (HealthLog, BnqoJob, AdminSession, SystemMigration, SystemSetting):
            model.query.delete()
        db.session.commit()
        self.now = datetime.utcnow()

    def _health_rows(self, old, recent):
        for index in range(old):
            db.session.add(HealthLog(timestamp=self.now - timedelta(days=200),
                                     level="info", category="test",
                                     message="old %d" % index))
        for index in range(recent):
            db.session.add(HealthLog(timestamp=self.now - timedelta(days=1),
                                     level="info", category="test",
                                     message="new %d" % index))
        db.session.commit()

    def test_preview_counts_without_deleting(self):
        self._health_rows(old=4, recent=3)
        before = HealthLog.query.count()
        result = retention.preview("health_logs")
        self.assertTrue(result["enabled"])
        self.assertEqual(result["policies"]["health_logs"]["eligible"], 4)
        self.assertEqual(HealthLog.query.count(), before)

    def test_old_rows_are_deleted_in_batches_and_resume(self):
        self._health_rows(old=5, recent=3)
        first = retention.run("health_logs", batch_size=2, max_batches=1)
        self.assertEqual(first["policies"]["health_logs"]["deleted"], 2)
        self.assertEqual(HealthLog.query.filter(
            HealthLog.timestamp < self.now - timedelta(days=90)).count(), 3)
        second = retention.run("health_logs", batch_size=2, max_batches=1)
        self.assertEqual(second["policies"]["health_logs"]["deleted"], 2)
        third = retention.run("health_logs", batch_size=2, max_batches=5)
        self.assertEqual(third["policies"]["health_logs"]["deleted"], 1)
        self.assertEqual(HealthLog.query.count(), 3)
        self.assertEqual(
            HealthLog.query.filter_by(category="test").count(), 3)

    def test_recent_rows_are_never_deleted(self):
        self._health_rows(old=0, recent=3)
        retention.run("health_logs", batch_size=10, max_batches=5)
        self.assertEqual(HealthLog.query.count(), 3)

    def test_the_ledger_records_progress_and_finishes(self):
        self._health_rows(old=2, recent=0)
        retention.run("health_logs", batch_size=10, max_batches=5)
        record = SystemMigration.query.filter_by(
            migration_id="retention:health_logs").one()
        self.assertEqual(record.processed_rows, 2)
        self.assertEqual(record.status, "done")
        self.assertIsNotNone(record.finished_at)
        self.assertIn("last_id", json.loads(record.cursor_json))
        again = retention.run("health_logs", batch_size=10, max_batches=5)
        self.assertEqual(again["policies"]["health_logs"]["deleted"], 0)

    def test_a_zero_window_disables_the_policy(self):
        self._health_rows(old=3, recent=0)
        db.session.add(SystemSetting(key="retention_days_health_logs", value="0"))
        db.session.commit()
        preview = retention.preview("health_logs")
        self.assertTrue(preview["policies"]["health_logs"]["disabled"])
        result = retention.run("health_logs", batch_size=10, max_batches=5)
        self.assertEqual(result["policies"]["health_logs"]["deleted"], 0)
        self.assertEqual(HealthLog.query.count(), 3)

    def test_retention_can_be_disabled_globally(self):
        self._health_rows(old=3, recent=0)
        db.session.add(SystemSetting(key="retention_enabled", value="false"))
        db.session.commit()
        result = retention.run("health_logs", batch_size=10, max_batches=5)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["reason"], "retention_disabled")
        self.assertEqual(HealthLog.query.count(), 3)

    def test_expired_sessions_are_pruned_but_live_ones_are_kept(self):
        admin = Admin(username="retention-admin", role="admin", enabled=True)
        admin.set_password("CorrectHorseBattery1!")
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id
        db.session.add_all([
            AdminSession(admin_id=admin_id, token_hash="a" * 64,
                         expires_at=self.now - timedelta(days=60)),
            AdminSession(admin_id=admin_id, token_hash="b" * 64,
                         expires_at=self.now - timedelta(days=1)),
            AdminSession(admin_id=admin_id, token_hash="c" * 64,
                         expires_at=self.now + timedelta(days=7)),
        ])
        db.session.commit()
        retention.run("admin_sessions", batch_size=10, max_batches=5)
        remaining = {row.token_hash for row in AdminSession.query.all()}
        self.assertNotIn("a" * 64, remaining)
        self.assertIn("b" * 64, remaining)
        self.assertIn("c" * 64, remaining)

    def test_pending_jobs_are_kept_and_delivered_ones_expire(self):
        agent = BnqoAgent(name="retention-agent", role="iran", token="t" * 32,
                          pubkey="p" * 32)
        db.session.add(agent)
        db.session.commit()
        agent_id = agent.id
        db.session.add_all([
            BnqoJob(job_id="job-pending", agent_id=agent_id, type="probe",
                    expires_at=self.now - timedelta(days=90), status="pending",
                    created_at=self.now - timedelta(days=90)),
            BnqoJob(job_id="job-acked", agent_id=agent_id, type="probe",
                    expires_at=self.now - timedelta(days=90), status="acked",
                    created_at=self.now - timedelta(days=90)),
        ])
        db.session.commit()
        retention.run("bnqo_jobs", batch_size=10, max_batches=5)
        remaining = {row.job_id for row in BnqoJob.query.all()}
        self.assertIn("job-pending", remaining)
        self.assertNotIn("job-acked", remaining)

    def test_status_reports_every_policy(self):
        summary = retention.status()
        self.assertIn("enabled", summary)
        self.assertEqual(sorted(summary["policies"]),
                         sorted(policy.name for policy in retention.policies()))
        for entry in summary["policies"].values():
            self.assertIn("days", entry)
            self.assertIn("default_days", entry)

    def test_the_audit_trail_is_not_a_retention_policy(self):
        self.assertNotIn("audit_logs", [policy.name for policy in retention.policies()])


class RetentionDoctorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.admin = Admin(username="retention-doctor", role="admin", enabled=True)
        cls.admin.set_password("CorrectHorseBattery1!")
        db.session.add(cls.admin)
        db.session.commit()
        cls.admin_id = cls.admin.id
        cls.client = app.test_client()
        with cls.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = cls.admin_id
            sess["role"] = "admin"
            sess["is_superadmin"] = False

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_doctor_reports_the_retention_policies(self):
        response = self.client.get("/api/doctor")
        self.assertEqual(response.status_code, 200, response.data)
        check = response.get_json()["checks"]["retention"]
        self.assertEqual(check["state"], "ok")
        self.assertIn("health_logs", check["policies"])


class RetentionCliTests(unittest.TestCase):
    def test_dry_run_cli_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["DATABASE_URL"] = "sqlite:///" + os.path.join(tmp, "cli.db").replace(os.sep, "/")
            env["FLASK_ENV"] = "development"
            env["SESSION_SECRET"] = "retention-cli-secret"
            env["DISABLE_BACKGROUND_THREADS"] = "1"
            result = subprocess.run(
                [sys.executable, "-m", "panel.services.retention", "--dry-run", "--json"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # App log lines share stdout, so read the last JSON line the CLI wrote.
            payload = None
            for line in reversed(result.stdout.strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    payload = json.loads(line)
                    break
            self.assertIsNotNone(payload, result.stdout[-500:])
            self.assertIn("policies", payload)


if __name__ == "__main__":
    unittest.main()
