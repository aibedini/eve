"""Renewal verdicts require measured evidence, not an optimistic write flag."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
from unittest.mock import Mock

from panel.services import renew_activation as ra

GB = 1024 ** 3
EXPECTED = {'expiryTime': 2_000_000_000_000, 'totalGB': 20 * GB,
            'enable': True, 'inbound_id': 1600}


def measure(*, expected=None, expiry=None, quota=None, enabled=True,
            membership=True, traffic=None, pending=None, before=None, mutation=None,
            read=None, partial=False):
    expected = expected or EXPECTED
    client = {'expiryTime': expected['expiryTime'] if expiry is None else expiry,
              'totalGB': expected['totalGB'] if quota is None else quota, 'enable': enabled}
    layers = ra.analyze_activation(expected=expected, global_client=client,
        inbound_ids=[1600], memberships={1600: {'enable': membership}},
        traffic=traffic, node_pending=pending, write_may_be_partial=partial)
    return ra.classify_layers(layers, expected=expected, before=before,
                             mutation=mutation, read=read or {'status': 'ok'})


class RenewalEvidenceTests(unittest.TestCase):
    def test_existing_quota_match_and_expiry_mismatch_do_not_prove_partial_write(self):
        layers = measure(expiry=EXPECTED['expiryTime'] - 86400000,
                         before={'totalGB': 20 * GB}, partial=True)
        self.assertEqual(layers.final_state, ra.STATE_UNKNOWN)
        self.assertIn('EXPIRY_MISMATCH', layers.reason_codes)
        quota = layers.evidence['fields']['totalGB']
        self.assertTrue(quota['value_matched'])
        self.assertFalse(quota['change_confirmed'])

    def test_expiry_match_and_quota_mismatch(self):
        layers = measure(quota=5 * GB)
        self.assertIn('QUOTA_MISMATCH', layers.reason_codes)
        self.assertNotIn('EXPIRY_MISMATCH', layers.reason_codes)
        self.assertEqual(layers.final_state, ra.STATE_UNKNOWN)

    def test_partial_change_is_measured_even_without_write_hint(self):
        layers = measure(expiry=1, before={'totalGB': 5 * GB, 'expiryTime': 1})
        self.assertEqual(layers.final_state, ra.STATE_PARTIALLY_APPLIED)
        self.assertTrue(layers.evidence['fields']['totalGB']['change_confirmed'])

    def test_rejected_and_unchanged_authoritative_read_can_prove_not_applied(self):
        layers = measure(expiry=1, before={'totalGB': 20 * GB, 'expiryTime': 1, 'enable': True},
                         mutation={'accepted': False, 'rejected': True})
        self.assertEqual(layers.final_state, ra.STATE_NOT_APPLIED)
        self.assertIn('MUTATION_REJECTED', layers.reason_codes)

    def test_same_before_and_after_with_uncertain_mutation_is_unknown(self):
        layers = measure(before=EXPECTED, mutation={'accepted': None})
        self.assertEqual(layers.final_state, ra.STATE_UNKNOWN)
        self.assertIn('MUTATION_OUTCOME_UNKNOWN', layers.reason_codes)

    def test_disabled_global_and_membership_are_independent(self):
        global_disabled = measure(enabled=False)
        member_disabled = measure(membership=False)
        self.assertIn('GLOBAL_DISABLED', global_disabled.reason_codes)
        self.assertNotIn('MEMBERSHIP_DISABLED', global_disabled.reason_codes)
        self.assertIn('MEMBERSHIP_DISABLED', member_disabled.reason_codes)
        self.assertEqual(member_disabled.disabled_inbound_ids, [1600])
        self.assertFalse(global_disabled.activation_converged)
        self.assertFalse(member_disabled.activation_converged)

    def test_legacy_intent_still_records_expected_global_activation(self):
        expected = {key: value for key, value in EXPECTED.items() if key != 'enable'}
        field = measure(expected=expected).evidence['fields']['enable']
        self.assertTrue(field['expected'])
        self.assertTrue(field['value_matched'])

    def test_missing_membership_and_unknown_enable_do_not_confirm_activation(self):
        missing = ra.analyze_activation(expected=EXPECTED,
            global_client=EXPECTED, inbound_ids=[1600], memberships={})
        self.assertIn('MEMBERSHIP_MISSING', missing.reason_codes)
        self.assertFalse(measure(enabled=None).activation_converged)
        self.assertFalse(measure(membership=None).activation_converged)

    def test_consumed_quota_is_not_repaired_by_enabling(self):
        layers = measure(traffic={'available': True, 'enable': True, 'up': 20 * GB, 'down': 0})
        self.assertIn('TRAFFIC_DEPLETED', layers.reason_codes)
        self.assertEqual(layers.evidence['observed']['remaining_quota'], 0)
        self.assertFalse(layers.activation_converged)
        self.assertTrue(ra.account_is_still_depleted(layers, EXPECTED)[0])

    def test_reset_failure_never_repeats_reset_or_activation(self):
        layers = measure(mutation={'accepted': True,
                                   'traffic_reset': {'requested': True, 'accepted': False}})
        repair = Mock()
        result, _ = ra.converge_activation(initial=layers, verify=Mock(), repair=repair)
        self.assertIn('TRAFFIC_RESET_FAILED', result.reason_codes)
        self.assertFalse(result.activation_converged)
        repair.assert_not_called()

    def test_unavailable_and_stale_panel_reads_are_unknown(self):
        for status in ('failed', 'stale'):
            layers = measure(read={'status': status})
            self.assertEqual(layers.final_state, ra.STATE_UNKNOWN)
            self.assertFalse(layers.config_applied)
        missing = ra.analyze_activation(expected=EXPECTED, global_client=None)
        self.assertEqual(missing.final_state, ra.STATE_UNKNOWN)
        self.assertIn('PANEL_READ_FAILED', missing.reason_codes)

    def test_exact_30_day_first_use_duration_is_valid_but_31_days_is_mismatch(self):
        expected = dict(EXPECTED, expiryTime=-30 * 86400000)
        self.assertTrue(measure(expected=expected).config_applied)
        mismatch = measure(expected=expected, expiry=-31 * 86400000)
        self.assertIn('EXPIRY_MISMATCH', mismatch.reason_codes)
        self.assertEqual(mismatch.evidence['expiry']['difference_days'], -1)
        self.assertEqual(mismatch.evidence['expected']['expiryTime'], -30 * 86400000)

    def test_timestamp_diagnostic_and_later_read_recover_same_intent(self):
        mismatch = measure(expiry=EXPECTED['expiryTime'] - 86400000)
        delta = mismatch.evidence['expiry']
        self.assertEqual((delta['difference_ms'], delta['difference_seconds'], delta['difference_days']),
                         (-86400000, -86400, -1))
        recovered = measure(mutation={'accepted': True})
        self.assertEqual(recovered.final_state, ra.STATE_APPLIED_ACTIVE)
        self.assertEqual(mismatch.evidence['expected'], recovered.evidence['expected'])


@unittest.skipUnless(shutil.which('node'), 'Node is needed for the actual modal helpers')
class RenewalEvidenceUiTests(unittest.TestCase):
    def run_ui(self, expression, calendar='gregorian'):
        template = (Path(__file__).resolve().parents[1] / 'templates/dashboard.html').read_text(encoding='utf-8')
        region = template.split('// renew-result-modal:start', 1)[1].split('// renew-result-modal:end', 1)[0]
        region = re.sub(r'const RENEW_LANG = .*?;', "const RENEW_LANG = 'en';", region)
        code = ('const EveDate = {format: d => ' + json.dumps(calendar + ':')
                + ' + d.toISOString()};\n' + region
                + '\n(async () => console.log(JSON.stringify(await (' + expression
                + '))))().catch(e => {console.error(e); process.exit(1);});')
        result = subprocess.run(['node'], input=code, capture_output=True,
                                encoding='utf-8', check=True)
        return json.loads(result.stdout)

    def test_uncertain_and_partial_are_amber_and_proven_failure_is_red(self):
        self.assertEqual(self.run_ui('[RENEW_STATE_TONE.UNKNOWN, RENEW_STATE_TONE.PARTIALLY_APPLIED, RENEW_STATE_TONE.NOT_APPLIED]'),
                         ['amber', 'amber', 'red'])

    def test_exact_expected_observed_and_configured_calendar_are_inline(self):
        verify = measure(expiry=EXPECTED['expiryTime'] - 86400000).as_dict()
        for calendar in ('jalali', 'gregorian'):
            rows = self.run_ui('renewEvidenceRows(' + json.dumps(verify) + ')', calendar)
            expiry = rows[0]
            self.assertIn(str(EXPECTED['expiryTime']), expiry)
            self.assertIn(str(EXPECTED['expiryTime'] - 86400000), expiry)
            self.assertIn(calendar + ':', expiry)
            self.assertIn('-86400000 ms', rows[2])

    def test_customer_actions_require_active_finalized_operation_and_own_text(self):
        for state in ('UNKNOWN', 'PARTIALLY_APPLIED', 'NOT_APPLIED', 'CONFIG_APPLIED_ACTIVATION_PENDING'):
            payload = {'final_state': state, 'business_finalized': True, 'sending_allowed': True}
            view = self.run_ui('renewOutcomeViewModel(' + json.dumps(payload)
                               + ', {context: {copyText: "success"}})')
            self.assertFalse(view['copyEnabled'])
        active = {'final_state': 'APPLIED_ACTIVE', 'business_finalized': True, 'sending_allowed': True}
        self.assertTrue(self.run_ui('renewOutcomeViewModel(' + json.dumps(active)
                                   + ', {context: {copyText: "success"}})')['copyEnabled'])
        self.assertFalse(self.run_ui('renewOutcomeViewModel({final_state: "APPLIED_ACTIVE"}, {context: {copyText: "old"}})')['copyEnabled'])

    def test_late_recheck_response_cannot_replace_another_operations_modal(self):
        self.assertTrue(self.run_ui('''(async () => {
            global.document = {getElementById: () => null};
            global.showToast = () => {};
            let rendered = 0;
            renderRenewApiStatus = () => {rendered++;};
            applyClientMutation = () => {};
            lastRenewContext = {operationId: 'old', email: 'old-client', serverId: '1'};
            global.fetch = async () => ({json: async () => {
                lastRenewContext = {operationId: 'new', email: 'new-client', serverId: '1'};
                return {verify: {}, copy_text: 'old customer text'};
            }});
            await recheckRenewal();
            return rendered === 0;
        })()'''))


if __name__ == '__main__':
    unittest.main()
