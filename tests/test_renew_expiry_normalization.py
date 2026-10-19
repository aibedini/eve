"""Delayed-start vs absolute expiry: the comparison behind `EXPIRY_MISMATCH`.

Production evidence this suite exists for: a 31-day renewal that started after first
use was requested as ``expiryTime = -2678400000`` (negative = "31 days from first
connection") and the panel reported ``1795080448277`` - the same 31 days turned into
an absolute deadline at write time. Comparing the two raw numbers can never match,
so a correctly applied renewal was classified ``EXPIRY_MISMATCH`` with a difference
of ~20807 days, and the renewal was reported as unverified while the customer's
account actually held the new expiry.

The rule: a representation change is a MATCH when the deadline equals the conversion
anchor plus the requested duration. Anything else is a real mismatch - and the
comparison must anchor on the write, not on "now", or a Re-check hours later would
re-introduce the same false negative.
"""
import unittest

from panel.services import renew_activation as ra

DAY = 86400000
GB = 1024 ** 3

#: The production pair (see the module docstring).
OBSERVED = 1795080448277
EXPECTED = -2678400000                    # -31 days
ANCHOR = OBSERVED - abs(EXPECTED)         # when the write was sent


def measure(*, expected, expiry, anchor_ms=None, now_ms=None):
    """Classify one renewal read-back whose client has every layer enabled."""
    expected = dict(expected)
    expected.setdefault('anchor_ms', anchor_ms)
    expected.setdefault('now_ms', now_ms)
    client = {'expiryTime': expiry, 'totalGB': expected.get('totalGB', 20 * GB),
              'enable': True}
    layers = ra.analyze_activation(
        expected=expected, global_client=client, inbound_ids=[1600],
        memberships={1600: {'enable': True}},
        traffic={'available': True, 'enable': True, 'up': 0, 'down': 0,
                 'total': expected.get('totalGB', 20 * GB)})
    return ra.classify_layers(layers, expected=expected,
                              mutation={'accepted': True}, read={'status': 'ok'})


class NormalizeExpiryTests(unittest.TestCase):
    def test_the_production_pair_is_a_conversion_not_a_mismatch(self):
        result = ra.normalize_expiry(EXPECTED, OBSERVED, anchor_ms=ANCHOR)
        self.assertTrue(result['matched'])
        self.assertEqual(result['mode'], ra.MODE_CONVERTED)
        self.assertEqual(result['target_ms'], OBSERVED)
        self.assertEqual(result['difference_ms'], 0)

    def test_the_same_pair_without_the_anchor_is_not_trusted(self):
        # No recorded write time: comparing against "now" would be a guess, and a
        # guess must not turn an unverified expiry into a match.
        result = ra.normalize_expiry(EXPECTED, OBSERVED,
                                     now_ms=ANCHOR + 3 * 3600 * 1000)
        self.assertFalse(result['matched'])

    def test_a_late_recheck_still_compares_against_the_write(self):
        # Two days after the renewal the anchor has not moved: the conversion is
        # still recognised, which is what keeps Re-check from "losing" the proof.
        later = ANCHOR + 2 * DAY
        result = ra.normalize_expiry(EXPECTED, OBSERVED, anchor_ms=ANCHOR,
                                     now_ms=later)
        self.assertTrue(result['matched'])
        self.assertEqual(result['mode'], ra.MODE_CONVERTED)

    def test_a_conversion_that_lands_a_day_early_is_a_real_mismatch(self):
        result = ra.normalize_expiry(EXPECTED, OBSERVED, anchor_ms=ANCHOR - DAY)
        self.assertFalse(result['matched'])
        self.assertEqual(result['mode'], ra.MODE_MISMATCH)
        self.assertEqual(result['difference_ms'], DAY)

    def test_a_wrong_duration_is_a_real_mismatch(self):
        # The panel converted, but applied 30 days instead of 31.
        result = ra.normalize_expiry(EXPECTED, OBSERVED - DAY, anchor_ms=ANCHOR)
        self.assertFalse(result['matched'])
        self.assertEqual(result['difference_ms'], -DAY)

    def test_two_absolute_deadlines_use_a_tolerance(self):
        self.assertTrue(ra.normalize_expiry(2_000_000_000_000,
                                            2_000_000_030_000)['matched'])
        self.assertFalse(ra.normalize_expiry(2_000_000_000_000,
                                             2_000_000_120_000)['matched'])
        self.assertEqual(ra.normalize_expiry(2_000_000_000_000, 2_000_000_030_000)['mode'],
                         ra.MODE_ABSOLUTE)

    def test_two_pending_durations_are_compared_as_durations(self):
        match = ra.normalize_expiry(-30 * DAY, -30 * DAY)
        self.assertTrue(match['matched'])
        self.assertEqual(match['mode'], ra.MODE_PENDING)
        mismatch = ra.normalize_expiry(-30 * DAY, -31 * DAY)
        self.assertFalse(mismatch['matched'])
        self.assertEqual(mismatch['difference_ms'], -DAY)

    def test_unlimited_zero_matches_zero(self):
        result = ra.normalize_expiry(0, 0)
        self.assertTrue(result['matched'])

    def test_a_missing_value_is_unknown_not_a_match(self):
        for expected, observed in ((None, OBSERVED), (EXPECTED, None), (None, None)):
            result = ra.normalize_expiry(expected, observed, anchor_ms=ANCHOR)
            self.assertIsNone(result['matched'], (expected, observed))
            self.assertEqual(result['mode'], ra.MODE_UNKNOWN)

    def test_the_reverse_conversion_is_understood(self):
        # An absolute deadline was requested; the panel kept it in pending form. What
        # it stores is what was left of the deadline, not the original duration.
        absolute = ANCHOR + DAY
        result = ra.normalize_expiry(absolute, -DAY, anchor_ms=ANCHOR)
        self.assertTrue(result['matched'])
        self.assertEqual(result['mode'], ra.MODE_CONVERTED)


class ClassifiedRenewalExpiryTests(unittest.TestCase):
    def test_a_converted_delayed_start_verifies_as_applied(self):
        layers = measure(expected={'expiryTime': EXPECTED, 'totalGB': 20 * GB,
                                   'inbound_id': 1600},
                         expiry=OBSERVED, anchor_ms=ANCHOR)
        self.assertTrue(layers.config_applied)
        self.assertNotIn('EXPIRY_MISMATCH', layers.reason_codes)
        self.assertEqual(layers.final_state, ra.STATE_APPLIED_ACTIVE)
        self.assertTrue(layers.activation_converged)

    def test_the_evidence_names_the_representation_that_was_compared(self):
        layers = measure(expected={'expiryTime': EXPECTED, 'totalGB': 20 * GB},
                         expiry=OBSERVED, anchor_ms=ANCHOR)
        expiry = layers.evidence['expiry']
        self.assertEqual(expiry['mode'], ra.MODE_CONVERTED)
        self.assertTrue(expiry['matched'])
        self.assertEqual(expiry['anchor_ms'], ANCHOR)
        self.assertEqual(expiry['observed_ms'], OBSERVED)
        # The raw difference is still reported (it is what a human reads in the modal),
        # it is just no longer what decides the verdict.
        self.assertEqual(expiry['difference_days'], OBSERVED / DAY - EXPECTED / DAY)

    def test_a_real_expiry_mismatch_still_blocks_the_renewal(self):
        layers = measure(expected={'expiryTime': EXPECTED, 'totalGB': 20 * GB},
                         expiry=OBSERVED - 5 * DAY, anchor_ms=ANCHOR)
        self.assertIn('EXPIRY_MISMATCH', layers.reason_codes)
        self.assertFalse(layers.config_applied)
        self.assertNotEqual(layers.final_state, ra.STATE_APPLIED_ACTIVE)

    def test_a_pending_expiry_the_panel_kept_is_still_valid(self):
        # No conversion happened: the client simply has not connected yet.
        layers = measure(expected={'expiryTime': -31 * DAY, 'totalGB': 20 * GB},
                         expiry=-31 * DAY)
        self.assertTrue(layers.config_applied)
        self.assertNotIn('EXPIRY_MISMATCH', layers.reason_codes)


class FailureAttributionTests(unittest.TestCase):
    def test_a_disabled_membership_is_named_with_the_inbounds_that_fail(self):
        layers = ra.ActivationLayers()
        layers.global_found = True
        layers.global_enable = True
        layers.global_expiry = 2_000_000_000_000
        layers.global_total = 20 * GB
        layers.expected_inbound_ids = [5, 10, 17]
        layers.found_inbound_ids = [5, 10, 17]
        layers.enabled_inbound_ids = [5]
        layers.disabled_inbound_ids = [10, 17]
        layers = ra.classify_layers(
            layers,
            expected={'expiryTime': 2_000_000_000_000, 'totalGB': 20 * GB},
            mutation={'accepted': True}, read={'status': 'ok'})
        failures = ra.activation_failures(layers)
        membership = [item for item in failures if item['code'] == 'MEMBERSHIP_DISABLED']
        self.assertEqual(len(membership), 1)
        self.assertEqual(membership[0]['inbound_ids'], [10, 17])
        self.assertEqual(membership[0]['scope'], 'membership')
        self.assertTrue(membership[0]['repairable'])

    def test_a_global_disable_is_repairable_but_an_expiry_mismatch_is_not(self):
        global_disabled = ra.ActivationLayers()
        global_disabled.global_found = True
        global_disabled.global_enable = False
        global_disabled.global_expiry = 2_000_000_000_000
        global_disabled.global_total = 20 * GB
        global_disabled = ra.classify_layers(
            global_disabled,
            expected={'expiryTime': 2_000_000_000_000, 'totalGB': 20 * GB},
            mutation={'accepted': True}, read={'status': 'ok'})
        codes = {item['code']: item for item in ra.activation_failures(global_disabled)}
        self.assertTrue(codes['GLOBAL_DISABLED']['repairable'])

        mismatch = measure(expected={'expiryTime': 2_000_000_000_000},
                           expiry=2_000_000_000_000 - 3 * DAY)
        codes = {item['code']: item for item in ra.activation_failures(mismatch)}
        self.assertIn('EXPIRY_MISMATCH', codes)
        self.assertFalse(codes['EXPIRY_MISMATCH']['repairable'])
        self.assertEqual(codes['EXPIRY_MISMATCH']['difference_days'], -3)

    def test_a_converged_renewal_has_no_failed_steps(self):
        layers = measure(expected={'expiryTime': EXPECTED, 'totalGB': 20 * GB},
                         expiry=OBSERVED, anchor_ms=ANCHOR)
        self.assertEqual(ra.activation_failures(layers), [])


if __name__ == '__main__':
    unittest.main()
