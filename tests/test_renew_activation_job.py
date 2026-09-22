"""The activation reconciler: it resumes activation, it does not renew again.

A renewal whose config is applied but whose client is not active yet must be
finishable AFTER the browser was answered (a slow node, a nodePending write, a
request that died). The tests here pin what this worker may and may not do: it may
re-read the panel, repair activation once per tick, wait for a node, and finish a
business record the request never wrote - and it may never add days or volume,
reset traffic, charge, record a second renewal event, or give up on an account that
is merely slow.
"""
import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta
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
from panel.models import ClientOperation, Transaction  # noqa: E402
from panel.services import client_operations, renew_activation  # noqa: E402

GB = 1024 ** 3
EXPECTED = {'expiryTime': 2_000_000_000_000, 'totalGB': 20 * GB, 'enable': True}


def _layers(*, config_applied=True, converged=False, node_pending=None,
            depleted=False):
    layers = renew_activation.ActivationLayers()
    layers.global_found = True
    layers.global_enable = converged
    layers.global_expiry = EXPECTED['expiryTime'] if config_applied else 1
    layers.global_total = EXPECTED['totalGB'] if config_applied else 1
    layers.expected_inbound_ids = [1]
    layers.found_inbound_ids = [1]
    if converged:
        layers.enabled_inbound_ids = [1]
    else:
        layers.disabled_inbound_ids = [1]
    layers.node_pending = node_pending
    if depleted:
        # The panel holds the renewed cap, but the customer has already consumed all of
        # it: enabling now would be undone by the panel on its next traffic cycle.
        layers.traffic_available = True
        layers.traffic_enable = False
        layers.traffic_up = EXPECTED['totalGB']
        layers.traffic_down = 0
    return renew_activation.classify_layers(layers, expected=EXPECTED)


class ReconcilerTests(unittest.TestCase):
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
        Transaction.query.delete()
        db.session.commit()

    def _operation(self, key='renew-op-rec', *, state='activation_pending',
                   expected=None, response=None):
        operation = ClientOperation(
            idempotency_key=key, request_hash='h', action='renew', admin_id=1,
            server_id=1, inbound_id=1, client_email='renewed@example.com',
            amount=0, state=state,
            expected_json=client_operations._dumps(
                {'expected': EXPECTED if expected is None else expected,
                 'context': {'server_id': 1, 'inbound_id': 1},
                 'prepared_at': datetime.utcnow().isoformat()}),
            response_json=response)
        db.session.add(operation)
        db.session.commit()
        return operation

    def test_converged_activation_completes_the_operation(self):
        operation = self._operation()
        calls = []
        result = job.reconcile_operation(
            operation, read_layers=lambda: _layers(converged=True),
            repair=lambda: calls.append(1))
        self.assertEqual(result['action'], 'completed')
        self.assertEqual(calls, [], 'a converged account must not be written to')
        db.session.refresh(operation)
        self.assertEqual(operation.state, 'completed')

    def test_a_node_pending_panel_is_waited_for_not_rewritten(self):
        operation = self._operation()
        calls = []
        result = job.reconcile_operation(
            operation, read_layers=lambda: _layers(node_pending=True),
            repair=lambda: calls.append(1))
        self.assertEqual(result['action'], 'waiting_for_node')
        self.assertEqual(calls, [], 'a nodePending panel must be waited for')

    def test_activation_is_repaired_once_and_then_completed(self):
        operation = self._operation()
        state = {'repaired': False}

        def _read():
            return _layers(converged=state['repaired'])

        def _repair():
            state['repaired'] = True
            return {'transport_ok': True, 'panel_success': True}

        result = job.reconcile_operation(operation, read_layers=_read, repair=_repair)
        self.assertEqual(result['action'], 'repaired')
        again = job.reconcile_operation(operation, read_layers=_read, repair=_repair)
        self.assertEqual(again['action'], 'completed')
        db.session.refresh(operation)
        self.assertEqual(operation.state, 'completed')

    def test_a_still_depleted_account_is_never_enabled(self):
        operation = self._operation()
        calls = []
        result = job.reconcile_operation(
            operation, read_layers=lambda: _layers(depleted=True),
            repair=lambda: calls.append(1))
        self.assertEqual(result['action'], 'still_depleted')
        self.assertEqual(calls, [], 'enabling a depleted client is a lost race')

    def test_a_config_that_is_no_longer_applied_goes_to_reconciliation(self):
        operation = self._operation()
        result = job.reconcile_operation(
            operation, read_layers=lambda: _layers(config_applied=False))
        self.assertEqual(result['action'], 'needs_reconciliation')
        db.session.refresh(operation)
        self.assertEqual(operation.state, 'needs_reconciliation')

    def test_an_operation_without_a_recorded_expectation_is_not_guessed(self):
        operation = self._operation(expected={})
        result = job.reconcile_operation(operation, read_layers=lambda: _layers())
        self.assertEqual(result['action'], 'needs_reconciliation')

    def test_the_business_record_is_rebuilt_when_the_request_died(self):
        operation = self._operation(response=None)
        events = []

        class _Event:
            id = 7

        result = job.reconcile_operation(
            operation, read_layers=lambda: _layers(converged=True),
            finalize={'record_renewal_event': lambda ctx, exp: events.append('event') or _Event(),
                      'record_transaction': lambda ctx, exp: events.append('tx') or None})
        self.assertEqual(result['action'], 'completed')
        self.assertEqual(events, ['tx', 'event'])
        db.session.refresh(operation)
        import json
        stored = json.loads(operation.response_json or '{}')
        self.assertTrue(stored.get('business_finalized'))
        self.assertTrue(stored.get('rebuilt_after_restart'))

    def test_a_finalized_operation_is_never_rebuilt_again(self):
        import json
        operation = self._operation(response=json.dumps(
            {'business_finalized': True, 'copy_text': 'kept', 'final_state': 'APPLIED_ACTIVE'}))
        events = []
        job.reconcile_operation(
            operation, read_layers=lambda: _layers(converged=True),
            finalize={'record_renewal_event': lambda *a: events.append(1),
                      'record_transaction': lambda *a: events.append(1)})
        self.assertEqual(events, [])

    def test_only_aged_pending_operations_are_picked_up(self):
        fresh = self._operation(key='fresh', state='activation_pending')
        stale = self._operation(key='stale', state='activation_pending')
        done = self._operation(key='done', state='completed')
        ancient = self._operation(key='ancient', state='activation_pending')
        old = datetime.utcnow() - timedelta(hours=job.MAX_AGE_HOURS + 1)
        stale.updated_at = datetime.utcnow() - timedelta(minutes=5)
        ancient.updated_at = old
        db.session.commit()

        keys = {op.idempotency_key for op in job.pending_operations(limit=10)}
        self.assertIn('stale', keys)
        self.assertNotIn('fresh', keys, 'the inline attempts get their chance first')
        self.assertNotIn('done', keys)
        self.assertNotIn('ancient', keys, 'a stale intent must not be repaired')

    def test_a_repair_that_raises_is_recorded_and_does_not_loop(self):
        operation = self._operation()

        def _boom():
            raise RuntimeError('panel exploded')

        result = job.reconcile_operation(operation, read_layers=lambda: _layers(),
                                         repair=_boom)
        self.assertEqual(result['action'], 'repair_raised')
        db.session.refresh(operation)
        self.assertEqual(operation.state, 'activation_pending')


class ReconcilerCountersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def test_run_once_counts_and_never_raises(self):
        with mock.patch.object(job, 'pending_operations', return_value=[]):
            counters = job.run_reconciliation_once()
        self.assertEqual(counters['checked'], 0)

    def test_a_failing_operation_does_not_stop_the_pass(self):
        first = mock.Mock(idempotency_key='a')
        second = mock.Mock(idempotency_key='b')
        calls = []

        def _reconcile(op, **_kwargs):
            calls.append(op.idempotency_key)
            if op.idempotency_key == 'a':
                raise RuntimeError('boom')
            return {'action': 'completed'}

        with mock.patch.object(job, 'pending_operations', return_value=[first, second]), \
                mock.patch.object(job, 'reconcile_operation', side_effect=_reconcile):
            counters = job.run_reconciliation_once()
        self.assertEqual(calls, ['a', 'b'])
        self.assertEqual(counters['completed'], 1)
        self.assertEqual(counters['errors'], 1)


if __name__ == '__main__':
    unittest.main()
