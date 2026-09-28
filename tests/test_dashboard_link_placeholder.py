"""Regression coverage for the scheme-free dashboard-link placeholder."""

import base64
import os
from pathlib import Path
import tempfile
import unittest


_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault(
    'SERVER_PASSWORD_KEY',
    base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode(),
)
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import _render_text_template  # noqa: E402
from panel.routes.templates_api import _account_info_template_vars  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class DashboardLinkPlaceholderTests(unittest.TestCase):
    def test_server_renderer_derives_scheme_free_dashboard_link(self):
        template = '{dashboard_link}|{dashboard_link_no_https}'

        self.assertEqual(
            _render_text_template(template, {'dashboard_link': 'https://eve.example/s/1/id'}),
            'https://eve.example/s/1/id|eve.example/s/1/id',
        )
        self.assertEqual(
            _render_text_template(template, {'dashboard_link': 'HTTP://eve.example/s/1/id'}),
            'HTTP://eve.example/s/1/id|eve.example/s/1/id',
        )

    def test_explicit_scheme_free_value_is_preserved(self):
        rendered = _render_text_template(
            '{dashboard_link_no_https}',
            {
                'dashboard_link': 'https://eve.example/s/1/id',
                'dashboard_link_no_https': 'custom.example/link',
            },
        )

        self.assertEqual(rendered, 'custom.example/link')

    def test_placeholder_is_available_in_api_and_all_editor_chips(self):
        self.assertIn('{dashboard_link_no_https}', _account_info_template_vars())

        settings = (ROOT / 'templates' / 'settings.html').read_text(encoding='utf-8')
        self.assertIn("insertVar('{dashboard_link_no_https}')", settings)
        self.assertIn("insertRenewVar('{dashboard_link_no_https}')", settings)
        self.assertIn("insertAccountMessageVar('{dashboard_link_no_https}')", settings)

    def test_browser_renderers_derive_the_same_value(self):
        dashboard = (ROOT / 'templates' / 'dashboard.html').read_text(encoding='utf-8')
        account_message = (ROOT / 'static' / 'account-message.js').read_text(encoding='utf-8')

        self.assertIn("dashboard_link_no_https: dashboardLink.replace(/^https?:\\/\\//i, '')", dashboard)
        self.assertIn("dashboard_link_no_https: dashboardLink.replace(/^https?:\\/\\//i, '')", account_message)


if __name__ == '__main__':
    unittest.main()
