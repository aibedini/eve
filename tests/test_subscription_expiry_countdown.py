"""The subscription page's validity line: the countdown and its boundaries.

A client whose subscription ends TODAY used to read the fixed word "امروز" while its
account was actually disabled hours earlier or later, and the old formatter dropped
the minutes whenever hours were present ("23 ساعت") and fell back to "Today" for
anything under a minute - which is the value that also reached the statistics line as
"0 روز".

The formatter is now split into a pure `format_remaining_span`, mirrored in the page
script so the browser can refresh it every 30 s. The two implementations must agree
character for character, so they are pinned to the same vectors here.
"""
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
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

import app as app_module  # noqa: E402
from panel.routes.subscription_pages import _build_subscription_statistics_values  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SUBSCRIPTION = ROOT / 'templates' / 'subscription.html'

MINUTE = 60000
HOUR = 3600000
DAY = 86400000

#: (delta_ms, english, persian) - the contract, spelled out once.
VECTORS = (
    (0, '<1m left', 'کمتر از یک دقیقه'),
    (59000, '<1m left', 'کمتر از یک دقیقه'),
    (MINUTE, '1m left', '1 دقیقه باقی مانده'),
    (59 * MINUTE, '59m left', '59 دقیقه باقی مانده'),
    (HOUR, '1h left', '1 ساعت باقی مانده'),
    (HOUR + 45 * MINUTE, '1h 45m left', '1 ساعت و 45 دقیقه باقی مانده'),
    (23 * HOUR + 59 * MINUTE, '23h 59m left', '23 ساعت و 59 دقیقه باقی مانده'),
    (DAY, '1d left', '1 روز باقی مانده'),
    (DAY + 5 * HOUR, '1d 5h left', '1 روز و 5 ساعت باقی مانده'),
    (DAY + 5 * HOUR + 30 * MINUTE, '1d 5h left', '1 روز و 5 ساعت باقی مانده'),
    (-1, '<1m left', 'کمتر از یک دقیقه'),
)


def _run_node(source, expression):
    node = shutil.which('node')
    if not node:
        raise unittest.SkipTest('node is required to evaluate the page helpers')
    script = "%s\nprocess.stdout.write(JSON.stringify(%s));\n" % (source, expression)
    handle, path = tempfile.mkstemp(suffix='.cjs', prefix='eve_sub_clock_')
    os.close(handle)
    try:
        with open(path, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(script)
        result = subprocess.run([node, path], capture_output=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError('node failed:\n' + result.stderr.decode('utf-8', 'replace'))
        return json.loads(result.stdout.decode('utf-8', 'replace'))
    finally:
        os.remove(path)


def _clock_region():
    template = SUBSCRIPTION.read_text(encoding='utf-8')
    start = template.index('// subscription-expiry-clock:start')
    end = template.index('// subscription-expiry-clock:end')
    return template[start:end]


class FormatRemainingSpanTests(unittest.TestCase):
    def test_the_published_vectors(self):
        for delta_ms, english, persian in VECTORS:
            with self.subTest(delta_ms=delta_ms):
                self.assertEqual(app_module.format_remaining_span(delta_ms), english)
                self.assertEqual(
                    app_module.format_remaining_span(delta_ms, lang='fa'), persian)

    def test_minutes_are_kept_beside_hours(self):
        # The regression: "23 ساعت" told a customer nothing about how much of that
        # hour was left, and read as if the page had rounded against them.
        self.assertIn('45', app_module.format_remaining_span(23 * HOUR + 45 * MINUTE))
        self.assertIn('45', app_module.format_remaining_span(
            23 * HOUR + 45 * MINUTE, lang='fa'))

    def test_a_day_scale_span_drops_the_minutes(self):
        text = app_module.format_remaining_span(3 * DAY + 2 * HOUR + 30 * MINUTE)
        self.assertEqual(text, '3d 2h left')
        self.assertNotIn('0h', app_module.format_remaining_span(3 * DAY))

    def test_under_a_minute_is_not_zero_minutes(self):
        self.assertEqual(app_module.format_remaining_span(1), '<1m left')
        self.assertEqual(app_module.format_remaining_span(1, lang='fa'),
                         'کمتر از یک دقیقه')

    def test_a_non_numeric_span_is_treated_as_empty(self):
        self.assertEqual(app_module.format_remaining_span(None), '<1m left')
        self.assertEqual(app_module.format_remaining_span('nonsense'), '<1m left')


class ExpiryTypeTests(unittest.TestCase):
    def test_today_carries_the_countdown_instead_of_a_bare_day_count(self):
        now_ms = int(time.time() * 1000)
        info = app_module.format_remaining_days(
            now_ms + 3 * HOUR + 5 * MINUTE, lang='en')
        self.assertEqual(info['type'], 'today')
        self.assertEqual(info['days'], 0)
        self.assertEqual(info['text'], '3h 5m left')

    def test_expired_is_still_reported_as_expired(self):
        now_ms = int(time.time() * 1000)
        info = app_module.format_remaining_days(now_ms - 2 * DAY, lang='en')
        self.assertEqual(info['type'], 'expired')
        self.assertIn('Expired', info['text'])

    def test_a_delayed_start_is_not_a_countdown(self):
        info = app_module.format_remaining_days(-31 * DAY, lang='fa')
        self.assertEqual(info['type'], 'start_after_use')
        self.assertIn('بعد از اولین اتصال', info['text'])


class StatisticsLineTests(unittest.TestCase):
    def _values(self, expiry_info, lang='fa'):
        return _build_subscription_statistics_values(
            {'key': 'active', 'label': 'فعال'},
            expiry_info,
            remaining_bytes=10 * 1024 ** 3,
            total_limit=20 * 1024 ** 3,
            total_used=10 * 1024 ** 3,
            client_email='someone@example.com',
            lang=lang,
        )

    def test_a_today_expiry_does_not_render_zero_days(self):
        values = self._values({'type': 'today', 'days': 0,
                               'text': '۳ ساعت و ۵ دقیقه باقی مانده'})
        self.assertEqual(values['days'], '۳ ساعت و ۵ دقیقه باقی مانده')
        self.assertNotIn('0', values['days'])

    def test_a_normal_expiry_still_renders_a_day_count(self):
        values = self._values({'type': 'normal', 'days': 12, 'text': '12 روز باقی مانده'})
        self.assertEqual(values['days'], '12 روز')

    def test_an_unlimited_expiry_renders_unlimited(self):
        values = self._values({'type': 'unlimited', 'days': -1, 'text': 'نامحدود'})
        self.assertEqual(values['days'], 'نامحدود')

    def test_english_today_uses_the_countdown_too(self):
        values = self._values({'type': 'today', 'days': 0, 'text': '3h 5m left'}, lang='en')
        self.assertEqual(values['days'], '3h 5m left')


class PageScriptParityTests(unittest.TestCase):
    """The browser copy must produce exactly what the server produced."""

    def test_the_script_mirrors_every_vector(self):
        region = _clock_region()
        cases = [{'delta': delta, 'fa': False} for delta, _en, _fa in VECTORS]
        cases += [{'delta': delta, 'fa': True} for delta, _en, _fa in VECTORS]
        results = _run_node(
            region + '\nfunction __run(cases){return cases.map(function(c){'
                     'return subCountdownText(c.delta, c.fa);});}\n',
            '__run(%s)' % json.dumps(cases))
        for case, text in zip(cases, results):
            expected = app_module.format_remaining_span(
                case['delta'], lang='fa' if case['fa'] else 'en')
            self.assertEqual(text, expected, case)

    def test_persian_digits_cover_only_digits(self):
        region = _clock_region()
        results = _run_node(
            region + '\n',
            '[subPersianDigits("1404/07/28 14:30"), subPersianDigits(null)]')
        self.assertEqual(results[0], '۱۴۰۴/۰۷/۲۸ ۱۴:۳۰')
        self.assertEqual(results[1], '')

    def test_the_page_wires_the_counter_with_markers_and_ids(self):
        template = SUBSCRIPTION.read_text(encoding='utf-8')
        self.assertIn('// subscription-expiry-clock:start', template)
        self.assertIn('// subscription-expiry-clock:end', template)
        self.assertIn('id="sub-expiry-text"', template)
        self.assertIn('setInterval(refreshSubscriptionExpiryText, 30000)', template)
        self.assertIn('const SUB_EXPIRY_MS', template)
        self.assertIn('const SUB_SERVER_NOW_MS', template)

    def test_the_validity_line_renders_the_computed_text(self):
        template = SUBSCRIPTION.read_text(encoding='utf-8')
        region = template.split('id="sub-expiry-text"', 1)[1].split('</span>', 1)[0]
        self.assertIn('client.expiry', region)
        # "today" is deliberately no longer a special case: a same-day expiry renders
        # the same computed sentence as every other future expiry.
        self.assertNotIn("expiry_type == 'today'", region)
        self.assertNotIn('امروز', region)


if __name__ == '__main__':
    unittest.main()
