"""When did this subscription actually run out?

3x-ui stores usage counters and an expiry, never the moment a limit was hit. EVE
therefore keeps that moment itself, on the observed-state ledger
(``ServiceObservedState.state_entered_at`` + ``state_entered_at_quality``), and this
module turns one ledger row into the sentence the customer reads on the public
subscription page.

The two facts are kept apart on purpose:

* the STATE comes from the live panel read (what is true now);
* the MOMENT comes from the ledger (when EVE first saw it), and it is only ever
  shown when its quality says so. A baseline row - the first observation of an
  account that was already ended - has no evidence for "when", and printing the
  observation time would claim the customer ran out on the day EVE started looking.

The two can also DISAGREE: the page reads the panel now, the ledger was last written
by an earlier poll. `state_entered_at` then describes the beginning of a different
state (after a renewal it is the moment the account became ``active``), so it is used
only when the ledger's own state matches the one being displayed.

Quality vocabulary (written by ``panel.services.telemetry_state``):

* ``observed_transition`` - EVE saw the account active, then saw it ended;
* ``inferred_expiry``    - the expiry timestamp explains it (an expiry IS a moment);
* ``unknown_baseline``   - first sighting; do not claim a time.

Pure: no DB, no clock, and the date formatter is injected, so every quality is
testable without a panel or a freeze.
"""
from __future__ import annotations

from datetime import datetime, timezone

#: Only these two states have an "it ended" moment worth telling a customer about.
ENDED_STATES = ('volume_ended', 'expired')

QUALITY_OBSERVED = 'observed_transition'
QUALITY_INFERRED = 'inferred_expiry'
QUALITY_UNKNOWN = 'unknown_baseline'

#: Earliest timestamp treated as a real date (a 0/None expiry is "unlimited").
_SANE_AFTER = datetime(2000, 1, 1)


def _as_utc(moment):
    """A naive UTC datetime (the convention every ``*_at`` column follows)."""
    return moment.replace(tzinfo=None) if getattr(moment, 'tzinfo', None) else moment


def _from_expiry_ms(expiry_ms, now):
    try:
        moment = datetime.fromtimestamp(
            int(expiry_ms) / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if moment < _SANE_AFTER:
        return None
    if now is not None and moment > now:
        # An expiry in the future cannot explain an account that has ended.
        return None
    return moment


def ended_moment(*, state_key, ledger_state=None, state_entered_at=None, quality=None,
                 expiry_ms=None, now=None) -> tuple:
    """Return ``(moment, quality)`` for a subscription that ended, else ``(None,
    'unknown_baseline')``.

    ``expired`` uses the expiry timestamp itself: an expiry is the moment the state
    began, and it is exact. Only when no usable expiry exists does a
    ``state_entered_at`` from an ``observed_transition`` stand in for it.
    ``volume_ended`` has no such timestamp upstream, so it needs an observed
    transition; anything else is honestly unknown.

    ``ledger_state`` is the state the LEDGER believes this account is in. It is a
    different fact from the live ``state_key`` and the two can disagree: the page
    reads the panel now, while the ledger was last written by an earlier poll. When
    they disagree, ``state_entered_at`` describes the beginning of some OTHER state
    (for example the moment the account became ``active`` after a renewal), and
    presenting it as "when your data ran out" is a false claim. The timestamp is
    therefore only used when the ledger is in the same state being displayed.
    """
    state_key = str(state_key or '').strip().lower()
    quality = str(quality or '').strip().lower()
    ledger_state = str(ledger_state or '').strip().lower()
    entered = _as_utc(state_entered_at) if isinstance(state_entered_at, datetime) else None
    same_state = bool(ledger_state) and ledger_state == state_key
    observed = (entered if (entered is not None and same_state
                            and quality == QUALITY_OBSERVED) else None)
    now = _as_utc(now) if isinstance(now, datetime) else None

    if state_key not in ENDED_STATES:
        return None, QUALITY_UNKNOWN
    if state_key == 'expired':
        inferred = _from_expiry_ms(expiry_ms, now)
        if inferred is not None:
            return inferred, QUALITY_INFERRED
        if observed is not None:
            return observed, QUALITY_OBSERVED
        return None, QUALITY_UNKNOWN
    if observed is not None:
        return observed, QUALITY_OBSERVED
    return None, QUALITY_UNKNOWN


def _split_stamp(stamp):
    """``"1404/07/28 14:30"`` -> ``("1404/07/28", "14:30")``."""
    text = str(stamp or '').strip()
    if not text:
        return None, None
    date_part, _sep, time_part = text.rpartition(' ')
    if not date_part:
        return text, None
    return date_part, time_part


def notice_text(*, state_key, moment, lang, formatter) -> str | None:
    """The customer sentence for one ended moment, or None when it cannot be built."""
    if moment is None or formatter is None:
        return None
    state_key = str(state_key or '').strip().lower()
    if state_key not in ENDED_STATES:
        return None
    date_part, time_part = _split_stamp(formatter(moment))
    if not date_part:
        return None
    is_fa = str(lang or '').strip().lower() == 'fa'
    if is_fa:
        when = (f"در تاریخ {date_part} ساعت {time_part}" if time_part
                else f"در تاریخ {date_part}")
        return (f"حجم شما {when} تمام شد" if state_key == 'volume_ended'
                else f"اشتراک شما {when} منقضی شد")
    when = f"on {date_part} at {time_part}" if time_part else f"on {date_part}"
    return ("Your data ran out %s" % when if state_key == 'volume_ended'
            else "Your subscription expired %s" % when)


def build_notice(*, state_key, ledger_state=None, state_entered_at=None, quality=None,
                 expiry_ms=None, lang='en', formatter=None, now=None):
    """One call for the subscription page: ``{'text', 'moment', 'quality'}`` or None.

    Returns None whenever a customer should not read a time: the subscription is not
    in an ended state (a successful renewal is back to ``active``, which is exactly
    how the notice disappears), the ledger cannot prove when it ended, or the ledger
    is describing a DIFFERENT state than the one on screen (see ``ended_moment``).
    """
    moment, resolved_quality = ended_moment(
        state_key=state_key, ledger_state=ledger_state,
        state_entered_at=state_entered_at, quality=quality,
        expiry_ms=expiry_ms, now=now)
    text = notice_text(state_key=state_key, moment=moment, lang=lang,
                       formatter=formatter)
    if not text:
        return None
    return {'text': text, 'moment': moment, 'quality': resolved_quality}
