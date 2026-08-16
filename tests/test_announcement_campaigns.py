import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'

from app import (  # noqa: E402
    Admin, Announcement, AnnouncementDelivery, ClientOwnership, Server, SmsSendLog,
    app, db,
)
from panel.core.redis_client import GLOBAL_SERVER_DATA  # noqa: E402
from panel.jobs.messaging import (  # noqa: E402
    _announcement_campaign_recipients,
    _estimate_sms_campaign_duration,
    _queue_announcement_campaign,
    _refresh_pending_sms_statuses,
    _run_announcement_campaign_batch,
)
from panel.routes.content import _parse_announcement_payload  # noqa: E402


class AnnouncementCampaignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()
        try:
            os.unlink(_DB_FILE.name)
        except OSError:
            pass

    def setUp(self):
        self.previous_inbounds = GLOBAL_SERVER_DATA.get('inbounds')
        self.admin = Admin(username='campaign-admin', role='superadmin', enabled=True, is_superadmin=True)
        self.admin.set_password('StrongPassword123!')
        self.server = Server(name='Campaign server', host='https://campaign.test', username='u', password='p')
        db.session.add_all([self.admin, self.server])
        db.session.commit()
        GLOBAL_SERVER_DATA['inbounds'] = [{
            'id': 11, 'server_id': self.server.id, 'server_name': self.server.name,
            'clients': [
                {'email': 'first', 'comment': '09121234567', 'expiryTimestamp': 0,
                 'remaining_formatted': '10 GB', 'subId': 'sub-a'},
                {'email': 'duplicate', 'comment': '+989121234567', 'expiryTimestamp': 0,
                 'remaining_formatted': '8 GB', 'subId': 'sub-b'},
                {'email': 'optout', 'comment': '09120000000 #nosms', 'expiryTimestamp': 0,
                 'remaining_formatted': '4 GB', 'subId': 'sub-c'},
                {'email': 'missing', 'comment': '', 'expiryTimestamp': 0,
                 'remaining_formatted': '2 GB', 'subId': 'sub-d'},
            ],
        }]

    def tearDown(self):
        GLOBAL_SERVER_DATA['inbounds'] = self.previous_inbounds or []
        db.session.rollback()
        AnnouncementDelivery.query.delete()
        Announcement.query.delete()
        SmsSendLog.query.delete()
        ClientOwnership.query.delete()
        Server.query.delete()
        Admin.query.delete()
        db.session.commit()

    def test_outbound_payload_does_not_require_subscription_window(self):
        payload, error = _parse_announcement_payload({
            'channel': 'sms', 'message': 'Hello {account_name}', 'targets': '*',
            'delivery_mode': 'daily', 'daily_limit': 25,
        })
        self.assertIsNone(error)
        self.assertEqual('sms', payload['channel'])
        self.assertEqual('daily', payload['delivery_mode'])
        self.assertEqual(25, payload['daily_limit'])
        self.assertGreater(payload['end_at'], payload['start_at'])

    def test_sub_manager_renders_campaign_controls(self):
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = self.admin.id
            session_data['admin_username'] = self.admin.username
            session_data['role'] = 'superadmin'
            session_data['is_superadmin'] = True
        response = client.get('/sub-manager')
        self.assertEqual(200, response.status_code)
        html = response.get_data(as_text=True)
        self.assertIn('id="announcement-channel"', html)
        self.assertIn('id="announcement-recipient-preview"', html)
        self.assertIn('id="announcement-count-recipients"', html)
        self.assertIn('annRenderRecipientPreview(data)', html)
        self.assertIn('class="ann-preview-table"', html)
        self.assertIn('Excluded or cleaned up', html)
        self.assertIn('Estimated duration', html)
        self.assertIn('id="announcement-owner-filters"', html)
        self.assertIn('id="announcement-status-filters"', html)
        self.assertIn('id="announcement-failures-modal"', html)
        self.assertIn('resendSelectedAnnouncementFailures', html)
        self.assertIn("['remaining_volume', 'Remaining volume']", html)

    def test_sms_eta_uses_daily_segment_cap_and_send_pace(self):
        estimate = _estimate_sms_campaign_duration(
            450, 450, cfg={
                'daily_limit': 200, 'send_pace_seconds': 3,
                'quiet_enabled': False,
            })
        self.assertEqual(200, estimate['recipients_per_day'])
        self.assertEqual('SMS daily segment limit', estimate['bottleneck'])
        self.assertGreater(estimate['seconds'], 2 * 86400)

    @mock.patch('panel.jobs.messaging.load_snapshot_from_redis')
    def test_sms_preview_deduplicates_numbers_and_honors_opt_out(self, load_snapshot):
        recipients, stats = _announcement_campaign_recipients('sms', '*')
        self.assertEqual(1, len(recipients))
        self.assertEqual('+989121234567', recipients[0]['recipient'])
        self.assertEqual(1, stats['duplicates'])
        self.assertEqual(1, stats['opted_out'])
        self.assertEqual(1, stats['missing_contact'])

    @mock.patch('panel.jobs.messaging.load_snapshot_from_redis')
    def test_preview_api_estimates_rendered_sms_segments(self, load_snapshot):
        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = self.admin.id
            session_data['role'] = 'superadmin'
            session_data['is_superadmin'] = True
        response = client.post('/api/announcements/preview', json={
            'channel': 'sms', 'targets': '*', 'message': 'Hello {account_name}',
        })
        data = response.get_json()
        self.assertEqual(200, response.status_code)
        self.assertTrue(data['success'])
        self.assertEqual(1, data['unique'])
        self.assertEqual(1, data['sms']['estimated_total_segments'])
        self.assertGreater(data['eta']['seconds'], 0)

    @mock.patch('panel.jobs.messaging.load_snapshot_from_redis')
    def test_sms_audience_filters_owner_and_multiselect_state(self, load_snapshot):
        reseller = Admin(username='reseller-owner', role='reseller', enabled=True)
        reseller.set_password('StrongPassword123!')
        db.session.add(reseller)
        db.session.flush()
        db.session.add(ClientOwnership(
            reseller_id=reseller.id, server_id=self.server.id, inbound_id=11,
            client_email='first',
        ))
        db.session.commit()
        GLOBAL_SERVER_DATA['inbounds'][0]['clients'][0]['service_state'] = 'expired'
        GLOBAL_SERVER_DATA['inbounds'][0]['clients'][1]['service_state'] = 'volume_low'

        recipients, stats = _announcement_campaign_recipients(
            'sms', '*', ['reseller'], ['expired'])

        self.assertEqual(['first'], [row['email'] for row in recipients])
        self.assertGreaterEqual(stats['owner_filtered'], 1)
        self.assertEqual('reseller', recipients[0]['context']['owner_type'])

    @mock.patch('panel.jobs.messaging.load_snapshot_from_redis')
    def test_edit_running_campaign_keeps_sent_rows_and_adds_only_new_recipient(self, load_snapshot):
        row = Announcement(
            message='Old {account_name}', channel='sms', targets='*', all_servers=True,
            start_at=datetime.utcnow(), end_at=datetime.utcnow() + timedelta(days=1),
            delivery_mode='all', status='completed', total_count=1, sent_count=1,
            created_by='campaign-admin',
        )
        db.session.add(row)
        db.session.flush()
        db.session.add(AnnouncementDelivery(
            announcement_id=row.id, recipient_key='sms:+989121234567',
            recipient='+989121234567', email='first', server_id=self.server.id,
            inbound_id=11, context_json='{"account_name":"first"}', status='sent',
            sent_at=datetime.utcnow(), processed_at=datetime.utcnow(),
        ))
        GLOBAL_SERVER_DATA['inbounds'][0]['clients'].append({
            'email': 'new-user', 'comment': '09123334455', 'expiryTimestamp': 0,
            'remaining_formatted': '6 GB', 'service_state': 'active', 'subId': 'sub-new',
        })
        db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = self.admin.id
            session_data['role'] = 'superadmin'
            session_data['is_superadmin'] = True
        response = client.put(f'/api/announcements/{row.id}', json={
            'channel': 'sms', 'message': 'Edited {account_name}', 'targets': '*',
            'delivery_mode': 'all',
            'audience_owner_types': ['system', 'unowned'],
            'audience_statuses': ['other'],
        })

        self.assertEqual(200, response.status_code)
        db.session.refresh(row)
        deliveries = AnnouncementDelivery.query.filter_by(announcement_id=row.id).all()
        self.assertEqual(2, len(deliveries))
        self.assertEqual(1, sum(delivery.status == 'sent' for delivery in deliveries))
        self.assertEqual(1, sum(delivery.status == 'pending' for delivery in deliveries))
        self.assertEqual('queued', row.status)
        self.assertEqual('Edited {account_name}', row.message)

    @mock.patch('panel.jobs.messaging.load_snapshot_from_redis')
    @mock.patch('panel.jobs.messaging._get_sms_runtime_settings')
    def test_queue_materializes_unique_delivery_rows(self, runtime, load_snapshot):
        runtime.return_value = {'enabled': True, 'base_url': 'https://sms.test', 'api_key': 'secret'}
        row = Announcement(
            message='Hello {account_name}', channel='sms', targets='*', all_servers=True,
            start_at=datetime.utcnow(), end_at=datetime.utcnow() + timedelta(days=1),
            delivery_mode='all', status='draft', created_by='campaign-admin',
        )
        db.session.add(row)
        db.session.commit()
        _queue_announcement_campaign(row)
        db.session.commit()
        self.assertEqual('queued', row.status)
        self.assertEqual(1, row.total_count)
        self.assertEqual(1, AnnouncementDelivery.query.filter_by(announcement_id=row.id).count())

    @mock.patch('panel.jobs.messaging._sms_account_opted_out', return_value=False)
    @mock.patch('panel.jobs.messaging._sms_in_quiet_hours', return_value=False)
    @mock.patch('panel.jobs.messaging._get_sms_runtime_settings', return_value={'enabled': True})
    @mock.patch('panel.jobs.messaging._send_sms_via_gmweb')
    def test_worker_renders_personal_variables_and_finishes(self, send_sms, runtime, quiet, opted_out):
        send_sms.return_value = {
            'sent': True, 'reason': None, 'request_id': 'request-1', 'status': 'queued',
        }
        row = Announcement(
            message='Hello {account_name} on {server_name}', channel='sms', targets='*', all_servers=True,
            start_at=datetime.utcnow(), end_at=datetime.utcnow() + timedelta(days=1),
            delivery_mode='all', status='queued', total_count=1, created_by='campaign-admin',
        )
        db.session.add(row)
        db.session.flush()
        db.session.add(AnnouncementDelivery(
            announcement_id=row.id, recipient_key='sms:989121234567', recipient='989121234567',
            email='first', server_id=self.server.id, inbound_id=11,
            context_json='{"account_name":"first","server_name":"Campaign server"}',
        ))
        db.session.commit()

        self.assertEqual(1, _run_announcement_campaign_batch())
        db.session.refresh(row)
        self.assertEqual('completed', row.status)
        self.assertEqual(1, row.sent_count)
        self.assertEqual('Hello first on Campaign server', send_sms.call_args.args[1])
        delivery = AnnouncementDelivery.query.filter_by(announcement_id=row.id).one()
        self.assertEqual('request-1', delivery.gateway_request_id)
        self.assertTrue(send_sms.call_args.kwargs['idempotency_key'].endswith('-r0'))

    def test_failed_report_and_selected_resend_preserve_sent_rows(self):
        row = Announcement(
            message='Retry me', channel='sms', targets='*', all_servers=True,
            start_at=datetime.utcnow(), end_at=datetime.utcnow() + timedelta(days=1),
            delivery_mode='all', status='completed', total_count=3, sent_count=1,
            failed_count=2, created_by='campaign-admin',
        )
        db.session.add(row)
        db.session.flush()
        sent = AnnouncementDelivery(
            announcement_id=row.id, recipient_key='sms:+989120000001',
            recipient='+989120000001', email='sent', status='sent',
            context_json='{"account_name":"sent"}', sent_at=datetime.utcnow(),
            processed_at=datetime.utcnow(),
        )
        failed_gmweb = AnnouncementDelivery(
            announcement_id=row.id, recipient_key='sms:+989120000002',
            recipient='+989120000002', email='gateway-user', status='failed', attempts=5,
            context_json='{"account_name":"gateway-user","server_name":"Campaign server"}',
            last_error='provider rejected destination', last_error_source='gmweb',
            gateway_request_id='failed-request', gateway_state='failed',
            processed_at=datetime.utcnow(),
        )
        failed_panel = AnnouncementDelivery(
            announcement_id=row.id, recipient_key='sms:+989120000003',
            recipient='+989120000003', email='panel-user', status='failed', attempts=5,
            context_json='{"account_name":"panel-user"}',
            last_error='connection timeout', last_error_source='panel',
            processed_at=datetime.utcnow(),
        )
        db.session.add_all([sent, failed_gmweb, failed_panel])
        db.session.commit()

        client = app.test_client()
        with client.session_transaction() as session_data:
            session_data['admin_id'] = self.admin.id
            session_data['role'] = 'superadmin'
            session_data['is_superadmin'] = True

        report = client.get(f'/api/announcements/{row.id}/failures').get_json()
        self.assertEqual(2, report['total'])
        by_email = {item['email']: item for item in report['failures']}
        self.assertEqual('gmweb', by_email['gateway-user']['source'])
        self.assertEqual('provider rejected destination', by_email['gateway-user']['reason'])
        self.assertEqual('panel', by_email['panel-user']['source'])

        response = client.post(
            f'/api/announcements/{row.id}/failures/resend',
            json={'delivery_ids': [failed_gmweb.id]},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.get_json()['resent_count'])
        db.session.refresh(row)
        db.session.refresh(sent)
        db.session.refresh(failed_gmweb)
        db.session.refresh(failed_panel)
        self.assertEqual('sent', sent.status)
        self.assertEqual('retry', failed_gmweb.status)
        self.assertEqual(1, failed_gmweb.resend_count)
        self.assertIsNone(failed_gmweb.last_error)
        self.assertIsNone(failed_gmweb.gateway_request_id)
        self.assertEqual('failed', failed_panel.status)
        self.assertEqual('queued', row.status)

        bulk_response = client.post(
            f'/api/announcements/{row.id}/failures/resend', json={})
        self.assertEqual(200, bulk_response.status_code)
        self.assertEqual(1, bulk_response.get_json()['resent_count'])
        db.session.refresh(sent)
        db.session.refresh(failed_panel)
        self.assertEqual('sent', sent.status)
        self.assertEqual('retry', failed_panel.status)
        self.assertEqual(1, failed_panel.resend_count)

    @mock.patch('panel.jobs.messaging.requests.get')
    @mock.patch('panel.jobs.messaging._get_sms_runtime_settings')
    def test_terminal_gmweb_failure_updates_campaign_delivery(self, runtime, get_status):
        runtime.return_value = {
            'base_url': 'https://sms.test', 'api_key': 'secret', 'timeout_seconds': 5,
        }
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            'status': 'failed', 'state': 'failed', 'stage': 'provider',
            'terminal': True, 'successful': False,
            'failedReason': 'operator rejected message',
        }
        get_status.return_value = response
        row = Announcement(
            message='Hello', channel='sms', targets='*', all_servers=True,
            start_at=datetime.utcnow(), end_at=datetime.utcnow() + timedelta(days=1),
            delivery_mode='all', status='completed', total_count=1, sent_count=1,
            created_by='campaign-admin',
        )
        db.session.add(row)
        db.session.flush()
        delivery = AnnouncementDelivery(
            announcement_id=row.id, recipient_key='sms:+989120000004',
            recipient='+989120000004', email='later-failed', status='sent',
            context_json='{}', gateway_request_id='request-later-failed',
            sent_at=datetime.utcnow(), processed_at=datetime.utcnow(),
        )
        log = SmsSendLog(
            email='later-failed', server_id=0, state='announcement',
            recipient='0912***0004', status='queued', request_id='request-later-failed',
            terminal=False,
        )
        db.session.add_all([delivery, log])
        db.session.commit()

        self.assertEqual(1, _refresh_pending_sms_statuses())
        db.session.refresh(row)
        db.session.refresh(delivery)
        self.assertEqual('failed', delivery.status)
        self.assertEqual('operator rejected message', delivery.last_error)
        self.assertEqual('gmweb', delivery.last_error_source)
        self.assertEqual(1, row.failed_count)
        self.assertEqual(0, row.sent_count)

    @mock.patch('panel.jobs.messaging._sms_account_opted_out', return_value=False)
    @mock.patch('panel.jobs.messaging._sms_in_quiet_hours', return_value=False)
    @mock.patch('panel.jobs.messaging._get_sms_runtime_settings', return_value={'enabled': True})
    @mock.patch('panel.jobs.messaging._send_sms_via_gmweb', return_value={'sent': True, 'reason': None})
    def test_daily_mode_stops_at_campaign_recipient_limit(self, send_sms, runtime, quiet, opted_out):
        row = Announcement(
            message='Hello {account_name}', channel='sms', targets='*', all_servers=True,
            start_at=datetime.utcnow(), end_at=datetime.utcnow() + timedelta(days=1),
            delivery_mode='daily', daily_limit=1, status='queued', total_count=2,
            created_by='campaign-admin',
        )
        db.session.add(row)
        db.session.flush()
        for index in (1, 2):
            db.session.add(AnnouncementDelivery(
                announcement_id=row.id, recipient_key=f'sms:+98912000000{index}',
                recipient=f'+98912000000{index}', email=f'user-{index}', server_id=self.server.id,
                inbound_id=11, context_json=f'{{"account_name":"user-{index}"}}',
            ))
        db.session.commit()

        self.assertEqual(1, _run_announcement_campaign_batch(batch_size=25))
        self.assertEqual(1, send_sms.call_count)
        self.assertEqual(1, AnnouncementDelivery.query.filter_by(
            announcement_id=row.id, status='pending').count())
        self.assertEqual(0, _run_announcement_campaign_batch(batch_size=25))
        db.session.refresh(row)
        self.assertEqual('queued', row.status)


if __name__ == '__main__':
    unittest.main()
