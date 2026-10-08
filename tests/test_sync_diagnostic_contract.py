"""Regression contract for authoritative scheduler outcomes and safe reports."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
POLICY = (ROOT / "panel" / "core" / "refresh_policy.py").read_text(encoding="utf-8")
SCHEDULER = (ROOT / "panel" / "jobs" / "schedulers.py").read_text(encoding="utf-8")


class SyncDiagnosticContractTests(unittest.TestCase):
    def test_capacity_and_superseded_are_not_fetch_successes(self):
        self.assertIn("outcome='CAPACITY_DEFERRED'", SCHEDULER)
        self.assertIn("outcome='SUPERSEDED'", SCHEDULER)
        busy = SCHEDULER.split("if result.get('busy'):", 1)[1].split(
            "# Ordering barrier", 1)[0]
        self.assertNotIn("note_server_result(sid, True", busy)

    def test_shared_report_contains_bounded_diagnostic_fields(self):
        for field in (
            "error_code", "error_category", "last_error_summary",
            "last_failure_at", "last_success_at", "last_fetch_duration_ms",
            "consecutive_failures", "next_retry_at", "retry_in_seconds",
            "currently_fetching", "queue_delay_ms", "snapshot_revision",
            "server_revision",
        ):
            self.assertIn(f"'{field}'", POLICY)
        self.assertIn("[:200]", POLICY)

    def test_stale_report_suppresses_countdowns(self):
        self.assertIn("None if stale_report", POLICY)
        self.assertIn("'report_stale': bool(stale_report)", POLICY)


if __name__ == "__main__":
    unittest.main()
