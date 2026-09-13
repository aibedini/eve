"""The visual-regression harness must keep covering the widths and symptoms of the report.

The browser run itself needs Playwright and a live subscription page (see
docs/operations/VISUAL_REGRESSION.md), so this test guards the *harness*: the committed spec and
configuration must still cover every width the report named and every symptom it described. That
way the suite cannot silently lose coverage while it is unable to run in a development checkout.
"""
import io
import os
import re
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = os.path.join(_REPO_ROOT, 'e2e', 'visual', 'subscription.spec.js')
_CONFIG = os.path.join(_REPO_ROOT, 'e2e', 'visual', 'playwright.config.js')
_DOC = os.path.join(_REPO_ROOT, 'docs', 'operations', 'VISUAL_REGRESSION.md')

WIDTHS = (360, 390, 640, 768, 1024, 1280, 1440)


def _read(path):
    with io.open(path, encoding='utf-8') as handle:
        return handle.read()


class VisualHarnessTests(unittest.TestCase):
    def test_the_spec_and_config_cover_every_reported_width(self):
        spec = _read(_SPEC)
        config = _read(_CONFIG)
        spec_widths = {int(value) for value in re.findall(r'^\s*const WIDTHS = \[([^\]]*)\]',
                                                          spec, re.MULTILINE)[0].split(',')}
        config_widths = {int(value) for value in re.findall(r'\[([0-9, ]+)\]',
                                                            config)[0].split(',')}
        self.assertEqual(spec_widths, set(WIDTHS), spec_widths)
        self.assertEqual(config_widths, set(WIDTHS), config_widths)

    def test_the_spec_asserts_the_three_symptoms(self):
        spec = _read(_SPEC)
        lowered = spec.lower()
        self.assertIn('boundingclientrect', lowered)          # measured geometry
        self.assertIn('tobelessthanorequal', lowered)         # an upper bound, not just "exists"
        self.assertIn('scrollwidth', lowered)                 # horizontal overflow
        self.assertIn('clientwidth', lowered)
        self.assertIn('rows', lowered)                        # column stability
        self.assertIn('eve-build', lowered)                   # build agreement

    def test_the_spec_skips_without_a_target_instead_of_failing(self):
        spec = _read(_SPEC)
        self.assertIn('test.skip(!SUB_URL', spec)
        self.assertIn('EVE_E2E_SUB_URL', spec)

    def test_the_documentation_is_honest_about_the_run_status(self):
        doc = _read(_DOC)
        for token in ('360, 390, 640, 768, 1024, 1280 and 1440', 'EVE_E2E_SUB_URL',
                      'npx playwright install chromium',
                      'has not been executed in this development'):
            self.assertIn(token, doc, token)


if __name__ == '__main__':
    unittest.main()
