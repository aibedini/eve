# Release security

## Problem

A release is the moment several invariants have to hold at once, and nothing
checked them together. The tag workflow published an image from any `v*` tag
without verifying the version format, the hash-pinned dependency lock, the CI
scanners, the image user or the disclosure policy, and the published image carried
no machine-readable inventory of what was inside it.

## Change

`scripts/release_check.py` (new) is the machine-checked checklist. It is
stdlib-only, needs no network, and runs in about a second:

| check | required | what it verifies |
|-------|----------|------------------|
| app_version | yes | `APP_VERSION` in app.py is `x.y.z` |
| version_docs | release profile | CHANGELOG.md (and RELEASE_NOTES.md) name the version being released; on every commit the same check is informational because notes are cut on an explicit release |
| requirements_lock | yes | every entry in requirements.lock carries a `--hash=sha256:` |
| requirements_ranges | note | requirements.txt ranges are resolved by the lock (informational) |
| ci_workflows | yes | security.yml, tests.yml and docker-publish.yml exist and security.yml wires CodeQL, gitleaks, pip-audit, Trivy and the forbidden-artifact job |
| dockerfile | yes | a non-root `USER`, `pip install --require-hashes`, no `:latest` base image (a tag-pinned base is reported as a note) |
| security_policy | yes | SECURITY.md describes private vulnerability disclosure |
| tracked_secret_files | yes | `git ls-files` contains no `.env`, key, certificate, keystore or database file (`.example`/`.sample` files are allowed) |
| image_attestations | yes | docker-publish.yml builds `sbom: true` and `provenance: true` |

`--profile ci` (default) is what runs on every push; `--profile release` adds the
changelog/release-notes requirement and is what the tag flow runs. `--json` emits
the full report.

`.github/workflows/docker-publish.yml` gained a `release-guard` job that runs the
script (release profile on tags, ci profile otherwise) and `publish` now
`needs: [release-guard]`, so a non-compliant tree cannot publish an image. The
image build now sets `sbom: true` and `provenance: true` with the
`id-token: write` and `attestations: write` permissions those attestations need.

## Verifying a published image

    gh attestation verify oci://ghcr.io/aibedini/eve:sha-<commit> --owner aibedini

That checks the build provenance. The SBOM attestation carries the dependency
inventory for the same digest.

## Human steps that stay human

The automated guard cannot decide these; they are listed so a release does not
silently skip them:

1. Confirm the version bump: a release raises the minor version and resets the
   patch, updates CHANGELOG.md and RELEASE_NOTES.md, and only then gets a tag.
2. Rotate anything in `docs/security/INCIDENT_RESPONSE.md` that is still pending
   from an earlier incident; the guard only detects *tracked* secrets, not
   exposed ones.
3. After publishing, verify the digest and the attestation, then roll the
   deployment forward with `eve update`; keep the previous image digest for a
   rollback.
4. Rollback is a re-deploy of the previous digest plus the matching database
   backup; migrations are additive, but a downgrade across a schema change needs
   the backup, not `alembic downgrade` on production data.

## Verification

`tests/test_release_security.py` (18 tests): the version format, the
release-profile changelog requirement and the ci-profile tolerance, a lockfile
entry without a hash, a fully hashed lock, an empty lock, range reporting, missing
workflows, a missing scanner, a complete workflow set, non-root/hash-pinned/latest
Dockerfile rules, the disclosure-policy requirement, the attestation flags, a
tracked `server.key` in a throwaway git repository, the secret-path allowlist,
and an end-to-end run of the CLI on the real tree (ci profile, exit 0, no failed
checks) plus a guard that the publish workflow still gates on the check and still
attests the image.

## Residual risk

* The guard checks the repository, not the runtime: it cannot prove a deployment
  runs the digest it claims (that is what attestation verification is for) or that
  a live host is patched.
* pip-audit and Trivy run in CI, not in this script, so a vulnerable dependency is
  caught by the Security workflow rather than by the release guard. The guard
  would pass a tree whose lock is hash-pinned but outdated.
* A tag-pinned base image (`python:3.11-slim-bookworm`) is accepted and reported as
  a note; digest pinning would be stronger but has to be refreshed deliberately.
