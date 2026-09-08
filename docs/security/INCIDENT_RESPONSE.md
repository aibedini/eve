# Git Credential and Data Exposure Runbook

The repository history previously contained SQLite database snapshots. Assume
their credentials and personal/financial records were copied.

1. Restrict repository access and preserve audit logs and the affected commit
   identifiers.
2. Rotate X-UI passwords/API tokens, administrator passwords and sessions,
   Telegram/SMS/WhatsApp tokens, database credentials, and any exposed payment
   identifiers. Notify affected data owners as required by applicable law.
3. Back up the current remote refs. Coordinate a maintenance window with every
   contributor and deployment that clones the repository.
4. Use `git filter-repo` to remove `instance/servers.db`,
   `instance/backups/backup_20251206_210932.db`, and any other generated database
   artifacts from all refs. Re-run Gitleaks and TruffleHog against all history.
5. Force-push the rewritten branches/tags only after rotation. Delete stale
   forks/caches where possible; require everyone to delete old clones and clone
   again. Never merge an old branch back into clean history.
6. Record the response timeline and verify that CI blocks future database and
   secret commits.

History rewriting is destructive and is intentionally not performed by the
application or an unattended update.
