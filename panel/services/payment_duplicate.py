"""Fast, scoped lookup for recently declared sender card digits."""

from datetime import datetime, timedelta
from unicodedata import digit

from panel.extensions import db
from panel.models import Payment, SystemSetting, Transaction


SETTING_KEY = 'payment_duplicate_check_enabled'


def duplicate_check_enabled():
    setting = db.session.get(SystemSetting, SETTING_KEY)
    return bool(setting and setting.value == '1')


def last_four(value):
    digits = []
    for char in str(value or ''):
        try:
            digits.append(str(digit(char)))
        except (TypeError, ValueError):
            continue
    return ''.join(digits[-4:]) if len(digits) >= 4 else None


def recent_sender_card_matches(card_id, sender_card, *, admin_id=None, now=None):
    """Return matching declarations made in the preceding 24 hours, newest first."""
    suffix = last_four(sender_card)
    if not suffix:
        return []
    cutoff = (now or datetime.utcnow()) - timedelta(hours=24)
    payments = Payment.query.filter(
        Payment.card_id == card_id,
        Payment.created_at >= cutoff,
    )
    transactions = Transaction.query.filter(
        Transaction.card_id == card_id,
        Transaction.created_at >= cutoff,
        Transaction.category == 'income',
        Transaction.amount > 0,
    )
    if admin_id is not None:
        payments = payments.filter(Payment.admin_id == admin_id)
        transactions = transactions.filter(Transaction.admin_id == admin_id)

    matches = []
    for row in payments.with_entities(
        Payment.id, Payment.sender_card, Payment.sender_name, Payment.client_email,
        Payment.amount, Payment.created_at,
    ):
        if last_four(row.sender_card) == suffix:
            matches.append({
                'source': 'payment', 'id': row.id,
                'user': row.client_email or row.sender_name or '—',
                'amount': row.amount, 'created_at': row.created_at,
            })
    for row in transactions.with_entities(
        Transaction.id, Transaction.sender_card, Transaction.sender_name,
        Transaction.client_email, Transaction.amount, Transaction.created_at,
    ):
        if last_four(row.sender_card) == suffix:
            matches.append({
                'source': 'transaction', 'id': row.id,
                'user': row.client_email or row.sender_name or '—',
                'amount': row.amount, 'created_at': row.created_at,
            })
    matches.sort(key=lambda item: item['created_at'], reverse=True)
    return matches
