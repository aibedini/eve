"""The ONE candidate gate ladder, and the preview that must not lie.

The defect these tests pin: the preview answered
``matched == eligible == len(candidates)`` with ``deferred = suppressed = 0``.
That is not an evaluation - it promised every matched candidate was eligible
while the real run would defer most of them - and it is the same shape as
``matched_count == eligible_count`` in the run manifest.

The claims that matter:
  * the ladder decides by ORDER, and the order is the policy;
  * a run-level block makes every candidate non-eligible and says why;
  * `summarize` can and does report eligible < matched;
  * the real run and the preview call the SAME function (a second copy would
    drift, which is worse than the stub it replaced).
"""
import base64
import os
import tempfile
import unittest

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

from panel.services import sms_candidate_evaluator as ev  # noqa: E402


class LadderTests(unittest.TestCase):
    def test_a_clean_candidate_is_eligible_and_sendable(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts())
        self.assertEqual(verdict.disposition, ev.ELIGIBLE_NOW)
        self.assertIsNone(verdict.reason_code)
        self.assertTrue(verdict.sendable)
        self.assertIsNone(verdict.stop_reason)

    def test_the_invariant_sendable_iff_eligible(self):
        """No gate may produce a sendable non-eligible candidate."""
        cases = [
            ev.CandidateFacts(),
            ev.CandidateFacts(has_recipient=False),
            ev.CandidateFacts(opted_out=True),
            ev.CandidateFacts(manual_review=True),
            ev.CandidateFacts(obligation_outstanding=True),
            ev.CandidateFacts(cooldown_seconds_remaining=60),
            ev.CandidateFacts(template_present=False),
            ev.CandidateFacts(message_empty=True),
            ev.CandidateFacts(budget_available=False, budget_stop_reason='rate_limited'),
        ]
        for facts in cases:
            verdict = ev.evaluate_candidate(facts)
            self.assertEqual(verdict.sendable, verdict.disposition == ev.ELIGIBLE_NOW)

    # ── the order IS the policy ───────────────────────────────────────────────

    def test_a_missing_recipient_beats_every_other_gate(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts(
            has_recipient=False, opted_out=True, cooldown_seconds_remaining=99))
        self.assertEqual(verdict.disposition, ev.INVALID_RECIPIENT)
        self.assertEqual(verdict.reason_code, 'no_recipient')

    def test_opt_out_beats_an_outstanding_obligation(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts(
            opted_out=True, obligation_outstanding=True))
        self.assertEqual((verdict.disposition, verdict.reason_code),
                         (ev.SUPPRESSED, 'opted_out_recheck'))

    def test_manual_review_beats_an_outstanding_obligation(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts(
            manual_review=True, obligation_outstanding=True))
        self.assertEqual(verdict.reason_code, 'manual_review_pending')

    def test_an_outstanding_obligation_beats_a_cooldown(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts(
            obligation_outstanding=True, cooldown_seconds_remaining=600))
        self.assertEqual(verdict.disposition, ev.ACTIVE_OBLIGATION)
        self.assertEqual(verdict.reason_code, 'obligation_outstanding')
        self.assertFalse(verdict.sendable)

    def test_a_cooldown_beats_a_missing_template(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts(
            cooldown_seconds_remaining=600, template_present=False))
        self.assertEqual((verdict.disposition, verdict.reason_code),
                         (ev.DEFERRED, 'cooldown_active'))

    def test_a_missing_template_beats_an_empty_message(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts(
            template_present=False, message_empty=True))
        self.assertEqual(verdict.reason_code, 'no_template')

    def test_an_empty_message_beats_the_budget(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts(
            message_empty=True, budget_available=False, budget_stop_reason='rate_limited'))
        self.assertEqual((verdict.disposition, verdict.reason_code),
                         (ev.SUPPRESSED, 'empty_message'))

    # ── dispositions and reasons ──────────────────────────────────────────────

    def test_each_suppression_names_itself(self):
        for facts, reason in ((ev.CandidateFacts(opted_out=True), 'opted_out_recheck'),
                              (ev.CandidateFacts(manual_review=True), 'manual_review_pending'),
                              (ev.CandidateFacts(template_present=False), 'no_template'),
                              (ev.CandidateFacts(message_empty=True), 'empty_message')):
            verdict = ev.evaluate_candidate(facts)
            self.assertEqual(verdict.disposition, ev.SUPPRESSED)
            self.assertEqual(verdict.reason_code, reason)

    def test_a_cooldown_is_a_deferral_not_a_suppression(self):
        """A deferral is owed later; a suppression is not. Conflating them is how
        a reminder silently stops being owed."""
        verdict = ev.evaluate_candidate(ev.CandidateFacts(cooldown_seconds_remaining=1))
        self.assertEqual(verdict.disposition, ev.DEFERRED)

    def test_an_exhausted_budget_stops_the_run(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts(
            budget_available=False, budget_stop_reason='daily_limit_reached'))
        self.assertEqual(verdict.disposition, ev.DEFERRED)
        self.assertEqual(verdict.stop_reason, 'daily_limit_reached')

    def test_a_per_send_refusal_does_not_stop_the_run(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts(
            budget_available=False, budget_stop_reason='recipient_cooldown'))
        self.assertEqual(verdict.disposition, ev.DEFERRED)
        self.assertIsNone(verdict.stop_reason)
        self.assertEqual(verdict.reason_code, 'recipient_cooldown')

    def test_a_budget_refusal_without_a_reason_still_defers(self):
        verdict = ev.evaluate_candidate(ev.CandidateFacts(budget_available=False))
        self.assertEqual(verdict.reason_code, 'rate_limited')
        self.assertIsNone(verdict.stop_reason)


class RunGateTests(unittest.TestCase):
    def test_a_healthy_run_is_ready(self):
        self.assertEqual(ev.evaluate_run(sms_enabled=True, gateway_ready=True,
                                         quiet_hours=False), ev.RUN_READY)

    def test_disabled_beats_every_other_run_gate(self):
        self.assertEqual(ev.evaluate_run(sms_enabled=False, gateway_ready=False,
                                         quiet_hours=True), ev.RUN_SMS_DISABLED)

    def test_gateway_readiness_beats_quiet_hours(self):
        self.assertEqual(ev.evaluate_run(sms_enabled=True, gateway_ready=False,
                                         quiet_hours=True), ev.RUN_GATEWAY_NOT_READY)

    def test_quiet_hours_is_the_last_run_gate(self):
        self.assertEqual(ev.evaluate_run(sms_enabled=True, gateway_ready=True,
                                         quiet_hours=True), ev.RUN_QUIET_HOURS)


class SummaryTests(unittest.TestCase):
    def test_eligible_is_never_silently_equal_to_matched(self):
        """The whole point: most candidates deferred, eligible counted honestly."""
        evaluations = [ev.evaluate_candidate(ev.CandidateFacts())] + [
            ev.evaluate_candidate(ev.CandidateFacts(cooldown_seconds_remaining=600))
            for _ in range(4)]
        summary = ev.summarize(evaluations)
        self.assertEqual(summary['matched'], 5)
        self.assertEqual(summary[ev.ELIGIBLE_NOW], 1)
        self.assertEqual(summary[ev.DEFERRED], 4)
        self.assertNotEqual(summary['eligible'], summary['matched'])
        self.assertEqual(summary['eligible'], summary[ev.ELIGIBLE_NOW])

    def test_every_disposition_is_counted(self):
        evaluations = [
            ev.evaluate_candidate(ev.CandidateFacts()),
            ev.evaluate_candidate(ev.CandidateFacts(cooldown_seconds_remaining=60)),
            ev.evaluate_candidate(ev.CandidateFacts(opted_out=True)),
            ev.evaluate_candidate(ev.CandidateFacts(has_recipient=False)),
            ev.evaluate_candidate(ev.CandidateFacts(obligation_outstanding=True)),
        ]
        summary = ev.summarize(evaluations)
        self.assertEqual(summary['matched'], 5)
        for disposition in ev.DISPOSITIONS:
            self.assertEqual(summary[disposition], 1, disposition)
        self.assertEqual(sum(summary[d] for d in ev.DISPOSITIONS), summary['matched'])

    def test_the_reason_breakdown_adds_up(self):
        evaluations = [
            ev.evaluate_candidate(ev.CandidateFacts(opted_out=True)),
            ev.evaluate_candidate(ev.CandidateFacts(opted_out=True)),
            ev.evaluate_candidate(ev.CandidateFacts(cooldown_seconds_remaining=60)),
            ev.evaluate_candidate(ev.CandidateFacts()),
        ]
        summary = ev.summarize(evaluations)
        self.assertEqual(summary['reasons']['opted_out_recheck'], 2)
        self.assertEqual(summary['reasons']['cooldown_active'], 1)
        # An eligible candidate contributes no reason.
        self.assertNotIn(None, summary['reasons'])

    def test_a_run_level_block_makes_every_candidate_non_eligible(self):
        evaluations = [ev.evaluate_candidate(ev.CandidateFacts()) for _ in range(3)]
        summary = ev.summarize(evaluations, run_state=ev.RUN_QUIET_HOURS)
        self.assertEqual(summary['matched'], 3)
        self.assertEqual(summary[ev.ELIGIBLE_NOW], 0)
        self.assertEqual(summary[ev.DEFERRED], 3)
        self.assertEqual(summary['reasons'], {'quiet_hours': 3})
        self.assertEqual(summary['run_state'], ev.RUN_QUIET_HOURS)

    def test_an_empty_audience_summarizes_to_zeros(self):
        summary = ev.summarize([])
        self.assertEqual(summary['matched'], 0)
        self.assertEqual(summary['eligible'], 0)
        self.assertEqual(summary['reasons'], {})


class SingleEvaluatorTests(unittest.TestCase):
    """A preview that runs its OWN copy of the ladder would drift, and a drifted
    preview is worse than the stub it replaced because it lies with confidence."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.root = Path(__file__).resolve().parents[1]

    def _jobs_source(self):
        return (self.root / "panel" / "jobs" / "messaging.py").read_text(encoding="utf-8")

    def test_the_run_and_the_preview_call_the_same_evaluator(self):
        source = self._jobs_source()
        self.assertGreaterEqual(source.count("candidate_evaluator.evaluate_candidate("), 2)
        self.assertIn("def _facts_for(", source)
        # One fact resolution shared by both callers.
        self.assertEqual(source.count("_facts_for("), 3)

    def test_the_preview_no_longer_equates_matched_with_eligible(self):
        source = self._jobs_source()
        self.assertNotIn("'eligible': len(candidates)", source)
        self.assertNotIn("'matched': len(candidates)", source)
        self.assertIn("budget_checked", source)

    def test_the_budget_is_not_resolved_as_a_fact(self):
        """Checking the budget consumes it, so only the run may do it."""
        source = self._jobs_source()
        facts_block = source.split("def _facts_for(", 1)[1].split("    if preview:", 1)[0]
        self.assertNotIn("_sms_take_send_slot", facts_block)

    def test_the_preview_route_is_implemented(self):
        source = (self.root / "panel" / "routes" / "messaging.py").read_text(encoding="utf-8")
        self.assertIn("preview=True", source)
        self.assertIn("_run_sms_depletion_scan(", source)
        self.assertIn("no-store", source)

    def test_the_ladder_has_no_second_definition(self):
        source = self._jobs_source()
        # The outcome literals must not be re-derived in the caller.
        self.assertNotIn("'cooldown_active', False", source)
        self.assertNotIn("return 'suppressed'", source)


if __name__ == '__main__':
    unittest.main()
