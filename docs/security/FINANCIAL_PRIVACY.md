# Financial data privacy

## Rule

A stored financial identifier (card number, IBAN, account number, transaction or
payment sender card) is never returned in full by a list or detail API. The
default representation carries the masked form only; the real value is available
exclusively through an audited, permission- and step-up-gated reveal route.

## Masking format (panel/core/finance_privacy.py)

| Value | Mask |
|-------|------|
| Card number (PAN) | first 4 + last 4, e.g. 6037********5678 |
| IBAN / account number | last 4 only |
| Value shorter than the mask window | fully masked, never a partial number |

Masking is idempotent: a value that already contains a mask character (*, x, X)
is returned unchanged, so re-rendering a legacy mask cannot collapse it into a
plausible-looking short number (6037********5678 must not become 60375678).

## Where masking is applied

- panel/models/core.py: BankCard.to_dict() returns masked card_number, iban and
  account_number, plus masked_card, and revealed: False. BankCard.to_reveal_dict()
  is the only serializer that returns the real values.
- panel/models/finance.py: Payment.to_dict() and Transaction.to_dict() mask
  sender_card and expose only a masked destination-card summary.
- panel/routes/finance.py: the transactions list, the payments list and the
  per-client transaction history mask sender_card.
- panel/routes/clients.py: the last-renewal payload masks sender_card.

## Mask round-trip guard

Edit forms are pre-filled from the masked payload, so a client can post the mask
back. The update paths ignore a submission that equals the mask of the stored
value (panel.core.finance_privacy.is_masked_value) instead of persisting it:

- PUT /api/bank-cards/<id> (card_number, iban, account_number)
- PUT /api/transactions/<id> and the payment-to-expense conversion (sender_card)
- PUT /api/payments/<id> (sender_card)

The finance payment form keeps the sender-card input empty and shows the mask as
a placeholder, and omits the field entirely when it is blank, so saving a form
without retyping the card cannot overwrite or clear the stored value.

## Reveal endpoints

| Endpoint | Permission | Step-up | Scope |
|----------|------------|---------|-------|
| POST /api/bank-cards/<id>/reveal | bank.reveal | bank.reveal | central/own/assigned card, or superadmin |
| POST /api/payments/<id>/reveal | finance.manage | finance.manage | payment owner or superadmin |
| POST /api/transactions/<id>/reveal | finance.manage | finance.manage | transaction owner or superadmin |

Each is rate limited to 20 requests per minute, writes an AuditLog row
(bank_card.reveal, payment.sender_card_reveal, transaction.sender_card_reveal)
and returns the value only in the response body. The revealed number is never
written to the audit row, a log line, a metric label, a URL or an error message.

## Deliberate exceptions

- Customer-facing payment instructions (Telegram bot) read the BankCard attribute
  directly, because the customer must be told where to send the money. Only the
  API/serializer surface is masked.
- The operator-facing Telegram bot messages may fall back to the card number when
  a card has no label; that message goes to the card owner, not to a customer.
- Legacy compatibility: encryption (domains, enc:v<N>: envelopes) is unchanged.
  This phase only changes what leaves the process over HTTP.

## Tests

tests/test_financial_privacy.py covers the helpers, the serializers, the list
endpoints (raw-body assertion that the full number is absent), the round-trip
guard, and the reveal endpoints (permission, scope, 404, audit row).
