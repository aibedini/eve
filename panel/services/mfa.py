"""Time-based one-time passwords (RFC 6238) and MFA backup codes.

Implemented with the standard library only: HMAC-SHA1 HOTP plus a small
verification window. Secrets are stored with the application Fernet key via
EncryptedText; backup codes are stored as domain-separated HMAC digests so a
database leak does not reveal usable codes.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

TOTP_DIGITS = 6
TOTP_PERIOD = 30
TOTP_DEFAULT_WINDOW = 1  # accept current step +/- 1 (clock drift)


def generate_totp_secret() -> str:
    """Return a new base32 TOTP secret (160 bits, RFC 4226 recommendation)."""
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _decode_secret(secret) -> bytes:
    raw = str(secret or '').strip().replace(' ', '').upper()
    if not raw:
        raise ValueError('empty TOTP secret')
    padded = raw + '=' * (-len(raw) % 8)
    return base64.b32decode(padded)


def hotp(key: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    message = int(counter).to_bytes(8, 'big')
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** digits)).zfill(digits)


def totp_code(secret, at_time: float | None = None, digits: int = TOTP_DIGITS,
              period: int = TOTP_PERIOD) -> str:
    """Return the TOTP for ``at_time`` (unix seconds, default now)."""
    moment = time.time() if at_time is None else float(at_time)
    return hotp(_decode_secret(secret), int(moment // period), digits)


def verify_totp(secret, code, *, at_time: float | None = None,
                window: int = TOTP_DEFAULT_WINDOW, last_counter: int | None = None,
                digits: int = TOTP_DIGITS, period: int = TOTP_PERIOD) -> tuple[bool, int | None]:
    """Verify a TOTP code and return ``(ok, counter)``.

    ``last_counter`` (the previously accepted step) makes replay impossible:
    a code at or below it is never accepted again.
    """
    candidate = str(code or '').strip().replace(' ', '')
    if not candidate.isdigit():
        return False, last_counter
    candidate = candidate.zfill(digits)
    key = _decode_secret(secret)
    moment = time.time() if at_time is None else float(at_time)
    current = int(moment // period)
    low = current - max(0, int(window))
    high = current + max(0, int(window))
    for counter in range(low, high + 1):
        if last_counter is not None and counter <= int(last_counter):
            continue
        if hmac.compare_digest(hotp(key, counter, digits), candidate):
            return True, counter
    return False, last_counter


def provisioning_uri(secret, account: str, issuer: str = 'Eve') -> str:
    label = quote(f'{issuer}:{account}', safe='')
    params = urlencode({
        'secret': secret,
        'issuer': issuer,
        'algorithm': 'SHA1',
        'digits': TOTP_DIGITS,
        'period': TOTP_PERIOD,
    })
    return f'otpauth://totp/{label}?{params}'


def generate_backup_codes(count: int = 10) -> list[str]:
    """Return fresh human-copyable recovery codes (normalized uppercase)."""
    return [secrets.token_hex(5).upper() for _ in range(max(1, int(count)))]


def normalize_backup_code(code) -> str:
    return str(code or '').strip().replace('-', '').replace(' ', '').upper()
