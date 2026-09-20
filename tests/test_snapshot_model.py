import unittest

from panel.core import snapshot_model


UUID_A = '4ce7db6e-4576-4e55-bb2a-452487fe1bb6'


class SnapshotModelTests(unittest.TestCase):
    def _mirrors(self):
        return [
            {'id': inbound_id, 'server_id': 7, 'remark': 'i%d' % inbound_id,
             'clients': [{'id': UUID_A, 'email': 'User@Example.com', 'total': 100,
                          'inbound_id': inbound_id}]}
            for inbound_id in (10, 20, 30, 40)
        ]

    def test_one_uuid_becomes_one_entity_and_four_memberships(self):
        retained, block = snapshot_model.normalize_retained_block(self._mirrors(), 7)
        self.assertEqual(len(block['clients']), 1)
        key = 'uuid:' + UUID_A
        self.assertEqual(block['membership_index'][key], [10, 20, 30, 40])
        self.assertTrue(all(inbound['clients'][0] is retained[0]['clients'][0]
                            for inbound in retained))

    def test_same_email_with_distinct_reliable_uuids_stays_distinct(self):
        rows = [{'id': 1, 'clients': [
            {'id': UUID_A, 'email': 'same@example.com'},
            {'id': 'ff5f8c59-9fd9-4609-93fa-5c5a0bbf02a4', 'email': 'same@example.com'},
        ]}]
        block = snapshot_model.normalize_server_block(rows, 7)
        self.assertEqual(len(block['clients']), 2)

    def test_legacy_rows_are_not_normalized_implicitly(self):
        original = self._mirrors()
        self.assertEqual(len(original), 4)
        self.assertIsNot(original[0]['clients'][0], original[1]['clients'][0])

    def test_materialization_restores_membership_specific_inbound_id(self):
        block = snapshot_model.normalize_server_block(self._mirrors(), 7)
        expanded = snapshot_model.materialize_server_block(block)
        self.assertEqual([row['clients'][0]['inbound_id'] for row in expanded],
                         [10, 20, 30, 40])
        self.assertIsNot(expanded[0]['clients'][0], expanded[1]['clients'][0])

    def test_membership_overrides_round_trip(self):
        rows = self._mirrors()[:2]
        rows[1]['clients'][0]['total'] = 200
        block = snapshot_model.normalize_server_block(rows, 7)
        expanded = snapshot_model.materialize_server_block(block)
        self.assertEqual([row['clients'][0]['total'] for row in expanded], [100, 200])

    def test_membership_deletion_keeps_entity_until_last_reference(self):
        block = snapshot_model.normalize_server_block(self._mirrors()[:2], 7)
        key = 'uuid:' + UUID_A
        self.assertTrue(snapshot_model.remove_membership(block, key, 10))
        self.assertIn(key, block['clients'])
        self.assertEqual(snapshot_model.affected_inbound_ids(block, key), [20])
        snapshot_model.remove_membership(block, key, 20)
        self.assertNotIn(key, block['clients'])

    def test_unknown_schema_is_rejected(self):
        with self.assertRaises(snapshot_model.UnknownSnapshotSchema):
            snapshot_model.hydrate_server_block({'schema_version': 99})

    def test_requested_retained_view_materializes_only_named_inbound(self):
        retained, _ = snapshot_model.normalize_retained_block(self._mirrors(), 7)
        view = snapshot_model.materialize_retained_inbounds(
            retained, {7}, keys=[[7, 20]])
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]['id'], 20)
        self.assertEqual(view[0]['clients'][0]['inbound_id'], 20)
        self.assertNotIn('inbound_id', retained[1]['clients'][0])


if __name__ == '__main__':
    unittest.main()
