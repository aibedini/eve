# CI required checks and release gating

## Workflows

| Workflow | Job (check name) | Blocking? | Purpose |
|----------|------------------|-----------|---------|
| `Security` | `codeql` | yes | CodeQL static analysis (Python) |
| `Security` | `secrets-and-dependencies` | yes | gitleaks full history + pip-audit + Trivy HIGH/CRITICAL |
| `Security` | `forbidden-artifacts` | yes | refuse tracked/added DB, backup, key, `.env` files |
| `Tests` | `Unit tests` | yes | focused security/wallet/backup/guard tests |
| `Tests` | `Integration tests` | yes | full `pytest` suite |
| `Docker` | `publish` | build | image build + offline tar |
| `Build Offline Bundle` | `build` | release | offline bundle and release assets |

## Enable them as required checks (repository admin, one-time)

Branch protection is a repository setting, not a file; it must be applied by an
admin. For `main`:

1. Settings → Branches → Add branch protection rule (or edit the existing one).
2. Enable **Require status checks to pass before merging**.
3. Select: `codeql`, `secrets-and-dependencies`, `forbidden-artifacts`,
   `Unit tests`, `Integration tests`, `publish`, `build`.
4. Enable **Require branches to be up to date before merging**.
5. Do not allow bypassing the rule for the release account.

Equivalent via the API (requires an admin token):

```bash
gh api -X PUT repos/aibedini/eve/branches/main/protection \
  -H "Accept: application/vnd.github+json" \
  -f required_status_checks[strict]=true \
  -f 'required_status_checks[contexts][]=codeql' \
  -f 'required_status_checks[contexts][]=secrets-and-dependencies' \
  -f 'required_status_checks[contexts][]=forbidden-artifacts' \
  -f 'required_status_checks[contexts][]=Unit tests' \
  -f 'required_status_checks[contexts][]=Integration tests' \
  -f 'required_status_checks[contexts][]=publish' \
  -f 'required_status_checks[contexts][]=build' \
  -f enforce_admins=true
```

## Release gate

A release is only allowed when, on the exact release commit:

- `Security` is green (CodeQL, gitleaks, pip-audit, Trivy, forbidden-artifacts);
- `Tests` is green (unit + integration);
- `Docker` built the image successfully;
- `pip-audit`/Trivy report no HIGH/CRITICAL for pinned dependencies.

Do not create a tag or GitHub release while any of these is red, even if the
failure looks unrelated to the change.

This is enforced in code: the `preflight` job in `offline-bundle.yml` runs on
every tag push and queries the GitHub API for the `Security` and `Tests`
conclusions on that exact commit. If either is not `success` (including
`missing`), the release build is refused before any artifact or release asset is
produced. Non-tag `workflow_dispatch` builds skip the gate.

## Trivy failure history (fixed)

The `secrets-and-dependencies` job failed for five consecutive pushes because
the composite `aquasecurity/trivy-action` step aborted in 2–4 s even on a cold
cache — far too fast to download a vulnerability DB. Dependency scans
(`pip-audit` locally, OSV for `requirements.lock` and `bnqo/Cargo.lock`) show
**zero** known vulnerabilities, so the failure was tooling rather than
findings. The composite action passes `token: ${{ inputs.token-setup-trivy }}`
to `setup-trivy` even when the caller leaves it empty, which overrides
`setup-trivy`'s own default and breaks release resolution.

The job now installs a **pinned** Trivy version through `aquasecurity/setup-trivy`
with an explicit `token: ${{ secrets.GITHUB_TOKEN }}` and runs the `trivy` CLI
directly, so a failure is attributed to a visible command instead of an opaque
composite step.
