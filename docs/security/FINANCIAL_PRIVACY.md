# Financial data privacy

## Rule

A stored destination financial identifier (bank card number, IBAN, or account
number) is never returned in full by a list or detail API. Sender cards are an
intentional operator-facing exception: authenticated, row-scoped finance and
renewal APIs return them in full so an operator can reconcile the payment with
the originating card. Sender cards remain encrypted at rest and must never be
written to logs, audit metadata, URLs, or metrics.

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
- panel/models/finance.py: Payment.to_dict() and Transaction.to_dict() return the
  complete sender_card to their existing authenticated, row-scoped consumers,
  while destination-card summaries stay masked.
- panel/routes/finance.py: transaction and payment lists return the complete
  sender_card after applying their existing owner/role filters.
- panel/routes/clients.py: the authenticated last-renewal payload returns the
  complete sender_card after applying reseller ownership filtering.

## Mask round-trip guard

Edit forms are pre-filled from the masked payload, so a client can post the mask
back. The update paths ignore a submission that equals the mask of the stored
value (panel.core.finance_privacy.is_masked_value) instead of persisting it:

- PUT /api/bank-cards/<id> (card_number, iban, account_number)
- PUT /api/transactions/<id> and the payment-to-expense conversion (sender_card)
- PUT /api/payments/<id> (sender_card)

The finance payment form receives and edits the complete sender card. The legacy
masked-value guards remain compatible with older clients that may still submit a
previously cached masked value.

## Reveal endpoints

| Endpoint | Permission | Step-up | Scope |
|----------|------------|---------|-------|
| POST /api/bank-cards/<id>/reveal | bank.reveal | bank.reveal | central/own/assigned card, or superadmin |
| POST /api/payments/<id>/reveal | finance.manage | finance.manage | payment owner or superadmin |
| POST /api/transactions/<id>/reveal | finance.manage | finance.manage | transaction owner or superadmin |

These compatibility endpoints remain rate limited to 20 requests per minute and write an AuditLog row
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

tests/test_financial_privacy.py covers destination masking, complete sender-card
visibility in scoped serializers/list/last-renewal APIs, the round-trip guard,
and the compatibility reveal endpoints (permission, scope, 404, audit row).
