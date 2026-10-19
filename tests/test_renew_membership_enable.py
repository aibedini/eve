"""Enabling the per-inbound membership, not just the global client record.

A 3x-ui v3 client has two independent ``enable`` flags: the global record returned by
``/clients/get/{email}`` and the copy of the client inside each inbound's
``settings.clients[]``. The production activation report was

    Global enable: false
    Membership enabled: false · Disabled inbound IDs: 5, 10, 11, 12, 13, 14, 15, 17

and the repair path only ever called ``bulkEnable`` / ``clients/update``, which speak
for the first flag. The membership rows stayed disabled, the customer stayed offline,
and EVE kept reporting the renewal as "activation pending" with no way out.

These tests pin the membership repair: it writes only the inbounds that are actually
disabled, it is idempotent, and it reports a failure instead of claiming success.
"""
import unittest
from unittest import mock

from panel.adapters import xui as xui


def _inbound(inbound_id, *, email='renewed@example.com', enable=False,
             settings_as_json=True, extra_client=False):
    clients = [{'id': 'uuid-%d' % inbound_id, 'email': email, 'enable': enable,
                'totalGB': 10}]
    if extra_client:
        clients.append({'id': 'uuid-other', 'email': 'someone-else@example.com',
                        'enable': True, 'totalGB': 1})
    settings = {'clients': clients}
    return {'id': inbound_id, 'remark': 'in-%d' % inbound_id,
            'settings': __import__('json').dumps(settings) if settings_as_json else settings,
            'streamSettings': {'network': 'tcp'}}


class MembershipEnableTests(unittest.TestCase):
    def setUp(self):
        self.server = mock.Mock(host='https://panel.example:8443', panel_type='3x-ui')
        self.session = mock.Mock()

    def _run(self, inbounds, **kwargs):
        with mock.patch.object(xui, '_push_full_inbound',
                               return_value=(True, None)) as push:
            result = xui.v3_enable_client_memberships(
                self.server, self.session, 'renewed@example.com',
                inbounds=inbounds, **kwargs)
        return result, push

    def test_a_disabled_membership_is_enabled_by_pushing_its_inbound(self):
        inbounds = [_inbound(5), _inbound(10)]
        result, push = self._run(inbounds)
        self.assertTrue(result['ok'])
        self.assertEqual(result['enabled_ids'], [5, 10])
        self.assertTrue(result['attempted'])
        self.assertEqual(push.call_count, 2)
        # The write carries the same inbound with ONLY the enable flag changed.
        pushed_inbound, pushed_settings = push.call_args[0][2], push.call_args[0][3]
        self.assertEqual(pushed_inbound['id'], 10)
        rows = pushed_settings['clients']
        self.assertTrue(all(row['enable'] is True for row in rows))
        self.assertEqual(rows[0]['totalGB'], 10, 'unrelated fields must be preserved')

    def test_an_already_enabled_membership_is_not_written(self):
        inbounds = [_inbound(5, enable=True), _inbound(10, enable=False)]
        result, push = self._run(inbounds)
        self.assertEqual(result['enabled_ids'], [10])
        self.assertEqual(result['already_enabled_ids'], [5])
        self.assertEqual(push.call_count, 1)
        pushed_inbound = push.call_args[0][2]
        self.assertEqual(pushed_inbound['id'], 10)

    def test_the_repair_is_idempotent(self):
        first, first_push = self._run([_inbound(5, enable=False)])
        self.assertEqual(first['enabled_ids'], [5])
        self.assertEqual(first_push.call_count, 1)
        # Second attempt reads the PANEL state again, which now reports the membership
        # as enabled: nothing is rewritten and no node is restarted for nothing.
        second, second_push = self._run([_inbound(5, enable=True)])
        self.assertEqual(second['enabled_ids'], [])
        self.assertEqual(second['already_enabled_ids'], [5])
        self.assertFalse(second['attempted'])
        self.assertEqual(second_push.call_count, 0)

    def test_only_the_attached_inbounds_are_considered(self):
        inbounds = [_inbound(5), _inbound(10), _inbound(11)]
        result, push = self._run(inbounds, inbound_ids=[10, 11])
        self.assertEqual(result['enabled_ids'], [10, 11])
        self.assertEqual(push.call_count, 2)
        written = sorted(call[0][2]['id'] for call in push.call_args_list)
        self.assertEqual(written, [10, 11])

    def test_a_client_that_is_not_a_member_is_reported_not_written(self):
        inbounds = [_inbound(5, email='other@example.com')]
        result, push = self._run(inbounds)
        self.assertEqual(result['not_member_ids'], [5])
        self.assertEqual(result['enabled_ids'], [])
        self.assertEqual(push.call_count, 0)

    def test_one_failing_inbound_does_not_hide_the_others(self):
        inbounds = [_inbound(5), _inbound(10)]

        def _push(server, session, inbound, settings):
            if inbound['id'] == 5:
                return False, 'HTTP 500'
            return True, None

        with mock.patch.object(xui, '_push_full_inbound', side_effect=_push):
            result = xui.v3_enable_client_memberships(
                self.server, self.session, 'renewed@example.com', inbounds=inbounds)
        self.assertFalse(result['ok'])
        self.assertEqual(result['enabled_ids'], [10])
        self.assertEqual([item['inbound_id'] for item in result['failed']], [5])
        self.assertIn('inbound 5', result['error'])

    def test_a_decoded_settings_object_is_handled_too(self):
        inbounds = [_inbound(5, settings_as_json=False)]
        result, push = self._run(inbounds)
        self.assertEqual(result['enabled_ids'], [5])
        self.assertEqual(push.call_count, 1)


class EnableEverywhereTests(unittest.TestCase):
    def setUp(self):
        self.server = mock.Mock(host='https://panel.example:8443', panel_type='3x-ui')
        self.session = mock.Mock()

    def test_both_layers_are_reported_separately(self):
        memberships = {'ok': True, 'enabled_ids': [5], 'already_enabled_ids': [],
                       'not_member_ids': [], 'failed': [], 'attempted': True,
                       'error': None}
        with mock.patch.object(xui, 'v3_enable_client', return_value=(False, None, 'denied')), \
                mock.patch.object(xui, 'v3_enable_client_memberships',
                                  return_value=memberships):
            ok, summary, error = xui.v3_enable_client_everywhere(
                self.server, self.session, 'renewed@example.com', {'email': 'renewed@example.com'})
        self.assertFalse(ok)
        self.assertFalse(summary['global_ok'])
        self.assertTrue(summary['memberships']['ok'])
        self.assertEqual(summary['global_error'], 'denied')
        self.assertEqual(error, 'denied')
        self.assertTrue(summary['transport_ok'],
                        'a partial repair still reached the panel')

    def test_a_clean_repair_reports_success_for_both_layers(self):
        memberships = {'ok': True, 'enabled_ids': [], 'already_enabled_ids': [5],
                       'not_member_ids': [], 'failed': [], 'attempted': False,
                       'error': None}
        with mock.patch.object(xui, 'v3_enable_client', return_value=(True, {}, None)), \
                mock.patch.object(xui, 'v3_enable_client_memberships',
                                  return_value=memberships):
            ok, summary, error = xui.v3_enable_client_everywhere(
                self.server, self.session, 'renewed@example.com', {'email': 'renewed@example.com'})
        self.assertTrue(ok)
        self.assertTrue(summary['panel_success'])
        self.assertIsNone(error)


if __name__ == '__main__':
    unittest.main()
