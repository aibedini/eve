"""The SMS Center follows the panel language everywhere: English or Persian, never a mix."""
import base64
import os
import re
import tempfile
import unittest
from pathlib import Path

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault('SERVER_PASSWORD_KEY',
                      base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from panel.core.sms_center_copy import SMS_CENTER_COPY, sms_center_copy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PERSIAN = re.compile('[؀-ۿ]')
PLACEHOLDER = re.compile(r'\{(\w+)\}')
# Values that are legitimately language-neutral (a template of placeholders only).
NEUTRAL_FA = {'today.window', 'health.unavailable_probe'}
# Every reason code the evaluator, the scan or the send logs can record.
REASON_CODES = (
    'cooldown_active', 'daily_limit_reached', 'hourly_limit_reached', 'rate_limited',
    'quiet_hours', 'sms_disabled', 'gateway_not_ready', 'no_recipient', 'opted_out_recheck',
    'manual_review_pending', 'obligation_outstanding', 'no_template', 'empty_message',
    'reseller_owned', 'unlimited_skipped', 'expired_too_old', 'ended_too_old',
    'trigger_disabled_by_operator', 'evaluation_failed', 'state_changed_recheck',
    'account_missing_recheck', 'generation_changed_recheck', 'generation_unknown_recheck',
    'snapshot_predates_lifecycle_change', 'snapshot_time_unknown_recheck',
    'snapshot_missing_recheck',
)
DECISIONS = ('eligible_now', 'deferred', 'active_obligation', 'invalid_recipient', 'suppressed')


class SmsCenterCopyTests(unittest.TestCase):
    def test_both_languages_have_identical_keys_and_placeholders(self):
        en, fa = SMS_CENTER_COPY['en'], SMS_CENTER_COPY['fa']
        self.assertEqual(sorted(set(en) ^ set(fa)), [])
        for key in en:
            self.assertEqual(set(PLACEHOLDER.findall(en[key])), set(PLACEHOLDER.findall(fa[key])), key)

    def test_persian_is_persian_and_english_has_no_persian(self):
        for key, value in SMS_CENTER_COPY['fa'].items():
            self.assertTrue(value.strip(), key)
            if key not in NEUTRAL_FA:
                self.assertRegex(value, PERSIAN, 'Persian text expected for %s' % key)
        for key, value in SMS_CENTER_COPY['en'].items():
            self.assertTrue(value.strip(), key)
            self.assertNotRegex(value, PERSIAN, 'English text expected for %s' % key)

    def test_only_persian_selects_the_persian_table(self):
        self.assertIs(sms_center_copy('fa'), SMS_CENTER_COPY['fa'])
        for other in ('en', '', None, 'de'):
            self.assertIs(sms_center_copy(other), SMS_CENTER_COPY['en'])

    def test_every_reason_and_decision_explains_itself(self):
        for language, table in SMS_CENTER_COPY.items():
            for code in REASON_CODES:
                for suffix in ('', '.what', '.fix'):
                    self.assertIn('reason.%s%s' % (code, suffix), table, language)
            for decision in DECISIONS:
                self.assertIn('decision.%s' % decision, table, language)
                self.assertIn('decision.%s.help' % decision, table, language)
                self.assertIn('status.%s' % decision, table, language)
            for key in ('funnel.ready', 'funnel.waiting', 'funnel.pending', 'funnel.nomobile', 'funnel.blocked'):
                self.assertIn(key, table, language)
                self.assertIn(key + '.help', table, language)
            for key in ('delivered', 'failed', 'pending'):
                self.assertIn('carrier.%s' % key, table, language)
                self.assertIn('carrier.%s.help' % key, table, language)
            for state in ('sms_disabled', 'gateway_not_ready', 'quiet_hours'):
                self.assertIn('pause.%s' % state, table, language)

    def test_every_key_the_page_uses_exists(self):
        script = (ROOT / 'static' / 'sms-center.js').read_text(encoding='utf-8')
        template = (ROOT / 'templates' / 'sms_center.html').read_text(encoding='utf-8')
        used = set(re.findall(r'\bT\("([\w.]+)"', script))
        used |= set(re.findall(r'(?:\bt|sms_copy)\[\'([\w.]+)\'\]', template))
        used |= set(re.findall(r'\[\s*"(diag\.[\w]+)"', script))
        used |= set(re.findall(r'legend: "([\w.]+)"', script))
        used |= {key + '.help' for key in re.findall(r'legend: "([\w.]+)"', script)}
        self.assertGreater(len(used), 100)
        for language, table in SMS_CENTER_COPY.items():
            self.assertEqual(sorted(used - set(table)), [], language)
        # Dynamic lookups in the template, e.g. t['decision.' ~ key].
        for key in ('near_expiry', 'low_volume', 'expired', 'ended'):
            self.assertIn('state.%s' % key, SMS_CENTER_COPY['en'])
        for key in ('queued', 'sent', 'failed', 'skipped', 'delivered'):
            self.assertIn('status.%s' % key, SMS_CENTER_COPY['en'])

    def test_script_has_no_hardcoded_visible_english(self):
        script = (ROOT / 'static' / 'sms-center.js').read_text(encoding='utf-8')
        for fragment in ('"Unavailable', '"Loading', '"No matching', '"Previous"', '"Next"',
                         '"Today"', '"Yesterday"', '"Account"', '"Server"', '"Reason"'):
            self.assertNotIn(fragment, script, fragment)


class SmsCenterPageLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import app
        from panel.extensions import db
        cls.app = app
        cls.db = db
        cls.context = app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.context.pop()

    def _page(self, language):
        from app import PANEL_UI_LANG_SETTING_KEY, _set_system_setting_value
        from panel.models import Admin
        _set_system_setting_value(PANEL_UI_LANG_SETTING_KEY, language)
        self.db.session.commit()
        admin = Admin.query.filter_by(username='sms-lang-admin').first()
        if admin is None:
            admin = Admin(username='sms-lang-admin', password_hash='x', role='superadmin',
                          is_superadmin=True, enabled=True)
            self.db.session.add(admin)
            self.db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session['admin_id'] = admin.id
            browser_session['admin_username'] = admin.username
            browser_session['role'] = admin.role
            browser_session['is_superadmin'] = True
        response = client.get('/sms-center')
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def tearDown(self):
        from app import PANEL_UI_LANG_SETTING_KEY, _set_system_setting_value
        _set_system_setting_value(PANEL_UI_LANG_SETTING_KEY, 'en')
        self.db.session.commit()

    def test_persian_panel_renders_the_page_and_the_sidebar_in_persian(self):
        html = self._page('fa')
        self.assertIn('dir="rtl"', html)
        self.assertIn('مرکز پیامک', html)
        self.assertIn('همین الان: چه کسانی می‌توانند یادآوری بگیرند؟', html)
        self.assertIn('window.SMS_COPY', html)
        self.assertIn('<span>داشبورد</span>', html)
        self.assertNotIn('Today at a glance', html)
        self.assertNotIn('Eligibility, gateway acceptance', html)
        self.assertNotIn('>Dashboard</span>', html)

    def test_english_panel_renders_everything_in_english(self):
        html = self._page('en')
        self.assertIn('dir="ltr"', html)
        self.assertIn('Right now: who can receive a reminder?', html)
        self.assertIn('>Dashboard</span>', html)
        self.assertNotRegex(re.sub(r'<script.*?</script>', '', html, flags=re.S)
                            .split('<main class="sms-center', 1)[1], PERSIAN)


if __name__ == '__main__':
    unittest.main()
