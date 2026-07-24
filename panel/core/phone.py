"""Phone number normalization helpers (Iranian-first, international optional)."""
import re

__all__ = [
    'normalize_iran_mobile',
    'normalize_international_phone',
]


def _normalize_ascii_digits(value: str | None) -> str:
    val = str(value or '')
    table = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
    return val.translate(table)


def normalize_iran_mobile(value: str | None) -> str:
    """Return an Iranian mobile in canonical ``989xxxxxxxxx`` form.

    Accepts Persian/Arabic digits, common 09/9/+98/0098 prefixes, separators,
    and mobile numbers embedded in client labels. Returns an empty string when
    no complete Iranian mobile number is present.
    """
    if not value:
        return ''
    text_value = _normalize_ascii_digits(value)
    separator = r'[\s().\-]*'
    boundary = r'(?<![0-9A-Za-z؀-ۿ])'
    patterns = (
        boundary + r'\+98' + separator + r'(9(?:' + separator + r'\d){9})(?!\d)',
        boundary + r'0098' + separator + r'(9(?:' + separator + r'\d){9})(?!\d)',
        boundary + r'98' + separator + r'(9(?:' + separator + r'\d){9})(?!\d)',
        boundary + r'0' + separator + r'(9(?:' + separator + r'\d){9})(?!\d)',
        boundary + r'(9(?:' + separator + r'\d){9})(?!\d)',
    )
    for pattern in patterns:
        match = re.search(pattern, text_value)
        if match:
            subscriber = re.sub(r'\D', '', match.group(1))
            if len(subscriber) == 10 and subscriber.startswith('9'):
                return f'98{subscriber}'

    compact = re.sub(r'\D', '', text_value)
    for pattern in (r'(?<!\d)0098(9\d{9})(?!\d)', r'(?<!\d)98(9\d{9})(?!\d)',
                    r'(?<!\d)0(9\d{9})(?!\d)', r'(?<!\d)(9\d{9})(?!\d)'):
        match = re.search(pattern, compact)
        if match:
            return f'98{match.group(1)}'
    return ''


def normalize_international_phone(value: str | None) -> str:
    """Return a non-Iranian phone number as bare digits (E.164 without '+').

    Accepts Persian/Arabic digits, an optional leading '+' or '00' prefix, and
    common separators. Requires 8-15 digits total; returns an empty string
    otherwise. Callers must try ``normalize_iran_mobile`` first so Iranian
    numbers keep their canonical ``989xxxxxxxxx`` form.
    """
    if not value:
        return ''
    digits = re.sub(r'\D', '', _normalize_ascii_digits(value))
    if digits.startswith('00'):
        digits = digits[2:]
    if not 8 <= len(digits) <= 15:
        return ''
    return digits


def _normalize_contact_phone(value: str | None, allow_international: bool = False) -> str:
    """Canonicalize a contact phone, Iranian first, international when allowed."""
    canonical = normalize_iran_mobile(value)
    if canonical or not allow_international:
        return canonical
    return normalize_international_phone(value)


def _extract_iran_mobile_from_text(value: str | None, *extra_sources: str | None) -> str:
    """Extract first valid Iranian mobile from value, then extra_sources in order.

    Rules:
    - 09XXXXXXXXX  : must NOT be preceded by any letter or digit.
                     '1097' → no match (digit before 0).
                     'plus09...' → no match (letter before 0).
                     'user_09...' → match (underscore/separator before 0 is OK).
    - +98XXXXXXXXX : literal '+' required; '+' must not be preceded by a letter/digit.
    - 0098XXXXXXXXX: double-zero form; same prefix rule.
    - Spaces/dashes between digit groups are allowed (e.g. '0912 833 4643').
    """
    SEP = r'[\s\-]?'
    # Lookbehind: reject if immediately preceded by any letter (ASCII or Persian/Arabic) or digit.
    _LB = r'(?<![0-9A-Za-z؀-ۿ])'
    _PATTERNS = [
        _LB + r'\+98'  + SEP + r'(9(?:' + SEP + r'\d){9})(?!\d)',  # +98...
        _LB + r'0098'  + SEP + r'(9(?:' + SEP + r'\d){9})(?!\d)',  # 0098...
        _LB + r'0'     + SEP + r'(9(?:' + SEP + r'\d){9})(?!\d)',  # 09...
        _LB +                  r'(9(?:' + SEP + r'\d){9})(?!\d)',  # bare 9...
    ]

    def _try(text: str | None) -> str:
        if not text:
            return ''
        t = _normalize_ascii_digits(text)
        for pat in _PATTERNS:
            m = re.search(pat, t)
            if m:
                digits = re.sub(r'[^\d]', '', m.group(1))
                if len(digits) == 10 and digits.startswith('9'):
                    return f"+98{digits}"
        compact = re.sub(r'[^\d]', '', t)
        m = re.search(r'09\d{9}', compact)
        if m:
            return f"+98{m.group(0)[1:]}"
        m = re.search(r'98(9\d{9})', compact)
        if m:
            return f"+98{m.group(1)}"
        return ''

    for src in (value, *extra_sources):
        r = _try(src)
        if r:
            return r
    return ''
