"""Masking helpers for financial identifiers.

Default API representations must never expose a full card number, IBAN or
account number; only the privileged reveal endpoints do, behind a permission,
step-up MFA and an audit event. These helpers are intentionally pure so models,
routes and templates can all share one masking rule.
"""


# Characters that mark a value as already masked. Re-masking an existing mask
# must be a no-op: folding 6037********5678 down to its digits would render it
# as the plausible-looking 60375678 and break mask round-trip comparisons.
_MASK_CHARS = ('*', 'x', 'X', '•')


def _digits(value) -> str:
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


def _already_masked(raw: str) -> bool:
    return any(char in raw for char in _MASK_CHARS)


def mask_card_number(value):
    """Return something like 6037********1234 (first 4 + last 4)."""
    raw = str(value or '').strip()
    if not raw:
        return None
    if _already_masked(raw):
        return raw
    digits = _digits(raw)
    if not digits:
        return '*' * len(raw)
    if len(digits) >= 8:
        return f"{digits[:4]}{'*' * (len(digits) - 8)}{digits[-4:]}"
    if len(digits) > 4:
        return f"{'*' * (len(digits) - 4)}{digits[-4:]}"
    return '*' * len(digits)


def mask_account_like(value):
    """Return the last four digits only (IBAN, account number, identifiers)."""
    raw = str(value or '').strip()
    if not raw:
        return None
    if _already_masked(raw):
        return raw
    digits = _digits(raw)
    if not digits:
        return '*' * len(raw)
    if len(digits) <= 4:
        return '*' * len(digits)
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def mask_iban(value):
    return mask_account_like(value)


def is_masked_value(submitted, stored, masker) -> bool:
    """True when a submitted value is just the masked form of the stored one.

    Edit forms that pre-fill with the masked value must not overwrite the real
    secret with the mask.
    """
    if submitted in (None, '') or stored in (None, ''):
        return False
    return str(submitted).strip() == str(masker(stored) or '').strip()
