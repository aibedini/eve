"""XR-GA-019: EVE proposals inbox (GOD AI submission + operator decision).

Covers the machine API contract for proposals (auth, scopes, idempotency,
validation, masked listing) and the operator review flow (pending-only queue,
generation revalidation, accept/reject semantics and the audit trail). Accepting a
proposal must never send an SMS or mutate a client, so those side effects are
asserted absent.

No network, no server, and no production database: a temporary SQLite file is
bound through DATABASE_URL before the app is imported.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB_FILE.name.replace(os.sep, '/'))
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import Admin, app, db  # noqa: E402
from panel.models import (  # noqa: E402
    AuditLog,
    CustomerAccount,
    MachineApiKey,
    Proposal,
    Server,
    ServiceLifecycleState,
    ServiceNotificationOutbox,
    ServiceOwnership,
    SmsSendLog,
)
from panel.services import machine_api, permissions, proposals  # noqa: E402


def _utcnow():
    return datetime.utcnow()


class ProposalTestBase(unittest.TestCase):
    """Isolated scratch DB with one customer, one service and one reviewer."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls._clear()
        db.session.remove()
        cls.ctx.pop()

    @classmethod
    def _clear(cls):
        # Dependents first. Every delete is best-effort so a suite that dropped a
        # table earlier in the same process cannot break this file.
        for model in (Proposal, AuditLog, MachineApiKey, SmsSendLog,
                      ServiceNotificationOutbox, ServiceLifecycleState,
                      ServiceOwnership, CustomerAccount, Admin, Server):
            try:
                model.query.delete()
            except Exception:
                db.session.rollback()
        db.session.commit()
        # SQLite reuses primary keys after a bulk delete; drop the stale identity
        # map so a new key/admin can never collide with a previous test's object.
        db.session.remove()

    def setUp(self):
        type(self)._clear()
        self.server = Server(name='DE-3', host='10.0.0.1', username='root',
                             password='not-a-secret', enabled=True)
        # No password login in these tests (the session is injected), so a dummy
        # hash avoids hashing a real password twice per test.
        self.reviewer = Admin(username='reviewer', role='admin', enabled=True,
                              password_hash='x')
        self.reseller = Admin(username='reseller', role='reseller', enabled=True,
                              allowed_servers='[]', password_hash='x')
        db.session.add_all([self.server, self.reviewer, self.reseller])
        db.session.commit()

        self.customer = CustomerAccount(display_name='Ali Rezaei',
                                        primary_phone='989195292411',
                                        status='active', credit=12000)
        db.session.add(self.customer)
        db.session.commit()

        self.email = 'g276-09195292411@example.test'
        self.service_key = 'eve:%d:client-uuid-1' % self.server.id
        self.lifecycle = ServiceLifecycleState(
            service_key=self.service_key, server_id=self.server.id,
            client_uuid='client-uuid-1', client_email=self.email,
            generation=37, last_lifecycle_change_at=_utcnow(),
        )
        self.ownership = ServiceOwnership(
            customer_id=self.customer.id, server_id=self.server.id,
            client_uuid='client-uuid-1', client_email_snapshot=self.email,
            verification_method='admin', verified_at=_utcnow(),
        )
        db.session.add_all([self.lifecycle, self.ownership])
        db.session.commit()

        self.key, self.token = machine_api.create_machine_api_key(
            'proposal-writer',
            [machine_api.SCOPE_PROPOSALS_READ, machine_api.SCOPE_PROPOSALS_WRITE],
            created_by='unit-test')

    # -- request helpers -----------------------------------------------------
    def auth(self, token=None):
        return {'Authorization': 'Bearer %s' % (token or self.token)}

    def machine_post(self, body, token=None, headers=None):
        merged = dict(self.auth(token))
        merged.update(headers or {})
        return self.client.post('/machine/v1/proposals', json=body, headers=merged)

    def machine_get(self, query='', token=None):
        return self.client.get('/machine/v1/proposals' + query,
                               headers=self.auth(token))

    def login(self, admin):
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
            browser_session.update(admin_id=admin.id,
                                   admin_username=admin.username,
                                   role=admin.role,
                                   is_superadmin=bool(admin.is_superadmin))

    def proposal_body(self, proposal_id, **overrides):
        body = {
            'proposal_id': proposal_id,
            'type': 'reminder_text',
            'payload': {'text': 'Your subscription expires soon.'},
        }
        body.update(overrides)
        return body

    def create(self, proposal_id, **overrides):
        row, created = proposals.create_proposal(
            proposal_id, overrides.pop('type', 'reminder_text'),
            overrides.pop('payload', {'text': 'Your subscription expires soon.'}),
            **overrides)
        return row


class ProposalsMachineAuthTests(ProposalTestBase):
    def test_missing_bearer_token_is_401(self):
        response = self.client.post('/machine/v1/proposals',
                                    json=self.proposal_body('prop-no-auth'))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'unauthorized')

    def test_get_without_token_is_401(self):
        response = self.client.get('/machine/v1/proposals')
        self.assertEqual(response.status_code, 401)

    def test_key_without_write_scope_cannot_submit(self):
        _row, token = machine_api.create_machine_api_key(
            'read-only', [machine_api.SCOPE_PROPOSALS_READ])
        response = self.machine_post(self.proposal_body('prop-read-only'),
                                     token=token)
        self.assertEqual(response.status_code, 403)
        body = response.get_json()
        self.assertEqual(body['error'], 'insufficient_scope')
        self.assertEqual(body['required_scope'], machine_api.SCOPE_PROPOSALS_WRITE)
        self.assertEqual(Proposal.query.count(), 0)

    def test_key_without_read_scope_cannot_list(self):
        _row, token = machine_api.create_machine_api_key(
            'write-only', [machine_api.SCOPE_PROPOSALS_WRITE])
        response = self.machine_get(token=token)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'insufficient_scope')
        self.assertEqual(response.get_json()['required_scope'],
                         machine_api.SCOPE_PROPOSALS_READ)

    def test_expired_key_is_401(self):
        _row, token = machine_api.create_machine_api_key(
            'expired', [machine_api.SCOPE_PROPOSALS_WRITE],
            expires_at=_utcnow() - timedelta(minutes=1))
        response = self.machine_post(self.proposal_body('prop-expired'), token=token)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error'], 'expired_key')

    def test_scopes_are_known_to_key_issuing(self):
        self.assertIn('proposals.read', machine_api.KNOWN_SCOPES)
        self.assertIn('proposals.write', machine_api.KNOWN_SCOPES)


class ProposalSubmissionTests(ProposalTestBase):
    def test_new_proposal_is_201_pending(self):
        response = self.machine_post(self.proposal_body('prop-1'))
        self.assertEqual(response.status_code, 201, response.data)
        body = response.get_json()
        self.assertEqual(body['proposal_id'], 'prop-1')
        self.assertEqual(body['status'], 'pending')
        self.assertTrue(response.headers.get('X-Request-ID'))
        row = Proposal.query.filter_by(proposal_id='prop-1').one()
        self.assertEqual(row.status, 'pending')
        self.assertEqual(row.created_by_key_id, self.key.id)
        self.assertEqual(row.payload, '{"text":"Your subscription expires soon."}')

    def test_optional_binding_fields_are_stored(self):
        response = self.machine_post(self.proposal_body(
            'prop-bound', type='telegram_reply_draft',
            payload={'draft': 'Hello'}, customer_id=self.customer.id,
            service_key=self.service_key, generation=37))
        self.assertEqual(response.status_code, 201, response.data)
        row = Proposal.query.filter_by(proposal_id='prop-bound').one()
        self.assertEqual(row.type, 'telegram_reply_draft')
        self.assertEqual(row.customer_id, self.customer.id)
        self.assertEqual(row.service_key, self.service_key)
        self.assertEqual(row.generation, 37)

    def test_identical_replay_is_200_and_does_not_duplicate(self):
        first = self.machine_post(self.proposal_body('prop-replay'))
        self.assertEqual(first.status_code, 201)
        second = self.machine_post(self.proposal_body('prop-replay'))
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.get_json()['proposal_id'], 'prop-replay')
        self.assertEqual(second.get_json()['status'], 'pending')
        self.assertEqual(Proposal.query.filter_by(proposal_id='prop-replay').count(), 1)

    def test_conflicting_replay_is_409_and_keeps_the_original(self):
        self.assertEqual(self.machine_post(self.proposal_body('prop-conflict')).status_code, 201)
        response = self.machine_post(self.proposal_body(
            'prop-conflict', payload={'text': 'A different suggestion'}))
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.get_json()['error'], 'proposal_conflict')
        row = Proposal.query.filter_by(proposal_id='prop-conflict').one()
        self.assertEqual(row.payload, '{"text":"Your subscription expires soon."}')

    def test_same_id_with_a_different_type_is_409(self):
        self.assertEqual(self.machine_post(self.proposal_body('prop-type')).status_code, 201)
        response = self.machine_post(self.proposal_body(
            'prop-type', type='duplicate_receipt_flag'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error'], 'proposal_conflict')

    def test_invalid_type_is_400(self):
        response = self.machine_post(self.proposal_body('prop-bad-type',
                                                        type='delete_everything'))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.get_json()['error'], 'invalid_type')
        self.assertEqual(Proposal.query.count(), 0)

    def test_missing_proposal_id_is_400(self):
        body = self.proposal_body('prop-missing')
        del body['proposal_id']
        response = self.machine_post(body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'proposal_id_required')

    def test_payload_must_be_a_json_object(self):
        response = self.machine_post(self.proposal_body('prop-not-object',
                                                        payload='just a string'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_payload')

    def test_payload_over_8kb_is_400(self):
        response = self.machine_post(self.proposal_body(
            'prop-huge', payload={'text': 'x' * (8 * 1024 + 1)}))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.get_json()['error'], 'payload_too_large')
        self.assertEqual(Proposal.query.count(), 0)

    def test_payload_at_the_limit_is_accepted(self):
        payload = {'text': 'x' * 8000}
        self.assertLessEqual(len(json.dumps(payload, sort_keys=True,
                                            separators=(',', ':'))), 8 * 1024)
        response = self.machine_post(self.proposal_body('prop-8k', payload=payload))
        self.assertEqual(response.status_code, 201, response.data)

    def test_invalid_service_key_is_400(self):
        response = self.machine_post(self.proposal_body(
            'prop-bad-service', service_key='not-a-service-key'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_service_key')

    def test_invalid_generation_is_400(self):
        response = self.machine_post(self.proposal_body(
            'prop-bad-generation', generation='later'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_generation')


class ProposalListingTests(ProposalTestBase):
    def test_listing_is_pending_only_and_newest_first(self):
        self.create('prop-old')
        self.create('prop-new')
        accepted = self.create('prop-decided')
        proposals.accept_proposal(accepted, actor=self.reviewer)

        response = self.machine_get()
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_json()
        self.assertEqual(body['schema_version'], 'eve.context.proposals.v1')
        self.assertEqual(body['status'], 'pending')
        self.assertEqual(body['count'], 2)
        self.assertEqual([row['proposal_id'] for row in body['proposals']],
                         ['prop-new', 'prop-old'])

    def test_listing_can_filter_a_decided_status(self):
        accepted = self.create('prop-accepted')
        proposals.accept_proposal(accepted, actor=self.reviewer)
        self.create('prop-still-pending')
        body = self.machine_get('?status=accepted').get_json()
        self.assertEqual(body['count'], 1)
        self.assertEqual(body['proposals'][0]['proposal_id'], 'prop-accepted')
        self.assertEqual(body['proposals'][0]['status'], 'accepted')

    def test_invalid_status_is_400(self):
        response = self.machine_get('?status=deleted')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'invalid_status')

    def test_limit_is_bounded_and_never_returns_more(self):
        for index in range(4):
            self.create('prop-limit-%d' % index)
        body = self.machine_get('?limit=2').get_json()
        self.assertEqual(body['count'], 2)

    def test_projection_carries_no_key_material_and_masks_the_decider(self):
        row = self.create('prop-masked', customer_id=self.customer.id,
                          service_key=self.service_key, generation=37)
        proposals.accept_proposal(row, actor=self.reviewer)
        response = self.machine_get('?status=accepted')
        serialized = json.dumps(response.get_json(), ensure_ascii=False)
        item = response.get_json()['proposals'][0]
        self.assertEqual(item['proposal_id'], 'prop-masked')
        self.assertEqual(item['payload'], {'text': 'Your subscription expires soon.'})
        self.assertIn('***', item['decided_by'])
        self.assertNotIn('reviewer', serialized)
        self.assertNotIn(self.key.key_hash, serialized)
        self.assertNotIn(self.token, serialized)
        self.assertNotIn('created_by_key_id', item)
        self.assertNotIn('id', item)

    def test_payload_is_returned_as_json_not_a_string(self):
        self.create('prop-json', payload={'text': 'Hello', 'priority': 2})
        item = self.machine_get().get_json()['proposals'][0]
        self.assertEqual(item['payload'], {'text': 'Hello', 'priority': 2})


class ProposalServiceTests(ProposalTestBase):
    def test_create_is_idempotent_and_reports_replay(self):
        first, created_first = proposals.create_proposal(
            'prop-service', 'reminder_text', {'text': 'same'})
        self.assertTrue(created_first)
        second, created_second = proposals.create_proposal(
            'prop-service', 'reminder_text', {'text': 'same'})
        self.assertFalse(created_second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(Proposal.query.count(), 1)

    def test_conflicting_payload_raises_conflict(self):
        proposals.create_proposal('prop-conflict-svc', 'reminder_text', {'text': 'a'})
        with self.assertRaises(proposals.ProposalError) as caught:
            proposals.create_proposal('prop-conflict-svc', 'reminder_text',
                                      {'text': 'b'})
        self.assertEqual(caught.exception.code, 'proposal_conflict')
        self.assertEqual(caught.exception.status, 409)

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(proposals.ProposalError) as caught:
            proposals.create_proposal('prop-unknown', 'nope', {'text': 'x'})
        self.assertEqual(caught.exception.code, 'invalid_type')

    def test_payload_preview_prefers_text_and_is_one_line(self):
        row = self.create('prop-preview',
                          payload={'text': 'line one\nline two', 'extra': 'x'})
        self.assertEqual(proposals.payload_preview(row), 'line one line two')

    def test_payload_preview_falls_back_to_json_and_truncates(self):
        row = self.create('prop-preview-json', payload={'a': 'b' * 400})
        preview = proposals.payload_preview(row)
        self.assertTrue(preview.startswith('{'))
        self.assertTrue(preview.endswith('...'))
        self.assertLessEqual(len(preview), proposals.PAYLOAD_PREVIEW_LENGTH + 3)


class ProposalDecisionTests(ProposalTestBase):
    def test_accept_records_the_decision_and_the_audit_entry(self):
        row = self.create('prop-accept', customer_id=self.customer.id,
                          service_key=self.service_key, generation=37)
        result = proposals.accept_proposal(row, actor=self.reviewer)
        self.assertEqual(result['status'], 'accepted')
        self.assertIsNone(result['conflict'])
        db.session.expire_all()
        stored = Proposal.query.filter_by(proposal_id='prop-accept').one()
        self.assertEqual(stored.status, 'accepted')
        self.assertEqual(stored.decided_by, 'reviewer')
        self.assertIsNotNone(stored.decided_at)
        self.assertIsNone(stored.reason)
        entry = AuditLog.query.filter_by(action='proposal.accept').one()
        self.assertEqual(entry.target_type, 'Proposal')
        self.assertEqual(entry.target_id, 'prop-accept')
        self.assertEqual(entry.actor_admin_id, self.reviewer.id)

    def test_accept_has_no_sms_or_client_side_effect(self):
        lifecycle_before = self.lifecycle.generation
        credit_before = self.customer.credit
        row = self.create('prop-side-effects', customer_id=self.customer.id,
                          service_key=self.service_key, generation=37)
        proposals.accept_proposal(row, actor=self.reviewer)
        db.session.expire_all()
        self.assertEqual(SmsSendLog.query.count(), 0)
        self.assertEqual(ServiceNotificationOutbox.query.count(), 0)
        self.assertEqual(
            db.session.get(ServiceLifecycleState, self.lifecycle.id).generation,
            lifecycle_before)
        self.assertEqual(db.session.get(CustomerAccount, self.customer.id).credit,
                         credit_before)
        self.assertEqual(
            ServiceOwnership.query.filter_by(customer_id=self.customer.id).count(), 1)

    def test_stale_generation_marks_expired_and_does_not_apply(self):
        row = self.create('prop-stale', service_key=self.service_key, generation=37)
        # The service was renewed after the proposal was written.
        self.lifecycle.generation = 38
        db.session.commit()
        result = proposals.accept_proposal(row, actor=self.reviewer)
        self.assertEqual(result['status'], 'expired')
        self.assertEqual(result['conflict'], 'generation_stale')
        db.session.expire_all()
        stored = Proposal.query.filter_by(proposal_id='prop-stale').one()
        self.assertEqual(stored.status, 'expired')
        self.assertEqual(stored.reason, 'generation_stale')
        self.assertEqual(stored.decided_by, 'reviewer')
        self.assertEqual(
            db.session.get(ServiceLifecycleState, self.lifecycle.id).generation, 38)
        self.assertEqual(SmsSendLog.query.count(), 0)
        self.assertEqual(ServiceNotificationOutbox.query.count(), 0)
        self.assertTrue(AuditLog.query.filter_by(action='proposal.expire').count())

    def test_missing_lifecycle_row_marks_expired(self):
        row = self.create('prop-missing-service', service_key=self.service_key,
                          generation=37)
        ServiceLifecycleState.query.delete()
        db.session.commit()
        result = proposals.accept_proposal(row, actor=self.reviewer)
        self.assertEqual(result['status'], 'expired')
        self.assertEqual(result['conflict'], 'service_missing')

    def test_proposal_without_service_is_accepted(self):
        row = self.create('prop-unbound')
        result = proposals.accept_proposal(row, actor=self.reviewer)
        self.assertEqual(result['status'], 'accepted')

    def test_second_decision_is_rejected_as_already_decided(self):
        row = self.create('prop-double')
        proposals.accept_proposal(row, actor=self.reviewer)
        db.session.expire_all()
        stored = Proposal.query.filter_by(proposal_id='prop-double').one()
        with self.assertRaises(proposals.ProposalError) as caught:
            proposals.reject_proposal(stored, 'too late', actor=self.reviewer)
        self.assertEqual(caught.exception.code, 'already_decided')
        self.assertEqual(caught.exception.status, 409)

    def test_reject_requires_a_reason(self):
        row = self.create('prop-reject-empty')
        with self.assertRaises(proposals.ProposalError) as caught:
            proposals.reject_proposal(row, '   ', actor=self.reviewer)
        self.assertEqual(caught.exception.code, 'reason_required')
        db.session.expire_all()
        self.assertEqual(
            Proposal.query.filter_by(proposal_id='prop-reject-empty').one().status,
            'pending')

    def test_reject_records_reason_and_audit(self):
        row = self.create('prop-reject')
        result = proposals.reject_proposal(row, 'Wrong customer', actor=self.reviewer)
        self.assertEqual(result['status'], 'rejected')
        db.session.expire_all()
        stored = Proposal.query.filter_by(proposal_id='prop-reject').one()
        self.assertEqual(stored.status, 'rejected')
        self.assertEqual(stored.reason, 'Wrong customer')
        self.assertEqual(stored.decided_by, 'reviewer')
        self.assertIsNotNone(stored.decided_at)
        entry = AuditLog.query.filter_by(action='proposal.reject').one()
        self.assertEqual(entry.target_id, 'prop-reject')


class ProposalAdminPageTests(ProposalTestBase):
    def test_anonymous_is_401(self):
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        response = self.client.get('/proposals')
        self.assertEqual(response.status_code, 401)

    def test_reseller_without_permission_is_403(self):
        self.login(self.reseller)
        self.assertEqual(self.client.get('/proposals').status_code, 403)

    def test_admin_permission_default_includes_proposals_review(self):
        self.assertTrue(permissions.has_permission(self.reviewer, 'proposals.review'))
        self.assertFalse(permissions.has_permission(self.reseller, 'proposals.review'))

    def test_pending_queue_lists_only_pending_rows_and_the_nav_entry(self):
        pending = self.create('prop-visible', customer_id=self.customer.id,
                              service_key=self.service_key, generation=37)
        decided = self.create('prop-hidden')
        proposals.accept_proposal(decided, actor=self.reviewer)
        self.login(self.reviewer)
        response = self.client.get('/proposals')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('prop-visible', html)
        self.assertNotIn('prop-hidden', html)
        self.assertIn('Ali Rezaei', html)
        self.assertIn(self.service_key, html)
        self.assertIn('Generation', html)
        self.assertIn('37', html)
        self.assertIn('/proposals', html)
        self.assertIn('Reminder text', html)

    def test_payload_preview_is_html_escaped(self):
        self.create('prop-xss', payload={'text': '<script>alert(1)</script>'})
        self.login(self.reviewer)
        html = self.client.get('/proposals').get_data(as_text=True)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)

    def test_accept_redirects_and_updates_the_row(self):
        row = self.create('prop-ui-accept', customer_id=self.customer.id,
                          service_key=self.service_key, generation=37)
        self.login(self.reviewer)
        response = self.client.post('/proposals/%d/accept' % row.id)
        self.assertEqual(response.status_code, 302)
        self.assertIn('notice=accepted', response.headers['Location'])
        db.session.expire_all()
        self.assertEqual(
            Proposal.query.filter_by(proposal_id='prop-ui-accept').one().status,
            'accepted')

    def test_accept_with_a_stale_generation_shows_a_conflict_notice(self):
        row = self.create('prop-ui-stale', service_key=self.service_key,
                          generation=37)
        self.lifecycle.generation = 42
        db.session.commit()
        self.login(self.reviewer)
        response = self.client.post('/proposals/%d/accept' % row.id)
        self.assertEqual(response.status_code, 302)
        self.assertIn('notice=expired', response.headers['Location'])
        self.assertIn('prop-ui-stale', response.headers['Location'])
        db.session.expire_all()
        self.assertEqual(
            Proposal.query.filter_by(proposal_id='prop-ui-stale').one().status,
            'expired')

    def test_reject_without_a_reason_is_400_and_keeps_pending(self):
        row = self.create('prop-ui-reject-empty')
        self.login(self.reviewer)
        response = self.client.post('/proposals/%d/reject' % row.id, data={})
        self.assertEqual(response.status_code, 400)
        db.session.expire_all()
        self.assertEqual(
            Proposal.query.filter_by(proposal_id='prop-ui-reject-empty').one().status,
            'pending')

    def test_reject_with_a_reason_records_it(self):
        row = self.create('prop-ui-reject')
        self.login(self.reviewer)
        response = self.client.post('/proposals/%d/reject' % row.id,
                                    data={'reason': 'Customer already renewed'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('notice=rejected', response.headers['Location'])
        db.session.expire_all()
        stored = Proposal.query.filter_by(proposal_id='prop-ui-reject').one()
        self.assertEqual(stored.status, 'rejected')
        self.assertEqual(stored.reason, 'Customer already renewed')

    def test_reseller_cannot_decide(self):
        row = self.create('prop-ui-forbidden')
        self.login(self.reseller)
        response = self.client.post('/proposals/%d/accept' % row.id)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            Proposal.query.filter_by(proposal_id='prop-ui-forbidden').one().status,
            'pending')

    def test_unknown_proposal_is_404(self):
        self.login(self.reviewer)
        self.assertEqual(self.client.post('/proposals/999999/accept').status_code, 404)

    def test_persian_panel_renders_the_queue(self):
        from app import PANEL_UI_LANG_SETTING_KEY, _set_system_setting_value
        _set_system_setting_value(PANEL_UI_LANG_SETTING_KEY, 'fa')
        db.session.commit()
        self.addCleanup(self._restore_english)
        self.create('prop-fa', payload={'text': 'یادآوری تمدید'})
        self.login(self.reviewer)
        response = self.client.get('/proposals')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('پیشنهادها', html)
        self.assertIn('یادآوری تمدید', html)

    @staticmethod
    def _restore_english():
        from app import PANEL_UI_LANG_SETTING_KEY, _set_system_setting_value
        _set_system_setting_value(PANEL_UI_LANG_SETTING_KEY, 'en')
        db.session.commit()


if __name__ == '__main__':
    unittest.main()
