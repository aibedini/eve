"""Guard: the CSP blocks inline on*= event attributes (script-src-attr 'none').

Templates and page scripts must use data-on*= handlers, which
static/eve-actions.js runs without eval. An inline handler would silently stop
working, so none may come back.
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS = ('click', 'dblclick', 'change', 'input', 'submit', 'keydown', 'keyup', 'focus',
          'blur', 'error', 'load', 'dragstart', 'dragend', 'dragover', 'dragleave', 'drop',
          'mouseenter', 'mouseleave', 'paste')
INLINE = re.compile(r'(?<=[\s"\'<])on(?:' + '|'.join(EVENTS) + r')\s*=\s*\\?["\']')


class NoInlineHandlersTest(unittest.TestCase):
    def test_templates_and_scripts_have_no_inline_event_attributes(self):
        files = glob.glob(os.path.join(ROOT, 'templates', '**', '*.html'), recursive=True)
        files += [f for f in glob.glob(os.path.join(ROOT, 'static', '*.js'))
                  if '.min.' not in f and not f.endswith('eve-actions.js')]
        offenders = []
        for path in files:
            with open(path, encoding='utf-8') as handle:
                text = handle.read()
            for match in INLINE.finditer(text):
                line = text.count('\n', 0, match.start()) + 1
                offenders.append(f'{os.path.relpath(path, ROOT)}:{line}')
        self.assertEqual(offenders, [], 'use data-on*= instead of inline on*= handlers')

    def test_pages_with_handlers_load_the_dispatcher(self):
        for name in ('base.html', 'login.html', 'change_password_client.html', 'subscription.html'):
            with open(os.path.join(ROOT, 'templates', name), encoding='utf-8') as handle:
                self.assertIn("filename='eve-actions.js'", handle.read(), name)


if __name__ == '__main__':
    unittest.main()
