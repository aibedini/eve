"""Subscription page templates and block order (Sub Manager -> Page Template)."""
import base64
import json
import os
import re
import tempfile
import unittest

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault('SERVER_PASSWORD_KEY',
                      base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from panel.services import subscription_layout as layout  # noqa: E402

BLOCK_RE = re.compile(r'data-sub-block="(\w+)"( hidden)?')


class NormalizeTests(unittest.TestCase):
    def test_unknown_duplicate_and_missing_blocks_are_repaired(self):
        blocks = layout.normalize_blocks([
            {'id': 'faq', 'visible': False}, 'usage', {'id': 'faq'}, {'id': 'evil'}, 42,
        ])
        self.assertEqual([b['id'] for b in blocks][:2], ['faq', 'usage'])
        self.assertEqual(sorted(b['id'] for b in blocks), sorted(layout.BLOCKS))
        self.assertFalse(blocks[0]['visible'])

    def test_required_blocks_cannot_be_hidden(self):
        blocks = layout.normalize_blocks([{'id': block, 'visible': False} for block in layout.BLOCKS])
        visible = {b['id'] for b in blocks if b['visible']}
        self.assertEqual(visible, set(layout.REQUIRED_BLOCKS))

    def test_bad_stored_value_falls_back_to_the_default(self):
        parsed = layout.parse_layout('{not json')
        self.assertEqual(parsed['template'], layout.DEFAULT_TEMPLATE)
        self.assertEqual(set(parsed['layouts']), set(layout.TEMPLATES))
        self.assertEqual(layout.parse_layout(json.dumps({'template': 'nope'}))['template'],
                         layout.DEFAULT_TEMPLATE)

    def test_each_template_keeps_its_own_order(self):
        data = layout.normalize_layout({'template': 'glass', 'layouts': {
            'glass': ['config'], 'compact': ['history']}})
        self.assertEqual(data['layouts']['glass'][0]['id'], 'config')
        self.assertEqual(data['layouts']['compact'][0]['id'], 'history')
        self.assertEqual(layout.page_layout(data)['template'], 'glass')
        self.assertEqual(layout.page_layout(data)['blocks'][0]['id'], 'config')


class LayoutRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import app
        from panel.extensions import db
        from panel.models import Admin
        cls.app, cls.db = app, db
        cls.context = app.app_context()
        cls.context.push()
        db.create_all()
        admin = Admin.query.filter_by(username='layout-admin').first()
        if admin is None:
            admin = Admin(username='layout-admin', password_hash='x', role='superadmin',
                          is_superadmin=True, enabled=True)
            db.session.add(admin)
            db.session.commit()
        cls.admin_id = admin.id

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.context.pop()

    def setUp(self):
        from panel.models import SystemConfig
        SystemConfig.query.filter_by(key=layout.LAYOUT_KEY).delete()
        self.db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as browser_session:
            browser_session.update(admin_id=self.admin_id, admin_username='layout-admin',
                                   role='superadmin', is_superadmin=True)

    def _render_subscription(self, sub_layout):
        from flask import render_template
        client = {
            'email': 'x', 'expiry': '', 'expiry_type': 'days', 'is_active': True,
            'percentage_used': 50, 'remaining': '1 GB', 'total_limit': '10 GB',
            'total_used': '1 GB', 'configs': ['vless://a'], 'last_ip': '', 'last_ip_operator': '',
            'server_name': 's', 'service_state_emoji': '', 'service_state_label': 'Active',
            'service_state_tag': 'ok', 'subscription_url': 'u',
        }
        with self.app.test_request_context('/s/1/abc'):
            return render_template(
                'subscription.html', client=client, apps=[], faqs=[], support={}, channels={},
                announcements=[], active_online_chat_script=None, backup_configs=[],
                sub_packages=[], renewal_recommendation=None, page_lang='en', server_id=1,
                sub_id='abc', csp_nonce='n', sub_layout=sub_layout)

    def test_page_renders_blocks_in_the_saved_order_and_hides_switched_off_ones(self):
        order = ['config', 'usage', 'faq', 'status', 'link', 'renew', 'history', 'backup']
        html = self._render_subscription({'template': 'dashboard', 'blocks': [
            {'id': block, 'visible': block != 'faq'} for block in order]})
        rendered = BLOCK_RE.findall(html)
        # backup has no configs, so its block is left out entirely.
        self.assertEqual([b for b, _ in rendered], [b for b in order if b != 'backup'])
        self.assertEqual(dict(rendered)['faq'], ' hidden')
        self.assertIn('data-sub-template="dashboard"', html)
        self.assertEqual(html.count('id="sec-usage"'), 1)

    def test_page_without_a_layout_keeps_the_classic_order(self):
        html = self._render_subscription(None)
        self.assertIn('data-sub-template="classic"', html)
        self.assertEqual([b for b, _ in BLOCK_RE.findall(html)],
                         [b for b in layout.BLOCKS if b != 'backup'])
        self.assertLess(html.index('id="sub-inbound-status"'), html.index('id="sec-renew"'))
        self.assertIn('id="sub-inbound-active-count">0</span>', html)
        self.assertIn('id="sub-inbound-disabled-count">0</span>', html)

    def test_compact_template_lists_visible_sections_as_shortcuts(self):
        html = self._render_subscription({'template': 'compact', 'blocks': [
            {'id': block, 'visible': block != 'history'} for block in layout.BLOCKS]})
        chips = re.findall(r'href="#sub-block-(\w+)" class="sub-quicknav-chip"', html)
        self.assertNotIn('history', chips)
        self.assertNotIn('backup', chips)
        self.assertIn('usage', chips)

    def test_save_validates_and_normalizes(self):
        bad = self.client.put('/api/subscription-page-layout', json={'template': 'nope'})
        # Business errors on /api/ reach the browser as 200 + X-Eve-Status (CDN workaround).
        self.assertEqual(bad.headers.get('X-Eve-Status'), '400')
        self.assertFalse(bad.get_json()['success'])
        response = self.client.put('/api/subscription-page-layout', json={
            'template': 'minimal',
            'layouts': {'minimal': [{'id': 'faq', 'visible': True}, {'id': 'usage', 'visible': False}]},
        })
        self.assertEqual(response.status_code, 200)
        saved = response.get_json()['layout']
        self.assertEqual(saved['template'], 'minimal')
        self.assertEqual([b['id'] for b in saved['layouts']['minimal']][:2], ['faq', 'usage'])
        self.assertTrue(saved['layouts']['minimal'][1]['visible'])
        stored = layout.load_layout()
        self.assertEqual(stored['template'], 'minimal')
        self.assertEqual(stored['layouts']['minimal'][0]['id'], 'faq')

    def test_save_requires_a_session(self):
        anonymous = self.app.test_client()
        response = anonymous.put('/api/subscription-page-layout', json={'template': 'glass'})
        self.assertIn(response.status_code, (302, 401, 403))
        self.assertEqual(layout.load_layout()['template'], layout.DEFAULT_TEMPLATE)

    def test_preview_renders_the_requested_template_and_order(self):
        response = self.client.get('/sub-manager/template-preview?template=glass&lang=en'
                                   '&order=faq,status,usage&hidden=faq')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-sub-template="glass"', html)
        ids = [b for b, _ in BLOCK_RE.findall(html)]
        self.assertEqual(ids[:3], ['faq', 'status', 'usage'])
        self.assertIn('data-sub-block="faq" hidden', html)

    def test_sub_manager_page_ships_the_editor_and_its_data(self):
        html = self.client.get('/sub-manager').get_data(as_text=True)
        self.assertIn('id="tab-page_layout"', html)
        self.assertIn('sub-layout-editor.js', html)
        data = re.search(r'<script type="application/json" id="sub-layout-data">(.*?)</script>',
                         html, re.S).group(1)
        payload = json.loads(data)
        self.assertEqual(set(payload['layout']['layouts']), set(layout.TEMPLATES))
        self.assertEqual(set(payload['required']), set(layout.REQUIRED_BLOCKS))
        for name in layout.TEMPLATES:
            self.assertIn('data-template="%s"' % name, html)


if __name__ == '__main__':
    unittest.main()
