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

from app import Admin, Announcement, AnnouncementDelivery, Server, SmsSendLog, app, db  # noqa: E402
from panel.core.redis_client import GLOBAL_SERVER_DATA  # noqa: E402
from panel.jobs.messaging import (  # noqa: E402
    _announcement_campaign_recipients,
    _queue_announcement_campaign,
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
        self.assertIn("['remaining_volume', 'Remaining volume']", html)

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
        send_sms.return_value = {'sent': True, 'reason': None}
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
