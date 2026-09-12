"""Phase 2: one canonical client shape and one mutation result.

Every panel mutation (create, edit, renew, enable, disable, reset, delete, rotate)
reports the same way, and the rule is encoded in the shape: a write is not UI state
until the panel has been read back, so `to_payload()` only exposes `client_state`
when the result is verified (or the operation is a delete, where absence is the
state).
"""
import unittest

from panel.services.client_state import (
    CLIENT_STATE_FIELDS,
    ClientMutationResult,
    normalize_client_state,
    verified_state_from_panel,
)

GB = 1024 ** 3


class NormalizeClientStateTests(unittest.TestCase):
    def _raw(self):
        return {'id': 'uuid-bob', 'email': 'bob', 'comment': 'x', 'enable': True,
                'totalGB': 35 * GB, 'expiryTime': 1_800_000_000_000}

    def test_the_canonical_shape_is_complete(self):
        raw = self._raw()
        row = {'up': 25 * GB, 'down': 0, 'remaining_bytes': 10 * GB,
               'service_state': 'active', 'inbound_id': 1, 'raw_client': raw}

        state = normalize_client_state(raw=raw, row=row)

        self.assertEqual(set(state), set(CLIENT_STATE_FIELDS))
        self.assertEqual(state['uuid'], 'uuid-bob')
        self.assertEqual(state['email'], 'bob')
        self.assertTrue(state['enable'])
        self.assertEqual(state['total_bytes'], 35 * GB)
        self.assertEqual(state['used_up'], 25 * GB)
        self.assertEqual(state['used_down'], 0)
        self.assertEqual(state['remaining_bytes'], 10 * GB)
        self.assertEqual(state['expiry_time'], 1_800_000_000_000)
        self.assertEqual(state['service_state'], 'active')
        self.assertEqual(state['inbound_id'], 1)

    def test_remaining_is_computed_when_the_row_does_not_carry_it(self):
        state = normalize_client_state(row={'raw_client': self._raw(), 'up': 25 * GB, 'down': 0})
        self.assertEqual(state['remaining_bytes'], 10 * GB)

    def test_unlimited_volume_uses_the_project_sentinel(self):
        state = normalize_client_state(raw={'email': 'x', 'totalGB': 0}, used_up=5, used_down=5)
        self.assertEqual(state['total_bytes'], 0)
        self.assertEqual(state['remaining_bytes'], -1)

    def test_a_panel_read_back_normalizes_without_a_cached_row(self):
        state = verified_state_from_panel(self._raw(), up=25 * GB, down=0,
                                          service_state='active', inbound_id=7)
        self.assertEqual(state['remaining_bytes'], 10 * GB)
        self.assertEqual(state['inbound_id'], 7)
        self.assertEqual(state['total_bytes'], 35 * GB)


class ClientMutationResultTests(unittest.TestCase):
    def _state(self):
        return normalize_client_state(raw={'id': 'u', 'email': 'bob', 'enable': True,
                                           'totalGB': 35 * GB}, used_up=25 * GB)

    def test_it_is_truthy_only_when_the_cache_changed(self):
        # patch_cached_client used to return a bool; callers still test it that way.
        self.assertFalse(ClientMutationResult(server_id=1, email='a', operation='update'))
        self.assertTrue(ClientMutationResult(server_id=1, email='a', operation='update',
                                             changed=True))

    def test_unverified_state_is_not_adoptable_by_the_ui(self):
        result = ClientMutationResult(server_id=1, email='bob', operation='renew',
                                      changed=True, client_state=self._state(),
                                      verified=False)
        payload = result.to_payload()
        self.assertFalse(payload['verified'])
        self.assertIsNone(payload['client_state'], 'unverified state must not reach the UI')

    def test_verified_state_travels_with_the_operation(self):
        result = ClientMutationResult(server_id=7, email='bob', operation='renew',
                                      client_id='uuid-bob', changed=True, verified=True,
                                      client_state=self._state(), server_revision=42,
                                      snapshot_revision=1042)
        payload = result.to_payload()
        self.assertTrue(payload['verified'])
        self.assertEqual(payload['server_revision'], 42)
        # The browser's poll cursor is a snapshot revision, not the per-server counter.
        self.assertEqual(payload['snapshot_revision'], 1042)
        self.assertEqual(payload['operation'], 'renew')
        self.assertEqual(payload['client_state']['total_bytes'], 35 * GB)
        self.assertEqual(payload['client_state']['remaining_bytes'], 10 * GB)

    def test_a_delete_needs_no_read_back(self):
        result = ClientMutationResult(server_id=1, email='bob', operation='delete',
                                      deleted=True, changed=True)
        payload = result.to_payload()
        self.assertTrue(payload['deleted'])
        self.assertIsNone(payload['client_state'])
        self.assertFalse(payload['verified'])


if __name__ == '__main__':
    unittest.main()
