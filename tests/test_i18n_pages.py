"""Converted pages follow the panel language everywhere (panel/core/i18n.py).

A page listed in PAGES must have: no hardcoded English left in its markup, every
``_()`` / ``_t()`` string translated by its Persian catalog, no unused catalog entry,
matching ``{placeholders}``, and a Persian render that differs from the English one.
"""
import base64
import importlib
import importlib.util
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

from panel.core import i18n  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PERSIAN = re.compile('[\u0600-\u06FF]')
PLACEHOLDER = re.compile(r'\{(\w+)\}')

# page catalog module -> (route, template files whose strings it owns)
PAGES = {
    'monitor': ('/monitor', ['templates/monitor.html']),
    'finance': ('/finance', ['templates/finance.html']),
    'servers': ('/servers', ['templates/servers.html']),
    'admins': ('/admins', ['templates/admins.html']),
    'dashboard': ('/', ['templates/dashboard.html']),
}

_spec = importlib.util.spec_from_file_location('i18n_scan', ROOT / 'scripts' / 'i18n_scan.py')
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)


def _source(name):
    return (ROOT / name).read_text(encoding='utf-8').replace('\r\n', '\n')


class I18nPageCatalogTests(unittest.TestCase):
    def test_every_used_string_is_translated_and_nothing_is_unused(self):
        import panel.i18n  # noqa: F401  (registers the catalogs)
        common = set(i18n.modules()['common'])
        for page, (_route, files) in PAGES.items():
            module = importlib.import_module('panel.i18n.' + page)
            owned = set(i18n.modules()[page])
            used = set()
            for name in files:
                used |= scan.used_keys(_source(name))
            dynamic = set(getattr(module, 'DYNAMIC', ()))
            self.assertEqual(sorted(used - owned - common), [], '%s: untranslated' % page)
            self.assertEqual(sorted(owned - used - dynamic), [], '%s: unused catalog entries' % page)

    def test_catalog_values_are_persian_and_keep_their_placeholders(self):
        import panel.i18n  # noqa: F401
        for module, table in i18n.modules().items():
            for english, persian in table.items():
                self.assertNotRegex(english, PERSIAN, '%s: key must be English' % module)
                self.assertRegex(persian, PERSIAN, '%s: %r' % (module, english))
                self.assertEqual(set(PLACEHOLDER.findall(english)),
                                 set(PLACEHOLDER.findall(persian)), '%s: %r' % (module, english))

    def test_a_word_is_translated_the_same_way_on_every_page(self):
        import panel.i18n  # noqa: F401
        seen = {}
        for module, table in i18n.modules().items():
            for english, persian in table.items():
                self.assertEqual(seen.setdefault(english, persian), persian,
                                 '%r differs between catalogs (%s)' % (english, module))

    def test_pages_keep_no_hardcoded_markup_text_or_backtick_calls(self):
        for page, (_route, files) in PAGES.items():
            for name in files:
                source = _source(name)
                left = [item[1] for item in scan.candidates(source)]
                self.assertEqual(left, [], '%s still has hardcoded text' % name)
                self.assertNotRegex(source, r'(?<![\w.])_t?\(\s*`', '%s: use quotes in _()/_t()' % name)

    def test_shared_shell_keeps_no_untranslated_text(self):
        # The sidebar is translated through nav_copy in base.html; the two usage-history
        # notices are already written once per language inside {% if panel_lang %}.
        allowed = {'Eve Manager', 'rows'}
        left = [item[1] for item in scan.candidates(_source('templates/base.html'))
                if item[1] not in allowed and not item[1].startswith(
                    ('Usage history is being compacted', 'One-time usage-history compaction'))]
        self.assertEqual(left, [])

    def test_english_is_the_identity_and_persian_translates(self):
        self.assertEqual(i18n.translate('Save', 'en'), 'Save')
        self.assertEqual(i18n.translate('Never translated', 'fa'), 'Never translated')
        self.assertEqual(i18n.translate('Save', 'fa'), 'ذخیره')
        self.assertEqual(i18n.translate('Selected users: {count}', 'en', count=3), 'Selected users: 3')
        self.assertEqual(i18n.translate('Selected users: {count}', 'fa', count=3), 'کاربران انتخاب‌شده: 3')
        self.assertEqual(i18n.translate('Variables: {user}', 'en'), 'Variables: {user}')


class I18nPageRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import app
        from panel.extensions import db
        cls.app, cls.db = app, db
        cls.context = app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove()
        cls.context.pop()

    def _render(self, route, language):
        from app import PANEL_UI_LANG_SETTING_KEY, _set_system_setting_value
        from panel.models import Admin
        _set_system_setting_value(PANEL_UI_LANG_SETTING_KEY, language)
        self.db.session.commit()
        admin = Admin.query.filter_by(username='i18n-page-admin').first()
        if admin is None:
            admin = Admin(username='i18n-page-admin', password_hash='x', role='superadmin',
                          is_superadmin=True, enabled=True)
            self.db.session.add(admin)
            self.db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session.update(admin_id=admin.id, admin_username=admin.username,
                                   role=admin.role, is_superadmin=True)
        response = client.get(route)
        self.assertEqual(response.status_code, 200, route)
        return response.get_data(as_text=True)

    def tearDown(self):
        from app import PANEL_UI_LANG_SETTING_KEY, _set_system_setting_value
        _set_system_setting_value(PANEL_UI_LANG_SETTING_KEY, 'en')
        self.db.session.commit()

    def test_persian_render_translates_markup_and_hands_the_script_its_catalog(self):
        for page, (route, files) in PAGES.items():
            english = self._render(route, 'en')
            persian = self._render(route, 'fa')
            self.assertIn('window.EVE_I18N = {}', english, page)
            self.assertNotIn('window.EVE_I18N = {}', persian, page)
            self.assertIn('window.EVE_I18N = {', persian, page)
            catalog = i18n.catalog('common', page)
            for name in files:
                for key in re.findall(r"\{\{ _\('((?:\\.|[^'\\])*)'\) \}\}", _source(name)):
                    key = re.sub(r'\\(.)', r'\1', key)
                    if key in catalog:
                        import html
                        self.assertIn(html.escape(catalog[key]), persian, key)
            visible = re.sub(r'<script.*?</script>|<style.*?</style>|<!--.*?-->', '', english, flags=re.S)
            self.assertNotRegex(visible.split('<main', 1)[-1] if '<main' in visible else visible, PERSIAN, page)


if __name__ == '__main__':
    unittest.main()
