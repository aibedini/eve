# GMweb Contract v5 rollout and rollback

Contract v5 adds carrier delivery evidence without changing the meaning of
gateway acceptance or device submission. Signed callbacks remain authoritative;
delivery-event search is read-only reconciliation.

## Safe rollout order

1. Back up Eve's database and deploy the Eve migration. The new columns are
   nullable, so existing Contract v4 traffic remains valid.
2. Deploy Eve v5 consumer code. Against GMweb v4, ordinary send/status/callback
   behavior continues; carrier search reports capability unavailable.
3. Merge and deploy the reviewed GMweb Contract v5 provider change.
4. Issue an Eve project key with `sms.status` and `transport:read`; never place a
   master token in Eve.
5. Configure the shared callback secret (at least 32 random characters) and the
   HTTPS callback URL on GMweb.
6. Verify transport health, one queued-to-submitted message, one authenticated
   carrier report, its signed callback, and the same event in reconciliation.

## Operational checks

- Submission and carrier columns remain independent.
- Duplicate DLRs do not create duplicate callbacks; conflicting reuse of an
  event ID returns 409.
- Reconciliation reports `mutated_local_events: 0`.
- A historical callback dead letter is visible but does not override current
  transport readiness.
- No response or log contains an API key, SMS body, or full recipient.

## Rollback

Roll back application code before downgrading the database. Contract v4 GMweb
remains compatible with the v5 Eve schema because all evidence columns are
nullable. If schema downgrade is necessary, stop Eve workers, take another
backup, run Alembic down one revision, and start the previous Eve version.
Downgrade removes v5-only evidence columns and indexes, so export any carrier
audit data that must be retained first.

## Not proven by deployment alone

A successful rollout does not prove a real handset delivery. Record separate
evidence for the real Android build, SIM/modem, carrier receipt behavior,
staging end-to-end callback path, and production GMweb configuration.
