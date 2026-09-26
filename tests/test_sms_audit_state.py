"""The candidate-audit diagnostic: counts, and the legacy-history verdict.

The distinction these tests pin is the one the delivery panel could not make:
"we have send history but no candidate manifest" is NOT the same claim as "there
were no candidates", and only the first one may be printed when the audit tables
say so.
"""
import base64
import os
import tempfile
import unittest
from datetime import datetime

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL',
                      'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault(
    'SERVER_PASSWORD_KEY',
    base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from panel.extensions import db  # noqa: E402
from panel.models import (  # noqa: E402
    ServiceNotificationEvent,
    SmsScanDecision,
    SmsScanRun,
    SmsSendLog,
)
from panel.services import lifecycle, sms_audit_state  # noqa: E402

SERVER_ID = 7
EMAIL = 'h34-09195758193@example.com'


class CandidateAuditStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        SmsScanDecision.query.delete()
        SmsScanRun.query.delete()
        SmsSendLog.query.delete()
        ServiceNotificationEvent.query.delete()
        db.session.commit()
        self.key = lifecycle.make_service_key(SERVER_ID, 'uuid-audit')

    # ── fixtures ──────────────────────────────────────────────────────────────

    def _send_log(self, status='sent'):
        row = SmsSendLog(email=EMAIL, server_id=SERVER_ID, state='ended',
                         recipient='0919***8193', status=status,
                         created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.session.add(row)
        db.session.commit()
        return row

    def _decision(self, *, service_key=None, disposition='deferred',
                  send_log_id=None, run_id='evt-1'):
        row = SmsScanDecision(
            run_id=run_id, service_key=service_key or self.key, server_id=SERVER_ID,
            client_email=EMAIL, state='ended', disposition=disposition,
            reason_code='evaluation_pending', decision_at=datetime.utcnow(),
            sms_send_log_id=send_log_id)
        db.session.add(row)
        db.session.commit()
        return row

    def _run(self, *, eligible_count=0, matched_count=5, audit_gap_count=3,
             status='completed'):
        run = SmsScanRun(
            run_id='run-1', triggered_by='manual', status=status,
            started_at=datetime.utcnow(), finished_at=datetime.utcnow(),
            matched_count=matched_count, eligible_count=eligible_count,
            audit_gap_count=audit_gap_count,
            created_at=datetime.utcnow(), updated_at=datetime.utcnow())
        db.session.add(run)
        db.session.commit()
        return run

    # ── the verdict ───────────────────────────────────────────────────────────

    def test_an_empty_workspace_reports_no_manifest_and_no_note(self):
        snapshot = sms_audit_state.candidate_audit_snapshot()
        self.assertEqual(snapshot['counts']['send_logs'], 0)
        self.assertEqual(snapshot['counts']['scan_decisions'], 0)
        self.assertFalse(snapshot['candidate_manifest_present'])
        self.assertFalse(snapshot['legacy_send_history_without_manifest'])
        self.assertIsNone(snapshot['note'])

    def test_send_history_without_decisions_is_legacy_not_zero_candidates(self):
        for _ in range(3):
            self._send_log()
        snapshot = sms_audit_state.candidate_audit_snapshot()
        self.assertEqual(snapshot['counts']['send_logs'], 3)
        self.assertEqual(snapshot['counts']['scan_decisions'], 0)
        self.assertFalse(snapshot['candidate_manifest_present'])
        self.assertTrue(snapshot['legacy_send_history_without_manifest'])
        self.assertEqual(snapshot['note'], sms_audit_state.LEGACY_NOTE)
        self.assertIn('do not prove there were no candidates', snapshot['note'])

    def test_a_manifest_clears_the_legacy_verdict(self):
        self._send_log()
        self._decision()
        snapshot = sms_audit_state.candidate_audit_snapshot()
        self.assertTrue(snapshot['candidate_manifest_present'])
        self.assertFalse(snapshot['legacy_send_history_without_manifest'])
        self.assertIsNone(snapshot['note'])

    # ── the two counts that prove the decision-first wiring ───────────────────

    def test_decisions_without_a_send_log_are_counted(self):
        self._decision()
        self._decision(service_key=lifecycle.make_service_key(SERVER_ID, 'uuid-b'))
        snapshot = sms_audit_state.candidate_audit_snapshot()
        self.assertEqual(snapshot['decisions_without_send_log'], 2)
        self.assertEqual(snapshot['dispositions'], {'deferred': 2})

    def test_send_logs_without_a_decision_are_counted(self):
        """The production defect: the pipeline that sends recorded no decisions."""
        row = self._send_log()
        self._send_log()
        self._decision(send_log_id=row.id, disposition='submitted')
        snapshot = sms_audit_state.candidate_audit_snapshot()
        self.assertEqual(snapshot['counts']['send_logs'], 2)
        self.assertEqual(snapshot['send_logs_without_decision'], 1)
        self.assertEqual(snapshot['decisions_without_send_log'], 0)

    # ── the latest run ────────────────────────────────────────────────────────

    def test_the_latest_run_reports_its_counts(self):
        self._run(matched_count=9, eligible_count=4, audit_gap_count=2)
        latest = sms_audit_state.candidate_audit_snapshot()['latest_run']
        self.assertEqual(latest['run_id'], 'run-1')
        self.assertEqual(latest['status'], 'completed')
        self.assertEqual(latest['matched_count'], 9)
        self.assertEqual(latest['eligible_count'], 4)
        self.assertEqual(latest['audit_gap_count'], 2)
        self.assertIsNotNone(latest['started_at'])

    def test_eligible_count_is_reported_as_unmeasured_when_never_written(self):
        """eligible_count is NOT NULL DEFAULT 0 and no code path writes it, so a
        finished run reporting 0 means 'not measured', never 'none eligible'."""
        self._run(eligible_count=0)
        snapshot = sms_audit_state.candidate_audit_snapshot()
        self.assertFalse(snapshot['eligible_count_is_measured'])

    def test_a_measured_eligible_count_is_reported_as_measured(self):
        self._run(eligible_count=7)
        self.assertTrue(
            sms_audit_state.candidate_audit_snapshot()['eligible_count_is_measured'])

    def test_no_run_reports_a_null_latest_run(self):
        snapshot = sms_audit_state.candidate_audit_snapshot()
        self.assertIsNone(snapshot['latest_run'])
        self.assertIsNone(snapshot['eligible_count_is_measured'])

    # ── it is a diagnostic, not a mutation ────────────────────────────────────

    def test_the_snapshot_writes_nothing(self):
        self._send_log()
        self._decision()
        self._run()
        before = (SmsSendLog.query.count(), SmsScanDecision.query.count(),
                  SmsScanRun.query.count(), ServiceNotificationEvent.query.count())
        for _ in range(3):
            sms_audit_state.candidate_audit_snapshot()
        after = (SmsSendLog.query.count(), SmsScanDecision.query.count(),
                 SmsScanRun.query.count(), ServiceNotificationEvent.query.count())
        self.assertEqual(before, after)

    def test_the_snapshot_is_json_serializable(self):
        import json
        self._send_log()
        self._decision()
        self._run()
        self.assertIsInstance(
            json.dumps(sms_audit_state.candidate_audit_snapshot(), default=str), str)


class CandidateAuditWiringTests(unittest.TestCase):
    """A diagnostic nobody renders is not a diagnostic."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.root = Path(__file__).resolve().parents[1]

    def test_the_doctor_exposes_the_candidate_audit_probe(self):
        source = (self.root / "panel" / "routes" / "doctor.py").read_text(encoding="utf-8")
        self.assertIn("checks['candidate_audit']", source)
        self.assertIn("candidate_audit_snapshot()", source)
        self.assertIn("legacy_send_history_without_manifest", source)

    def test_the_forensic_report_includes_the_candidate_audit(self):
        source = (self.root / "scripts" / "forensic_sms_delivery.py").read_text(
            encoding="utf-8")
        self.assertIn("report['candidate_audit'] = candidate_audit_snapshot()", source)


if __name__ == '__main__':
    unittest.main()
