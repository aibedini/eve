"""Deployment/static contract: HTML of build N may only reference assets of build N.

The intermittent Subscription layout bug was a version-skew symptom, so the contract is
mechanical: every static URL a page emits must carry the content version of the file it names,
that version must equal what the server computes today, and the page must state the same build
its response headers do. A page that references an unversioned or stale asset fails here instead
of failing in a customer's browser.

The same assertions run against the dashboard and the subscription page, which are the two
surfaces the report touched.
"""
import os
import re
import tempfile
import unittest

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import (  # noqa: E402
    Admin, GLOBAL_SERVER_DATA, Server, _static_asset_version, app, db,
)
from panel.core import build_identity  # noqa: E402

STATIC_REF = re.compile(r'(?:href|src)="(/static/[^"]+)"')
MISSING_VERSION_ALLOWED = (
    # Uploaded app files and receipt images carry their own per-file names (documented in
    # docs/performance/STATIC_ASSETS.md); they are not part of the build's asset set.
    '/static/uploads/',
    '/static/app-files/',
)


def _static_refs(html):
    return [match.group(1) for match in STATIC_REF.finditer(html)]


class StaticDeployContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        Admin.query.delete()
        Server.query.delete()
        db.session.commit()
        cls.admin = Admin(username='contract-admin', role='superadmin', is_superadmin=True,
                          enabled=True)
        cls.admin.set_password('CorrectHorseBattery1!')
        cls.server = Server(name='contract', host='https://contract.invalid', username='u',
                            password='p', sub_path='/sub/', panel_type='auto', enabled=True)
        db.session.add_all([cls.admin, cls.server])
        db.session.commit()
        cls.admin_id = int(cls.admin.id)
        cls.server_id = int(cls.server.id)
        cls.server_name = str(cls.server.name)
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def _dashboard_html(self):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_id'] = self.admin_id
            sess['role'] = 'superadmin'
            sess['is_superadmin'] = True
        return self.client.get('/').get_data(as_text=True)

    def _subscription_html(self):
        from flask import render_template
        client_payload = {
            'email': 'contract@example.test', 'expiry': '', 'expiry_days': 0,
            'expiry_type': 'days', 'is_active': True, 'percentage_used': 10,
            'remaining': '9 GB', 'total_limit': '10 GB', 'total_used': '1 GB', 'configs': [],
            'last_ip': '', 'last_ip_operator': '', 'server_name': 'contract',
            'service_state_emoji': '', 'service_state_label': 'Active',
            'service_state_tag': 'active', 'subscription_url': '',
        }
        with app.test_request_context('/s/1/token'):
            return render_template(
                'subscription.html', client=client_payload, apps=[], faqs=[], support={},
                channels={}, announcements=[], active_online_chat_script=None,
                backup_configs=[], sub_packages=[], renewal_recommendation=None,
                page_lang='en', server_id=self.server_id, sub_id='token',
                server={'id': self.server_id, 'name': self.server_name},
                sse_enabled=False, csp_nonce='n')

    def _assert_contract(self, html, *, label):
        refs = _static_refs(html)
        self.assertTrue(refs, '%s referenced no static assets at all' % label)
        for ref in refs:
            path, _, query = ref.partition('?')
            if any(allowed in path for allowed in MISSING_VERSION_ALLOWED):
                continue
            self.assertTrue(query.startswith('v='),
                            '%s referenced an unversioned asset: %s' % (label, ref))
            filename = path[len('/static/'):]
            expected = _static_asset_version(filename)
            self.assertIsNotNone(expected, '%s references a missing asset: %s' % (label, ref))
            self.assertEqual(
                query.split('v=', 1)[1], expected,
                '%s references %s at a stale version (page %s, file %s) - this is the skew the '
                'bug report describes' % (label, filename, query.split('v=', 1)[1], expected))

    def test_the_dashboard_references_only_its_own_builds_assets(self):
        self._assert_contract(self._dashboard_html(), label='dashboard')

    def test_the_subscription_page_references_only_its_own_builds_assets(self):
        self._assert_contract(self._subscription_html(), label='subscription')

    def test_the_page_states_the_same_build_as_the_response(self):
        build_identity.reset_cache()
        self.addCleanup(build_identity.reset_cache)
        response = self.client.get('/')
        html = response.get_data(as_text=True)
        header_build = response.headers.get('X-Eve-Build')
        self.assertIsNotNone(header_build)
        match = re.search(r'<meta name="eve-build" content="([^"]*)"', html)
        self.assertIsNotNone(match, 'the page does not state its build')
        self.assertEqual(match.group(1), header_build)

    def test_the_contract_detects_a_stale_reference(self):
        """The check has teeth: a page naming an old version must fail it."""
        html = self._dashboard_html()
        refs = _static_refs(html)
        self.assertTrue(refs)
        broken = html.replace(refs[0], refs[0].split('?')[0] + '?v=stale-build')
        with self.assertRaises(AssertionError):
            self._assert_contract(broken, label='simulated stale page')

    def test_an_unversioned_stylesheet_is_rejected(self):
        html = self._dashboard_html()
        refs = _static_refs(html)
        broken = html.replace(refs[0], refs[0].split('?')[0])
        with self.assertRaises(AssertionError):
            self._assert_contract(broken, label='simulated unversioned page')


class SubscriptionCachePolicyTests(unittest.TestCase):
    """Complementary half of the contract: which half may be cached at all."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        GLOBAL_SERVER_DATA.setdefault('inbounds', [])
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_assets_are_cacheable_and_pages_are_not(self):
        asset = self.client.get('/static/style.css?v=' + _static_asset_version('style.css'))
        self.assertIn('public', (asset.headers.get('Cache-Control') or '').lower())
        page = self.client.get('/s/9999/whatever')
        cache = (page.headers.get('Cache-Control') or '').lower()
        self.assertIn('private', cache)
        self.assertIn('no-store', cache)


if __name__ == '__main__':
    unittest.main()
