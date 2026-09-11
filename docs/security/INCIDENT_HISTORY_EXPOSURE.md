# Incident: database and receipt artifacts committed to git history

Status: **working tree contained; history not yet purged**.
Credential rotation is a mandatory human action (see below).

## What happened

| Path | Added | Removed from tracking |
|------|-------|-----------------------|
| `instance/servers.db` | `535f7a8` | `65acfec` |
| `instance/backups/backup_20251206_210932.db` | `4bedabf` | `ff380ad` |
| `instance/receipts/2025/12/*.png|*.jpg` | same window | `ff380ad` |

Removing a file from the latest commit does **not** remove it from git history:
all of the above remain reachable and can be extracted with `git cat-file`.

## Impact assessment

The two database blobs were extracted and inspected **without printing any
secret value**. They contained:

- one X-UI server row whose `password` column was still **plaintext**
  (pre-`enc:v1:` era) — this is a live-style panel credential;
- two admin password hashes;
- one bank-card row, two manual receipts, eleven client-ownership rows and ten
  transactions;
- two customer receipt images (potential PII).

Full-history scans with **gitleaks 8.30.1** (`--log-opts=--all`, 889 commits)
and **TruffleHog 3.97.4** found no other leaks. gitleaks reported one hit: a
deterministic, non-secret WireGuard test vector in
`tests/test_custom_subscriptions.py` (commit `a300481e`) that the current code
already generates at runtime; it is allowlisted in `.gitleaks.toml`.

## Required remediation (human action)

1. **Rotate the X-UI panel credential** for the leaked server and revoke any
   API token for it. Deleting the blob is not sufficient.
2. **Force a password reset** for the two admin accounts, then enable MFA
   (Phase 3).
3. If the leaked card/receipt data belongs to real customers, follow the
   notification steps in `docs/security/INCIDENT_RESPONSE.md`.
4. If there is any doubt whether the leaked DB predates the encryption keys,
   rotate `SERVER_PASSWORD_KEY`, `SESSION_SECRET` and `EVE_BACKUP_KEY`.
5. Purge history (runbook below) and force-push after coordinating with every
   clone/CI consumer.

## Prevention (implemented in Phase 2)

- `.gitignore` already covers `instance/`, `runtime/`, `*.db`, `*.sqlite*`.
- `scripts/check_tracked_artifacts.py` fails on any tracked — or
  newly-added-in-this-change — database, backup, key, `.env` or `*.bak`
  artifact.
- The `forbidden-artifacts` job in `.github/workflows/security.yml` runs it as
  a blocking check; `tests/test_ci_guards.py` unit-tests the matcher.

## History purge runbook

`git filter-repo` (preferred) or BFG. Always work on a fresh mirror clone and
coordinate the force-push:

```bash
# 1. Fresh mirror clone
git clone --mirror https://github.com/aibedini/eve.git eve-mirror.git
cd eve-mirror.git

# 2. Remove the offending paths from every commit
git filter-repo --force \
  --path instance/servers.db \
  --path instance/backups \
  --path-glob 'instance/receipts/*' \
  --invert-paths

# 3. Re-add the origin and push every ref
git remote add origin https://github.com/aibedini/eve.git
git push --force --all
git push --force --tags
```

After the purge:

- every developer must re-clone (an existing clone keeps the old objects);
- cached fork objects may persist on GitHub until support garbage-collects,
  which is exactly why rotation is mandatory and purging alone is not enough.
