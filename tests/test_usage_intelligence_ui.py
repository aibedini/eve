"""The subscription page shows why a package was recommended (RFP sections 26-27).

The real template is rendered with a real v5 payload: the four evidence rows, the
behaviour-change percentage and the explanation sentence in the selected language - and the
legacy v4 payload still renders the line it always did.
"""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from flask import render_template  # noqa: E402

from app import RenewalEvent, Server, UsageCounterState, UsageDaily, app, db  # noqa: E402
from panel.services.usage_intelligence import record_verified_renewal  # noqa: E402
from panel.services.usage_intelligence.recommendation import build_recommendation_v5  # noqa: E402

GB = 1024 ** 3
PACKAGES = [
    {'id': 1, 'name': 'standard', 'days': 30, 'volume': 60, 'price': 200},
    {'id': 2, 'name': 'plus', 'days': 30, 'volume': 120, 'price': 350},
    {'id': 3, 'name': 'max', 'days': 30, 'volume': 200, 'price': 500},
]


class SubscriptionRecommendationRenderTests(unittest.TestCase):
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

    def setUp(self):
        RenewalEvent.query.delete()
        UsageDaily.query.delete()
        UsageCounterState.query.delete()
        Server.query.delete()
        db.session.commit()
        self.server = Server(name='ui', host='https://ui.invalid', username='u',
                             password='p', panel_type='auto', enabled=True)
        db.session.add(self.server)
        db.session.commit()

    def _seed(self, sub_id='ui-account'):
        renewed_at = datetime.utcnow() - timedelta(days=8)

        def to_ms(value):
            return int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)

        record_verified_renewal(
            server_id=self.server.id, sub_id=sub_id, operation_id='op-ui', days=30,
            previous_volume_limit_bytes=50 * GB, new_volume_limit_bytes=100 * GB,
            previous_remaining_bytes=20 * GB, granted_volume_bytes=50 * GB,
            previous_expiry_ms=to_ms(renewed_at),
            new_expiry_ms=to_ms(renewed_at + timedelta(days=30)),
            renewed_at=renewed_at)
        for offset in range(31):
            observed = datetime.utcnow() - timedelta(days=offset)
            used = int((3.75 if offset < 8 else 1.3) * GB)
            db.session.add(UsageDaily(
                server_id=self.server.id, sub_id=sub_id,
                usage_date=date.today() - timedelta(days=offset),
                upload_bytes=0, download_bytes=used,
                opening_upload_bytes=0, opening_download_bytes=0,
                closing_upload_bytes=0, closing_download_bytes=used,
                sample_count=1, first_observed_at=observed,
                last_observed_at=observed))
        db.session.add(UsageCounterState(
            server_id=self.server.id, sub_id=sub_id, upload_bytes=0,
            download_bytes=60 * GB, total_bytes=60 * GB,
            observed_at=datetime.utcnow()))
        db.session.commit()
        return sub_id

    def _v5_payload(self):
        sub_id = self._seed()
        return build_recommendation_v5(
            self.server.id, sub_id, PACKAGES,
            live_usage={'total_bytes': 60 * GB, 'observed_at': datetime.utcnow()})

    def _render(self, payload, *, lang='fa'):
        # A minimal but complete context: the page reads these client fields and iterates
        # the lists, so they must exist to render the recommendation block for real.
        client = {
            'email': 'ui-account', 'expiry': '', 'expiry_days': 0, 'expiry_type': 'days',
            'is_active': True, 'percentage_used': 50, 'remaining': '30 GB',
            'total_limit': '100 GB', 'total_used': '60 GB', 'configs': [],
            'last_ip': '', 'last_ip_operator': '', 'server_name': self.server.name,
            'service_state_emoji': '', 'service_state_label': 'Active',
            'service_state_tag': 'active', 'subscription_url': '',
        }
        with app.test_request_context('/s/%d/ui-account' % self.server.id):
            return render_template(
                'subscription.html', client=client, apps=[], faqs=[], support={},
                channels={}, announcements=[], active_online_chat_script=None,
                backup_configs=[], sub_packages=list(PACKAGES),
                renewal_recommendation=payload,
                page_lang=lang, server_id=self.server.id, sub_id='ui-account',
                server={'id': self.server.id, 'name': self.server.name},
                sse_enabled=False, csp_nonce='test-nonce')

    def test_the_page_shows_the_evidence_behind_the_recommendation(self):
        payload = self._v5_payload()
        self.assertIsNotNone(payload)
        html = self._render(payload, lang='fa')
        self.assertIn('pkg-usage-evidence', html)
        self.assertIn('مصرف از آخرین تمدید', html)
        self.assertIn('میانگین ۳۱ روز گذشته', html)
        self.assertIn('تغییر رفتار', html)
        self.assertIn('پیش‌بینی مصرف', html)
        # The numbers are the measured ones: ~3.7 GB/day in the cycle against ~1.9.
        self.assertIn('%s GB' % payload['current_cycle']['average_daily_gb'], html)
        self.assertIn('%s GB' % payload['rolling_31d']['average_daily_gb'], html)
        self.assertIn('+%d%%' % payload['trend']['change_percent'], html)
        self.assertIn('%s GB' % payload['forecast']['projected_31d_gb'], html)

    def test_the_explanation_is_language_aware(self):
        payload = self._v5_payload()
        fa = self._render(payload, lang='fa')
        en = self._render(payload, lang='en')
        self.assertIn(payload['explanation']['fa'], fa)
        self.assertNotIn(payload['explanation']['en'], fa)
        self.assertIn(payload['explanation']['en'], en)
        self.assertIn('Usage since the last renewal', en)
        self.assertIn('Forecast', en)

    def test_a_v4_payload_still_renders_the_legacy_line(self):
        v4 = {
            'model_version': 'usage-fit-v4', 'package_id': 2, 'package_name': 'plus',
            'package_volume': 120, 'package_days': 30, 'package_price': 350,
            'average_daily_gb': 1.94, 'projected_31d_gb': 60.1, 'basis_days': 31.0,
            'covered_days': 31, 'confidence': 'high', 'confidence_label': 'high',
            'source': 'last_31_days', 'fast_cycle': False, 'capacity_limited': False,
            'safety_margin_percent': 10, 'buffered_requirement_gb': 66.1,
            'comfort_package_id': None,
        }
        html = self._render(v4, lang='en')
        # The v5 evidence rows are absent (only the stylesheet rule mentions them).
        self.assertNotIn('<div class="pkg-usage-evidence">', html)
        self.assertIn('Your average daily usage is 1.94 GB', html)
        self.assertIn('high confidence', html)

    def test_the_rendered_recommendation_carries_no_identifier(self):
        payload = self._v5_payload()
        html = self._render(payload, lang='en')
        start = html.index('<div class="pkg-usage-evidence">')
        note_start = html.index('<span class="pkg-usage-note">')
        end = html.index('</span>', note_start) + len('</span>')
        block = html[start:end]
        self.assertIn('pkg-usage-note', block)
        self.assertNotIn('@', block)
        self.assertNotIn('ui-account', block)
        self.assertNotIn('uuid', block.lower())


if __name__ == '__main__':
    unittest.main()
