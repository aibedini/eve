# MFA, sessions and step-up authentication

## Factors

| Factor | Status | Notes |
|--------|--------|-------|
| TOTP (RFC 6238) | **implemented** | stdlib HMAC-SHA1, 6 digits / 30 s, ±1 step window |
| Backup codes | **implemented** | 10 single-use codes, stored as keyed HMAC digests |
| WebAuthn / passkeys | **planned (next increment)** | schema and flow are factor-agnostic; tracked as Phase 3b |

The TOTP secret is stored through `EncryptedText` (the application Fernet key),
so a database dump does not reveal it. The last accepted TOTP step is persisted
and every candidate at or below it is rejected, which makes a captured code
non-replayable.

## Login flow

```
password
  ├─ superadmin without MFA  → /mfa/setup (enrol TOTP, confirm) → session
  ├─ MFA enrolled            → /mfa (verify TOTP or backup code) → session
  └─ MFA not required        → session
```

No `admin_id` is written to the cookie until MFA succeeds. A password-only
check leaves a short-lived (5 min) pending state that can only reach the MFA
endpoints. Failing to complete MFA never grants a session.

Enforcement is controlled by `EVE_MFA_REQUIRED_ROLES` (default
`superadmin`; set to `none` to disable). Every login, MFA success/failure,
enrolment and step-up is written to the audit log.

## Server-side sessions

The signed cookie carries only an opaque token (`session_token`). The
`admin_sessions` row is authoritative for timeouts, MFA state, step-up and
revocation, so a stolen cookie can be revoked from another device and privileged
roles expire quickly.

| Role | Idle timeout | Absolute lifetime |
|------|--------------|-------------------|
| superadmin / is_superadmin | 45 min | 12 h |
| admin | 4 h | 72 h |
| reseller | 12 h | 168 h |

Overridable per role with `EVE_SESSION_IDLE_MINUTES_<ROLE>` and
`EVE_SESSION_ABSOLUTE_HOURS_<ROLE>`. This replaces the previous 7-day cookie
lifetime as the effective control.

Endpoints:

- `GET  /api/sessions` — list active/all sessions with IP, user agent, created,
  last seen and expiry; marks the current one.
- `POST /api/sessions/<id>/revoke` — revoke one session.
- `POST /api/sessions/revoke-others` — revoke every other session.
- `GET  /logout` — revokes the current registry row.

## Step-up authentication

Sensitive mutations require a fresh MFA check (`EVE_STEP_UP_TTL_SECONDS`,
default 600 s). The endpoint `POST /api/mfa/step-up` accepts a TOTP or backup
code; when the proof is stale the guarded endpoint answers
`403 {"code": "step_up_required"}`.

Guarded today: system update start (`system.update`), backup restore
(`backups.restore`), admin create/update/delete (`admins.manage`) and
security/SSL/session settings (`settings.write`). Bank-data reveal and secret
management are guarded when Phases 5/6 land.

### Rolling-deploy note

`step_up_required` and the registry timeouts are enforced for
registry-backed sessions, i.e. every login created by this version. Sessions
that predate the registry are grandfathered until they expire; the test suite
seeds the Flask session directly and therefore also takes the legacy path. Do
not re-introduce direct `session['admin_id'] = ...` writes outside tests.
