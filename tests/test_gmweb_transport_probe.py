"""The GMweb transport-health probe: verdict mapping and field projection.

Pure unit tests - no HTTP, no app context, no sleeps.
"""
import json
import os
import unittest

os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'test-secret')
os.environ.setdefault('SERVER_PASSWORD_KEY', 'test-key')
os.environ.setdefault('DISABLE_BACKGROUND_THREADS', '1')

from panel.services import gmweb_transport_probe as probe  # noqa: E402


class StatusMappingTests(unittest.TestCase):
    def test_each_failure_gets_its_own_verdict(self):
        """Flattening these into one 'unknown' is the defect this replaces."""
        self.assertEqual(probe.probe_state_for_status(401), probe.PROBE_AUTH_FAILED)
        self.assertEqual(probe.probe_state_for_status(403), probe.PROBE_SCOPE_DENIED)
        self.assertEqual(probe.probe_state_for_status(404), probe.PROBE_CONTRACT_MISSING)
        self.assertEqual(probe.probe_state_for_status(405), probe.PROBE_CONTRACT_MISSING)
        self.assertEqual(probe.probe_state_for_status(500), probe.PROBE_UNREACHABLE)
        self.assertEqual(probe.probe_state_for_status(429), probe.PROBE_INVALID)
        self.assertEqual(probe.probe_state_for_status('nonsense'), probe.PROBE_INVALID)

    def test_the_verdicts_are_distinct(self):
        verdicts = [probe.probe_state_for_status(code)
                    for code in (401, 403, 404, 500, 429)]
        self.assertEqual(len(verdicts), len(set(verdicts)))

    def test_no_failure_maps_to_a_connected_verdict(self):
        for code in (400, 401, 403, 404, 405, 429, 500, 502, 503):
            self.assertNotEqual(probe.probe_state_for_status(code), probe.PROBE_CONNECTED)


class VocabularyTests(unittest.TestCase):
    def test_our_constants_are_the_declared_vocabulary(self):
        declared = set(probe.probe_states())
        for state in (probe.PROBE_CONNECTED, probe.PROBE_NOT_CONFIGURED,
                      probe.PROBE_UNREACHABLE, probe.PROBE_CONTRACT_MISSING,
                      probe.PROBE_VERSION_MISMATCH, probe.PROBE_AUTH_FAILED,
                      probe.PROBE_SCOPE_DENIED, probe.PROBE_INVALID):
            self.assertIn(state, declared)

    def test_every_state_except_connected_explains_itself(self):
        for state in probe.probe_states():
            if state == probe.PROBE_CONNECTED:
                continue
            self.assertTrue(probe.PROBE_DIAGNOSTICS.get(state), state)


SAMPLE = {
    'contract_version': 1,
    'observed_at': '2026-09-26T19:03:48.000Z',
    'gmweb': {'ready': True, 'reason': None},
    'transport': {'active': 'android', 'mode': 'pull', 'state': 'connected', 'reason': None},
    'device': {'state': 'connected', 'reason': None,
               'last_seen_at': '2026-09-26T19:03:45.000Z', 'last_seen_age_ms': 3000,
               'age_ms': 3000, 'deviceKey': 'must-not-survive'},
    'queue': {'pending': 4, 'inflight': 1},
    'last_ack': {'at': '2026-09-26T19:03:41.000Z', 'outcome': 'completed'},
    'unexpected': {'secret': 'must-not-survive'},
}


class ProjectionTests(unittest.TestCase):
    def test_only_declared_fields_survive(self):
        declared = probe.gmweb_contract.transport_health_sections()
        safe = probe.project_sections(SAMPLE)
        self.assertTrue(set(safe).issubset(set(declared)))
        self.assertIn('device', safe)
        # Both spellings of the age are declared, so both are carried.
        self.assertEqual(set(safe['device']),
                         set(declared['device']) & set(SAMPLE['device']))
        self.assertEqual(safe['device']['last_seen_age_ms'], 3000)
        self.assertEqual(safe['device']['age_ms'], 3000)
        self.assertNotIn('deviceKey', safe['device'])
        self.assertNotIn('unexpected', safe)

    def test_the_payload_is_never_echoed_wholesale(self):
        self.assertNotIn('must-not-survive', json.dumps(probe.project_sections(SAMPLE)))

    def test_a_non_dict_payload_projects_to_nothing(self):
        self.assertEqual(probe.project_sections(None), {})
        self.assertEqual(probe.project_sections(['nope']), {})


class SummaryTests(unittest.TestCase):
    def test_a_connected_probe_reports_the_scalars(self):
        summary = probe.summarize(SAMPLE, state=probe.PROBE_CONNECTED, status=200, contract=1)
        self.assertEqual(summary['probe_state'], probe.PROBE_CONNECTED)
        self.assertTrue(summary['configured'])
        self.assertTrue(summary['reachable'])
        self.assertTrue(summary['contract_supported'])
        self.assertEqual(summary['active_transport'], 'android')
        self.assertEqual(summary['device_state'], 'connected')
        self.assertEqual(summary['device_last_seen_age_ms'], 3000)
        self.assertEqual(summary['queue_pending'], 4)
        self.assertEqual(summary['queue_inflight'], 1)
        self.assertEqual(summary['last_ack_outcome'], 'completed')

    def test_the_summary_carries_no_private_field(self):
        serialized = json.dumps(probe.summarize(SAMPLE, state=probe.PROBE_CONNECTED,
                                                status=200, contract=1))
        self.assertNotIn('must-not-survive', serialized)
        self.assertNotIn('deviceKey', serialized)

    def test_an_unreachable_gateway_is_not_reported_as_reachable(self):
        summary = probe.summarize(None, state=probe.PROBE_UNREACHABLE)
        self.assertFalse(summary['reachable'])
        self.assertIsNone(summary['active_transport'])
        self.assertIsNone(summary['device_last_seen_age_ms'])

    def test_an_unconfigured_gateway_is_not_reported_as_configured(self):
        summary = probe.summarize(None, state=probe.PROBE_NOT_CONFIGURED)
        self.assertFalse(summary['configured'])
        self.assertFalse(summary['reachable'])

    def test_a_version_mismatch_is_not_supported(self):
        summary = probe.summarize(SAMPLE, state=probe.PROBE_VERSION_MISMATCH, contract=99)
        self.assertFalse(summary['contract_supported'])


if __name__ == '__main__':
    unittest.main()
