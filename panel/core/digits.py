"""Persian digits for customer-facing strings.

The panel UI mixes both digit sets on purpose: operator screens keep the Latin
digits every template already renders, while the public subscription page is the
one place a customer reads a clock ("23 ساعت و 45 دقیقه"). Converting only where
those strings are produced keeps the difference explicit instead of rewriting
every table in the panel.

The inverse table already exists in ``app._DIGIT_TRANSLATION`` (Persian -> Latin,
used when parsing operator input); this is the display direction.
"""
from __future__ import annotations

_LATIN_TO_PERSIAN = str.maketrans({
    '0': '\u06f0', '1': '\u06f1', '2': '\u06f2', '3': '\u06f3', '4': '\u06f4',
    '5': '\u06f5', '6': '\u06f6', '7': '\u06f7', '8': '\u06f8', '9': '\u06f9',
})


def to_persian_digits(value) -> str:
    """Return ``value`` as text with every ASCII digit written in Persian digits.

    Non-digits (including a decimal point, a slash or a colon) are left alone, so
    ``1404/07/28 14:30`` keeps its shape. ``None`` becomes an empty string: callers
    pass it straight into a template.
    """
    if value is None:
        return ''
    return str(value).translate(_LATIN_TO_PERSIAN)
