"""When did the subscription run out? Only the ledger may say.

3x-ui records counters and an expiry, never the moment a limit was hit, so EVE keeps
that moment on the observed-state ledger. The quality tag is what decides whether a
customer may read a time:

* ``observed_transition`` - EVE saw the account active, then saw it ended: a real time;
* ``inferred_expiry``    - the expiry timestamp explains an ``expired`` account;
* ``unknown_baseline``   - first sighting: no time, because "the day EVE started
  looking" is not "the day your data ran out".

A successful renewal puts the subscription back to ``active``, which is exactly why
the notice disappears instead of lingering.
"""
import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

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
from panel.core.digits import to_persian_digits  # noqa: E402
from panel.extensions import db  # noqa: E402
from panel.models import ServiceObservedState  # noqa: E402
from panel.routes import subscription_pages  # noqa: E402
from panel.services import subscription_ended as ended  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, 0)          # naive UTC, the column convention
ENTERED = datetime(2026, 10, 19, 11, 0, 0)      # 14:30 in Asia/Tehran


def _ms(moment):
    """Epoch milliseconds for a naive-UTC datetime (never the host's local zone)."""
    return int(moment.replace(tzinfo=timezone.utc).timestamp() * 1000)


EXPIRY_MS = _ms(datetime(2026, 10, 18, 20, 0, 0))  # 23:30 in Asia/Tehran


def _formatter(dt):
    """A fixed stamp so the wording is asserted without a calendar dependency."""
    return "%s %s" % (dt.strftime('%Y/%m/%d'), dt.strftime('%H:%M'))


class EndedMomentTests(unittest.TestCase):
    def test_an_observed_volume_end_has_a_real_moment(self):
        moment, quality = ended.ended_moment(
            state_key='volume_ended', ledger_state='volume_ended',
            state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED, now=NOW)
        self.assertEqual(moment, ENTERED)
        self.assertEqual(quality, ended.QUALITY_OBSERVED)

    def test_a_baseline_volume_end_has_no_moment(self):
        moment, quality = ended.ended_moment(
            state_key='volume_ended', ledger_state='volume_ended',
            state_entered_at=ENTERED, quality=ended.QUALITY_UNKNOWN, now=NOW)
        self.assertIsNone(moment)
        self.assertEqual(quality, ended.QUALITY_UNKNOWN)

    def test_volume_end_never_borrows_the_expiry(self):
        # An account can hit its volume cap days before its expiry: using the expiry
        # here would tell the customer the wrong moment with full confidence.
        moment, quality = ended.ended_moment(
            state_key='volume_ended', ledger_state='volume_ended',
            state_entered_at=None, quality=ended.QUALITY_INFERRED,
            expiry_ms=EXPIRY_MS, now=NOW)
        self.assertIsNone(moment)

    def test_an_expiry_is_its_own_moment(self):
        moment, quality = ended.ended_moment(
            state_key='expired', ledger_state='expired',
            state_entered_at=ENTERED, quality=ended.QUALITY_UNKNOWN,
            expiry_ms=EXPIRY_MS, now=NOW)
        self.assertEqual(quality, ended.QUALITY_INFERRED)
        self.assertEqual(_ms(moment), EXPIRY_MS)

    def test_an_expiry_without_a_timestamp_falls_back_to_the_transition(self):
        moment, quality = ended.ended_moment(
            state_key='expired', ledger_state='expired',
            state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED,
            expiry_ms=0, now=NOW)
        self.assertEqual(moment, ENTERED)
        self.assertEqual(quality, ended.QUALITY_OBSERVED)

    def test_an_expiry_in_the_future_cannot_explain_an_ended_account(self):
        future = _ms(NOW + timedelta(days=3))
        moment, quality = ended.ended_moment(
            state_key='expired', ledger_state='expired',
            quality=ended.QUALITY_INFERRED, expiry_ms=future, now=NOW)
        self.assertIsNone(moment)

    def test_an_active_subscription_has_no_moment(self):
        for state_key in ('active', 'expiring_soon', 'volume_low', 'inactive', None):
            moment, _quality = ended.ended_moment(
                state_key=state_key, ledger_state='volume_ended',
                state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED,
                expiry_ms=EXPIRY_MS, now=NOW)
            self.assertIsNone(moment, state_key)


class LedgerLagTests(unittest.TestCase):
    """The live state and the ledger's last written state are different facts.

    The page reads the panel now; the ledger was written by an earlier poll. When
    they disagree, ``state_entered_at`` is the moment a DIFFERENT state began - and
    printing it as "your data ran out at ..." invents a fact the evidence does not
    support. This is the reported bug: after a renewal the ledger still says
    ``active`` (with an observed transition, because the renewal itself was one),
    while the panel already shows ``volume_ended`` again.
    """

    def test_a_lagging_active_ledger_shows_no_volume_end_time(self):
        moment, quality = ended.ended_moment(
            state_key='volume_ended', ledger_state='active',
            state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED, now=NOW)
        self.assertIsNone(moment)
        self.assertEqual(quality, ended.QUALITY_UNKNOWN)

    def test_a_lagging_expired_ledger_shows_no_volume_end_time(self):
        moment, _quality = ended.ended_moment(
            state_key='volume_ended', ledger_state='expired',
            state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED, now=NOW)
        self.assertIsNone(moment)

    def test_a_matching_ledger_is_trusted(self):
        moment, quality = ended.ended_moment(
            state_key='volume_ended', ledger_state='volume_ended',
            state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED, now=NOW)
        self.assertEqual(moment, ENTERED)
        self.assertEqual(quality, ended.QUALITY_OBSERVED)

    def test_a_missing_ledger_state_is_not_a_match(self):
        # A row written before the state was recorded at all, or no row: the empty
        # string must never be treated as "same state".
        for ledger_state in (None, '', 'unknown', 'active'):
            moment, _quality = ended.ended_moment(
                state_key='volume_ended', ledger_state=ledger_state,
                state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED, now=NOW)
            self.assertIsNone(moment, ledger_state)

    def test_an_expired_account_does_not_need_the_ledger(self):
        # The expiry timestamp is the moment itself, so a lagging ledger cannot make
        # it wrong: the notice still appears.
        moment, quality = ended.ended_moment(
            state_key='expired', ledger_state='active',
            state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED,
            expiry_ms=EXPIRY_MS, now=NOW)
        self.assertEqual(quality, ended.QUALITY_INFERRED)
        self.assertEqual(_ms(moment), EXPIRY_MS)

    def test_a_lagging_ledger_cannot_rescue_an_expiry_without_a_timestamp(self):
        # No usable expiry AND a ledger describing another state: nothing is evidenced.
        moment, _quality = ended.ended_moment(
            state_key='expired', ledger_state='active',
            state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED,
            expiry_ms=0, now=NOW)
        self.assertIsNone(moment)


class NoticeTextTests(unittest.TestCase):
    def test_the_persian_volume_sentence_carries_date_and_time(self):
        text = ended.notice_text(state_key='volume_ended', moment=ENTERED,
                                 lang='fa', formatter=_formatter)
        self.assertEqual(text, 'حجم شما در تاریخ 2026/10/19 ساعت 11:00 تمام شد')

    def test_the_persian_expiry_sentence_is_different_wording(self):
        text = ended.notice_text(state_key='expired', moment=ENTERED,
                                 lang='fa', formatter=_formatter)
        self.assertEqual(text, 'اشتراک شما در تاریخ 2026/10/19 ساعت 11:00 منقضی شد')

    def test_english_wording(self):
        volume = ended.notice_text(state_key='volume_ended', moment=ENTERED,
                                   lang='en', formatter=_formatter)
        expired = ended.notice_text(state_key='expired', moment=ENTERED,
                                    lang='en', formatter=_formatter)
        self.assertEqual(volume, 'Your data ran out on 2026/10/19 at 11:00')
        self.assertEqual(expired, 'Your subscription expired on 2026/10/19 at 11:00')

    def test_no_moment_or_no_formatter_produces_nothing(self):
        self.assertIsNone(ended.notice_text(state_key='expired', moment=None,
                                            lang='fa', formatter=_formatter))
        self.assertIsNone(ended.notice_text(state_key='expired', moment=ENTERED,
                                            lang='fa', formatter=None))

    def test_a_date_only_stamp_still_reads(self):
        text = ended.notice_text(state_key='expired', moment=ENTERED, lang='fa',
                                 formatter=lambda _dt: '1404/07/28')
        self.assertEqual(text, 'اشتراک شما در تاریخ 1404/07/28 منقضی شد')


class BuildNoticeTests(unittest.TestCase):
    def test_the_three_qualities(self):
        observed = ended.build_notice(
            state_key='volume_ended', ledger_state='volume_ended',
            state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED,
            lang='fa', formatter=_formatter, now=NOW)
        self.assertIsNotNone(observed)
        self.assertEqual(observed['quality'], ended.QUALITY_OBSERVED)
        self.assertIn('حجم شما', observed['text'])

        inferred = ended.build_notice(
            state_key='expired', quality=ended.QUALITY_UNKNOWN,
            expiry_ms=EXPIRY_MS, lang='fa', formatter=_formatter, now=NOW)
        self.assertIsNotNone(inferred)
        self.assertEqual(inferred['quality'], ended.QUALITY_INFERRED)

        unknown = ended.build_notice(
            state_key='volume_ended', ledger_state='volume_ended',
            state_entered_at=ENTERED, quality=ended.QUALITY_UNKNOWN,
            lang='fa', formatter=_formatter, now=NOW)
        self.assertIsNone(unknown)

    def test_a_lagging_ledger_produces_no_notice_at_all(self):
        self.assertIsNone(ended.build_notice(
            state_key='volume_ended', ledger_state='active',
            state_entered_at=ENTERED, quality=ended.QUALITY_OBSERVED,
            lang='fa', formatter=_formatter, now=NOW))

    def test_persian_digits_do_not_touch_punctuation(self):
        self.assertEqual(to_persian_digits('2026/10/19 11:00'), '۲۰۲۶/۱۰/۱۹ ۱۱:۰۰')
        self.assertEqual(to_persian_digits(None), '')


class RouteLookupTests(unittest.TestCase):
    """The ledger read the subscription page performs for one client."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        ServiceObservedState.query.delete()
        db.session.commit()

    def _row(self, *, quality, entered_at=ENTERED, state='volume_ended'):
        row = ServiceObservedState(
            service_key='eve:7:uuid-abc', server_id=7, client_uuid='uuid-abc',
            client_email='someone@example.com', last_state=state,
            state_entered_at=entered_at, state_entered_at_quality=quality,
            last_observed_at=NOW)
        db.session.add(row)
        db.session.commit()
        return row

    def _notice(self, *, state_key, expiry_ms=0, lang='en'):
        with mock.patch.object(
                subscription_pages.lifecycle_service,
                'resolve_canonical_service_key', return_value='eve:7:uuid-abc'):
            return subscription_pages._subscription_ended_notice(
                server_id=7, client_email='someone@example.com',
                client={'id': 'uuid-abc', 'email': 'someone@example.com'},
                state_key=state_key, expiry_ms=expiry_ms, lang=lang)

    def test_an_observed_transition_is_rendered_in_the_application_timezone(self):
        self._row(quality=ended.QUALITY_OBSERVED, state='volume_ended')
        notice = self._notice(state_key='volume_ended')
        self.assertIsNotNone(notice)
        self.assertEqual(notice['quality'], ended.QUALITY_OBSERVED)
        # 11:00 UTC is 14:30 in Asia/Tehran (the default application timezone).
        self.assertIn('14:30', notice['text'])
        self.assertIn('ran out', notice['text'])

    def test_a_lagging_active_ledger_renders_no_time(self):
        # The reported bug: the panel says volume_ended, the ledger still says active
        # because the last transition it recorded was the renewal.
        self._row(quality=ended.QUALITY_OBSERVED, state='active')
        self.assertIsNone(self._notice(state_key='volume_ended'))

    def test_a_lagging_expired_ledger_renders_no_time_for_a_volume_end(self):
        self._row(quality=ended.QUALITY_OBSERVED, state='expired')
        self.assertIsNone(self._notice(state_key='volume_ended'))

    def test_an_expired_account_with_a_lagging_ledger_still_shows_its_expiry(self):
        self._row(quality=ended.QUALITY_OBSERVED, state='active')
        notice = self._notice(state_key='expired', expiry_ms=EXPIRY_MS)
        self.assertIsNotNone(notice)
        self.assertEqual(notice['quality'], ended.QUALITY_INFERRED)
        self.assertIn('23:30', notice['text'])

    def test_a_baseline_row_renders_no_time(self):
        self._row(quality=ended.QUALITY_UNKNOWN)
        self.assertIsNone(self._notice(state_key='volume_ended'))

    def test_an_expired_account_uses_its_expiry_even_without_a_row(self):
        notice = self._notice(state_key='expired', expiry_ms=EXPIRY_MS)
        self.assertIsNotNone(notice)
        self.assertEqual(notice['quality'], ended.QUALITY_INFERRED)
        self.assertIn('23:30', notice['text'])   # 20:00 UTC in Tehran

    def test_a_renewed_account_shows_nothing(self):
        self._row(quality=ended.QUALITY_OBSERVED)
        self.assertIsNone(self._notice(state_key='active', expiry_ms=EXPIRY_MS))

    def test_a_missing_ledger_row_is_not_an_error(self):
        with mock.patch.object(
                subscription_pages.lifecycle_service,
                'resolve_canonical_service_key', return_value='eve:7:missing'):
            notice = subscription_pages._subscription_ended_notice(
                server_id=7, client_email='someone@example.com', client={},
                state_key='volume_ended', expiry_ms=0, lang='en')
        self.assertIsNone(notice)


class RenderedPageTests(unittest.TestCase):
    """The two new lines must survive the real template, not just the helpers."""

    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        ServiceObservedState.query.delete()
        db.session.commit()

    def _client(self, **overrides):
        payload = {
            'email': 'someone@example.com', 'last_ip': '', 'last_ip_operator': '',
            'is_active': True, 'service_state': 'active', 'service_state_label': 'فعال',
            'service_state_emoji': '', 'service_state_tag': 'ok',
            'total_used': '1 GB', 'total_limit': '20 GB', 'percentage_used': 5,
            'expiry': '۳ ساعت و ۵ دقیقه باقی مانده', 'expiry_days': 0,
            'expiry_type': 'today', 'remaining': '19 GB',
            'subscription_url': 'https://eve.test/s/1/abc', 'configs': [],
            'server_name': 'panel', 'expiry_ms': 1, 'server_now_ms': 1,
            'ended_at_text': '',
        }
        payload.update(overrides)
        return payload

    def _render(self, client):
        from flask import render_template
        with app_module.app.test_request_context('/s/1/abc'):
            return render_template(
                'subscription.html', client=client, apps=[], faqs=[], support={},
                channels={}, announcements=[], active_online_chat_script=None,
                backup_configs=[], sub_packages=[], renewal_recommendation=None,
                page_lang='fa', server_id=1, sub_id='abc')

    def test_a_same_day_expiry_renders_its_countdown_not_a_fixed_word(self):
        html = self._render(self._client())
        self.assertIn('id="sub-expiry-text"', html)
        self.assertIn('۳ ساعت و ۵ دقیقه باقی مانده', html)
        self.assertNotIn('>امروز<', html)

    def test_a_delayed_start_keeps_its_own_sentence(self):
        # The branch this test protects was the one carrying a raw digit
        # ({{ client.expiry_days }}), which would have stayed Latin on a page that is
        # otherwise Persian-digit now.
        html = self._render(self._client(
            expiry_type='start_after_use', expiry_days=31,
            expiry='۳۱ روز بعد از اولین اتصال'))
        self.assertIn('۳۱ روز بعد از اولین اتصال', html)
        self.assertNotIn('31 روز بعد از اولین اتصال', html)

    def test_an_expired_account_keeps_the_state_word(self):
        html = self._render(self._client(
            expiry_type='expired', expiry='منقضی شده (۲ روز پیش)'))
        self.assertIn('منقضی شده', html)

    def test_the_ended_line_appears_only_when_the_ledger_proved_it(self):
        with_notice = self._render(self._client(
            ended_at_text='حجم شما در تاریخ ۱۴۰۴/۰۷/۲۸ ساعت ۱۴:۳۰ تمام شد'))
        self.assertIn('id="sub-ended-at"', with_notice)
        self.assertIn('حجم شما در تاریخ ۱۴۰۴/۰۷/۲۸ ساعت ۱۴:۳۰ تمام شد', with_notice)

        without = self._render(self._client(ended_at_text=''))
        self.assertNotIn('id="sub-ended-at"', without)

    def test_the_page_hands_the_script_its_absolute_milliseconds(self):
        html = self._render(self._client(expiry_ms=1795080448277,
                                         server_now_ms=1792402048277))
        self.assertIn('const SUB_EXPIRY_MS = 1795080448277;', html)
        self.assertIn('const SUB_SERVER_NOW_MS = 1792402048277;', html)

    def test_a_lagging_ledger_leaves_the_page_without_an_ended_line(self):
        # The reported bug, end to end: the panel says volume_ended while the ledger
        # still says active. The helper must return nothing, and the rendered page
        # must therefore have no "ended at" line at all.
        db.session.add(ServiceObservedState(
            service_key='eve:7:uuid-abc', server_id=7, client_uuid='uuid-abc',
            client_email='someone@example.com', last_state='active',
            state_entered_at=ENTERED,
            state_entered_at_quality=ended.QUALITY_OBSERVED,
            last_observed_at=NOW))
        db.session.commit()
        with mock.patch.object(
                subscription_pages.lifecycle_service,
                'resolve_canonical_service_key', return_value='eve:7:uuid-abc'):
            notice = subscription_pages._subscription_ended_notice(
                server_id=7, client_email='someone@example.com',
                client={'id': 'uuid-abc'}, state_key='volume_ended',
                expiry_ms=0, lang='fa')
        self.assertIsNone(notice)
        html = self._render(self._client(
            service_state='volume_ended', expiry_type='expired',
            expiry='منقضی شده', ended_at_text=''))
        self.assertNotIn('id="sub-ended-at"', html)


if __name__ == '__main__':
    unittest.main()
