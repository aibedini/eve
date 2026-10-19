"""The renewal confirmation follows the OPERATION, not the browser.

The production complaint: the SMS was fired inside the renewal request, right after a
verification that ran one second after the panel write. When activation had not
converged yet the send was skipped - and nothing ever retried it, so a customer whose
renewal WAS applied never received the message because the operator closed the modal
too early.

These tests pin the replacement contract:

* one message per operation, ever - a durable marker on the operation row, not a
  time window or a cache;
* sent by whoever observes convergence first (request, Re-check, Force Enable, the
  background reconciler);
* never sent while activation is unconfirmed, and never for an operation that has no
  customer text;
* a node that is merely synchronising is allowed through after two positive reads;
* a refused send is not marked, so it stays retryable.

No gateway, no network, no sleeps: the sender is injected.
"""
import base64
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault(
    'SERVER_PASSWORD_KEY',
    base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from panel.extensions import db  # noqa: E402
from panel.jobs import renew_activation as job  # noqa: E402
from panel.models import ClientOperation  # noqa: E402
from panel.services import client_operations, renew_activation, renew_notifications  # noqa: E402

GB = 1024 ** 3
EXPECTED = {'expiryTime': 2_000_000_000_000, 'totalGB': 20 * GB, 'enable': True}


def _business_payload(**overrides):
    payload = {
        'business_finalized': True,
        'copy_text': 'سلام، اکانت شما تمدید شد.',
        'tpl_vars': {'email': 'renewed@example.com', 'volume': '20GB'},
        'client_comment': 'phone 09120000000',
        'was_reactivated': True,
        'expected': EXPECTED,
    }
    payload.update(overrides)
    return payload


class ConfirmationSenderTests(unittest.TestCase):
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
        ClientOperation.query.delete()
        db.session.commit()
        self.sent = []

    def _operation(self, key='renew-sms-1', *, response=None):
        operation = ClientOperation(
            idempotency_key=key, request_hash='h', action='renew', admin_id=1,
            server_id=1, inbound_id=1, client_email='renewed@example.com',
            amount=0, state='activation_pending',
            expected_json=client_operations._dumps(
                {'expected': EXPECTED, 'context': {'server_id': 1, 'inbound_id': 1},
                 'prepared_at': datetime.utcnow().isoformat()}),
            response_json=(client_operations._dumps(response)
                           if response is not None else None))
        db.session.add(operation)
        db.session.commit()
        return operation

    def _sender(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    def test_a_converged_operation_is_messaged_exactly_once(self):
        operation = self._operation(response=_business_payload())
        first = renew_notifications.send_renewal_confirmation_once(
            operation, activation_converged=True, sender=self._sender)
        self.assertTrue(first['sent'])
        self.assertEqual(len(self.sent), 1)
        args = self.sent[0][0]
        self.assertEqual(args[0], 'renew')
        self.assertEqual(args[1], 1)
        self.assertEqual(args[2], 'renewed@example.com')
        self.assertEqual(args[5]['email'], 'renewed@example.com')

        second = renew_notifications.send_renewal_confirmation_once(
            operation, activation_converged=True, sender=self._sender)
        self.assertFalse(second['sent'])
        self.assertEqual(second['reason'], 'already_sent')
        self.assertEqual(len(self.sent), 1, 'the customer must never be texted twice')

    def test_the_marker_is_durable_on_the_operation_row(self):
        operation = self._operation(response=_business_payload())
        renew_notifications.send_renewal_confirmation_once(
            operation, activation_converged=True, sender=self._sender)
        db.session.expire_all()
        reread = ClientOperation.query.filter_by(idempotency_key='renew-sms-1').one()
        self.assertTrue(renew_notifications.renewal_sms_was_sent(reread))
        stored = json.loads(reread.response_json)
        self.assertTrue(stored.get('renew_sms_sent_at'))
        self.assertTrue(renew_notifications.send_renewal_confirmation_once(
            reread, activation_converged=True, sender=self._sender)['reason'] == 'already_sent')

    def test_an_unconfirmed_activation_withholds_the_message(self):
        operation = self._operation(response=_business_payload())
        result = renew_notifications.send_renewal_confirmation_once(
            operation, activation_converged=False, sender=self._sender)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'not_active')
        self.assertEqual(self.sent, [])
        self.assertFalse(renew_notifications.renewal_sms_was_sent(operation))

    def test_a_node_that_is_still_syncing_needs_two_confirmations(self):
        operation = self._operation(response=_business_payload())
        first = renew_notifications.send_renewal_confirmation_once(
            operation, activation_converged=True, node_pending=True, sender=self._sender)
        self.assertFalse(first['sent'])
        self.assertEqual(first['reason'], 'awaiting_node_sync_confirmation')
        self.assertEqual(first['confirmations'], 1)
        self.assertEqual(self.sent, [])

        second = renew_notifications.send_renewal_confirmation_once(
            operation, activation_converged=True, node_pending=True, sender=self._sender)
        self.assertTrue(second['sent'])
        self.assertEqual(len(self.sent), 1)

    def test_every_enable_layer_active_lets_the_node_pending_message_through(self):
        # Convergence reported by the enable layers is what the two reads confirm; a
        # panel that never clears nodePending must not withhold the message for ever.
        operation = self._operation(response=_business_payload())
        for _ in range(renew_notifications.NODE_SYNC_CONFIRMATIONS):
            result = renew_notifications.send_renewal_confirmation_once(
                operation, activation_converged=True, node_pending=True,
                sender=self._sender)
        self.assertTrue(result['sent'])

    def test_an_operation_without_customer_text_is_not_messaged(self):
        operation = self._operation(response=_business_payload(tpl_vars={}, copy_text=None))
        result = renew_notifications.send_renewal_confirmation_once(
            operation, activation_converged=True, sender=self._sender)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'no_text')
        self.assertEqual(self.sent, [])

    def test_an_operation_without_a_business_record_is_not_messaged(self):
        operation = self._operation(response=None)
        result = renew_notifications.send_renewal_confirmation_once(
            operation, activation_converged=True, sender=self._sender)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'no_business_record')

    def test_a_failed_send_stays_retryable(self):
        operation = self._operation(response=_business_payload())

        def _boom(*args, **kwargs):
            raise RuntimeError('gateway down')

        failed = renew_notifications.send_renewal_confirmation_once(
            operation, activation_converged=True, sender=_boom)
        self.assertFalse(failed['sent'])
        self.assertEqual(failed['reason'], 'send_raised')
        self.assertFalse(renew_notifications.renewal_sms_was_sent(operation),
                         'a refused send must not burn the once-only marker')

        retried = renew_notifications.send_renewal_confirmation_once(
            operation, activation_converged=True, sender=self._sender)
        self.assertTrue(retried['sent'])

    def test_no_operation_is_reported_instead_of_guessed(self):
        result = renew_notifications.send_renewal_confirmation_once(
            None, activation_converged=True, sender=self._sender)
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'no_operation')


def _layers(*, converged=True, node_pending=None):
    layers = renew_activation.ActivationLayers()
    layers.global_found = True
    layers.global_enable = True
    layers.global_expiry = EXPECTED['expiryTime']
    layers.global_total = EXPECTED['totalGB']
    layers.expected_inbound_ids = [1]
    layers.found_inbound_ids = [1]
    if converged:
        layers.enabled_inbound_ids = [1]
    else:
        layers.disabled_inbound_ids = [1]
    layers.node_pending = node_pending
    return renew_activation.classify_layers(layers, expected=EXPECTED,
                                            mutation={'accepted': True},
                                            read={'status': 'ok'})


class ReconcilerConfirmationTests(unittest.TestCase):
    """The background reconciler is the path that survives a closed browser."""

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
        ClientOperation.query.delete()
        db.session.commit()
        self.calls = []

    def _operation(self, key='renew-sms-job-1', response=None):
        operation = ClientOperation(
            idempotency_key=key, request_hash='h', action='renew', admin_id=1,
            server_id=1, inbound_id=1, client_email='renewed@example.com',
            amount=0, state='activation_pending',
            expected_json=client_operations._dumps(
                {'expected': EXPECTED, 'context': {'server_id': 1, 'inbound_id': 1},
                 'prepared_at': datetime.utcnow().isoformat()}),
            response_json=(client_operations._dumps(response)
                           if response is not None else None))
        db.session.add(operation)
        db.session.commit()
        return operation

    def _notify(self, operation, *, activation_converged, node_pending=None):
        self.calls.append((activation_converged, node_pending))
        return {'sent': True, 'reason': None}

    def test_a_late_convergence_sends_the_message_if_the_request_did_not(self):
        operation = self._operation(response=_business_payload())
        result = job.reconcile_operation(
            operation, read_layers=lambda: _layers(converged=True),
            notify=self._notify)
        self.assertEqual(result['action'], 'completed')
        self.assertEqual(self.calls, [(True, False)])
        self.assertTrue(result['confirmation']['sent'])

    def test_a_node_pending_wait_asks_for_the_confirmation_path(self):
        operation = self._operation(response=_business_payload())
        result = job.reconcile_operation(
            operation, read_layers=lambda: _layers(converged=True, node_pending=True),
            notify=self._notify)
        self.assertEqual(result['action'], 'waiting_for_node')
        self.assertEqual(self.calls, [(True, True)])

    def test_an_unconverged_operation_is_not_messaged(self):
        operation = self._operation(response=_business_payload())
        result = job.reconcile_operation(
            operation, read_layers=lambda: _layers(converged=False),
            repair=lambda: {'transport_ok': True, 'panel_success': True},
            notify=self._notify)
        self.assertNotEqual(result['action'], 'completed')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
