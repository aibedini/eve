"""The build each response came from (RFP-equivalent: deployment verifiability).

A version-skew symptom is only diagnosable when two responses can be attributed to builds, so
every response carries the identity, the HTML repeats it, and ``EVE_BUILD_SHA`` lets a deploy
make all nodes agree.
"""
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import APP_VERSION, Admin, app, db  # noqa: E402
from panel.core import build_identity  # noqa: E402


class BuildIdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        build_identity.reset_cache()
        self.addCleanup(build_identity.reset_cache)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop(build_identity.ENV_VAR, None)

    def test_the_deployment_stamp_wins(self):
        with mock.patch.dict(os.environ, {build_identity.ENV_VAR: 'deadbeefcafe'}):
            resolved = build_identity.resolve_build_sha('1.2.3')
        self.assertEqual(resolved, {'sha': 'deadbeefcafe', 'source': 'env'})
        # Every node of one release reports the same value because the deploy sets it.
        self.assertEqual(build_identity.build_sha('1.2.3'), 'deadbeefcafe')

    def test_the_git_revision_is_used_without_a_stamp(self):
        with mock.patch.object(build_identity, '_from_git', return_value='abc123def456'):
            resolved = build_identity.resolve_build_sha('1.2.3')
        self.assertEqual(resolved, {'sha': 'abc123def456', 'source': 'git'})

    def test_the_application_version_is_the_last_resort(self):
        with mock.patch.object(build_identity, '_from_git', return_value=None):
            resolved = build_identity.resolve_build_sha('1.2.3')
        self.assertEqual(resolved, {'sha': '1.2.3', 'source': 'app_version'})

    def test_a_git_failure_never_raises(self):
        with mock.patch.object(build_identity.subprocess, 'run',
                               side_effect=OSError('no git here')):
            build_identity.reset_cache()
            resolved = build_identity.resolve_build_sha('9.9.9')
        self.assertEqual(resolved['sha'], '9.9.9')
        self.assertEqual(resolved['source'], 'app_version')

    def test_an_absurdly_long_value_is_truncated(self):
        with mock.patch.dict(os.environ, {build_identity.ENV_VAR: 'x' * 500}):
            self.assertEqual(len(build_identity.build_sha()), build_identity.MAX_LENGTH)

    def test_the_value_is_stable_inside_a_process(self):
        with mock.patch.dict(os.environ, {build_identity.ENV_VAR: 'first'}):
            self.assertEqual(build_identity.build_sha(), 'first')
        os.environ.pop(build_identity.ENV_VAR, None)
        # Memoized: a response cannot disagree with the one before it in the same worker.
        self.assertEqual(build_identity.build_sha(), 'first')


class BuildHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        Admin.query.delete()
        cls.admin = Admin(username='build-admin', role='superadmin', is_superadmin=True,
                          enabled=True)
        cls.admin.set_password('CorrectHorseBattery1!')
        db.session.add(cls.admin)
        db.session.commit()
        cls.admin_id = int(cls.admin.id)
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def setUp(self):
        build_identity.reset_cache()
        self.addCleanup(build_identity.reset_cache)

    def test_every_response_kind_carries_the_build(self):
        with mock.patch.dict(os.environ, {build_identity.ENV_VAR: 'build-42'}):
            build_identity.reset_cache()
            for path in ('/', '/static/style.css', '/api/refresh'):
                response = self.client.get(path)
                self.assertEqual(response.headers.get('X-Eve-Build'), 'build-42', path)
                self.assertEqual(response.headers.get('X-Eve-Build-Source'), 'env', path)

    def test_without_a_stamp_the_header_still_exists(self):
        os.environ.pop(build_identity.ENV_VAR, None)
        with mock.patch.object(build_identity, '_from_git', return_value=None):
            build_identity.reset_cache()
            response = self.client.get('/')
            self.assertIsNotNone(response.headers.get('X-Eve-Build'))
            self.assertEqual(response.headers.get('X-Eve-Build-Source'), 'app_version')

    def test_the_html_repeats_the_build(self):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess['admin_id'] = self.admin_id
            sess['role'] = 'superadmin'
            sess['is_superadmin'] = True
        with mock.patch.dict(os.environ, {build_identity.ENV_VAR: 'build-html'}):
            build_identity.reset_cache()
            # The dashboard extends base.html, which carries the meta tag.
            body = self.client.get('/').get_data(as_text=True)
        self.assertIn('<meta name="eve-build" content="build-html">', body)

    def test_the_subscription_template_exposes_it_too(self):
        client = {
            'email': 'x', 'expiry': '', 'expiry_days': 0, 'expiry_type': 'days',
            'is_active': True, 'percentage_used': 50, 'remaining': '1 GB',
            'total_limit': '10 GB', 'total_used': '1 GB', 'configs': [],
            'last_ip': '', 'last_ip_operator': '', 'server_name': 's',
            'service_state_emoji': '', 'service_state_label': 'Active',
            'service_state_tag': 'active', 'subscription_url': '',
        }
        with app.test_request_context('/s/1/abc'):
            from flask import render_template
            html = render_template(
                'subscription.html', client=client, apps=[], faqs=[], support={},
                channels={}, announcements=[], active_online_chat_script=None,
                backup_configs=[], sub_packages=[], renewal_recommendation=None,
                page_lang='en', server_id=1, sub_id='abc',
                server={'id': 1, 'name': 's'}, sse_enabled=False, csp_nonce='n')
        self.assertIn('<meta name="eve-build"', html)
        self.assertIn(build_identity.build_sha(APP_VERSION), html)


if __name__ == '__main__':
    unittest.main()
