# Eve — Enterprise X-UI Operations Platform

Eve is a multi-tenant control plane for teams operating Sanaei 3X-UI and
Alireza X-UI infrastructure at scale. It unifies panel management, client
lifecycle operations, reseller commerce, customer messaging, observability,
and secure day-two operations in one responsive dashboard.

[![Tests](https://github.com/aibedini/eve/actions/workflows/tests.yml/badge.svg)](https://github.com/aibedini/eve/actions/workflows/tests.yml)
[![Security](https://github.com/aibedini/eve/actions/workflows/security.yml/badge.svg)](https://github.com/aibedini/eve/actions/workflows/security.yml)
[![Docker](https://github.com/aibedini/eve/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/aibedini/eve/actions/workflows/docker-publish.yml)

> Reliable, auditable VPN operations with first-class Persian/Jalali
> localization and Asia/Tehran timezone support.

## Deploy EVE

Choose the deployment model that fits your environment:

### Ubuntu/Debian installer

```bash
bash <(curl -Ls https://raw.githubusercontent.com/aibedini/eve/main/setup.sh)
```

Review the installer and configure production secrets before exposing Eve to a
network.

### Docker and offline bundles

```bash
bash scripts/docker/build-offline-bundle.sh
```

See [DOCKER.md](DOCKER.md) and [OFFLINE_INSTALL.md](OFFLINE_INSTALL.md) for
connected, restricted, and transferable image workflows.

## What Eve does

- Operates unlimited Sanaei 3X-UI and Alireza X-UI panels from one dashboard.
- Manages servers, inbounds, clients, subscriptions, traffic, QR links, and
  safe client mutations.
- Provides reseller wallets, packages, custom tariffs, ownership rules,
  server visibility, receipts, and financial statements.
- Automates SMS, WhatsApp, and Telegram purchase, trial, support, and delivery
  workflows with cooldowns, quiet hours, pacing, and audit history.
- Monitors panel reachability, usage, workers, database, disk, static assets,
  and operational drift through dashboards, SSE, and Doctor diagnostics.
- Runs on connected, restricted, or offline networks with native or Docker
  deployment paths.

## Enterprise capabilities

### X-UI compatibility and client lifecycle

- Version-gated 3.7.x and 3.8.x compatibility profiles; older, future, and
  unknown versions remain on a safe baseline.
- Typed authentication outcomes distinguish supported, missing route, invalid
  credentials, insufficient scope, transport, and invalid responses.
- Authoritative `limitHwid` preservation on every unrelated client mutation;
  failed reads fail closed instead of clearing an operator setting.
- Contract-protected preservation of reset, traffic, keep-alive, flow,
  subscription, comment, enable, expiry, and quota fields.
- Panel-side lifecycle automation is detected and surfaced as
  `partially_managed`; Eve never invents lifecycle events.
- Certified 3.8 subscription paths are authoritative, with explicit fallback
  visibility when panel settings are unavailable.

### Multi-tenant commerce

- Prepaid wallets, package marketplace, day/GB custom pricing, gift volume,
  frozen transaction pricing, and durable wallet ledger.
- Ownership claims, reseller scopes, allowed-server maps, superadmin controls,
  deposits, receipts, cards, CSV exports, and Jalali statements.

### Messaging and engagement

- SMS through GMweb; WhatsApp through Baileys/OpenWA-compatible gateways.
- Telegram bots with purchase, trial, emergency access, membership gates,
  receipts, support, and reseller flows.
- Event and state-based messaging for creation, renewal, low volume, near
  expiry, expiry, and volume exhaustion.
- Shared cooldowns, quiet hours, age cutoffs, opt-out tags, rate-limit backoff,
  circuit breakers, delivery queues, and searchable send logs.
- Royalty and re-engagement workflows for inactive customers.

### Security, reliability, and observability

- Rate-limited authentication, secure cookies, strong password hashing,
  MFA/WebAuthn, RBAC, and scoped reseller authorization.
- Encrypted operational secrets, encrypted Telegram backups, TLS enforcement,
  hardened uploads, private-key handling, audit chains, and retention policy.
- Redis-backed snapshots, delta synchronization, bounded polling, refresh
  queues, usage rollups, traffic checks, and worker health monitoring.
- File-locked idempotent migration runner; all new schema changes use Alembic.
- RAM-aware Gunicorn workers, dedicated background processes, compressed
  responses, offline bundles, SBOM, and provenance attestations.

## Architecture

```text
 Browser / Telegram / API clients
              │
              ▼
 Flask routes + auth guards (25 domain blueprints)
              │
              ▼
 Services and adapters: billing, ownership, lifecycle,
 subscriptions, backup, BNQO, and X-UI compatibility
              │                 │
              ▼                 ▼
 SQLAlchemy models        Sanaei / Alireza X-UI panels
 core · finance · ops · telegram
              │
              ▼
 PostgreSQL · Redis · encrypted runtime filesystem

 Background plane: refresh · messaging · schedulers · usage · watchdog
 Control plane:    auth · RBAC · CRUD · finance · audit · subscriptions
```

The application bootstrap and compatibility surface remain in `app.py`. New
domain code follows one-way dependency flow inside `panel/`:

```text
panel/
├── core/       locks, Redis, phones, snapshots, transport primitives
├── models/     core, finance, telegram, and operations models
├── adapters/   external integrations, including adapters/xui.py
├── services/   billing, ownership, subscriptions, lifecycle, backup, BNQO
├── routes/     authenticated domain blueprints
├── jobs/       refresh, messaging, schedulers, usage, and BNQO workers
└── migrate.py  serialized schema migration and seed runner
```

## Runtime model

Production separates web requests from background work: Gunicorn serves the
control plane, Redis coordinates snapshots and workers, PostgreSQL stores
durable state, and dedicated schedulers perform refresh, rollups, watchdog,
backup, and messaging jobs. Single-process development mode is also supported.

### Development

Requirements: Python 3.11+, PostgreSQL, and Redis for the production split.

```bash
git clone https://github.com/aibedini/eve.git
cd eve
python -m venv .venv
source .venv/bin/activate              # Linux/macOS
pip install --require-hashes -r requirements.lock
export DATABASE_URL='postgresql://user:password@localhost/eve'
export SESSION_SECRET='replace-with-a-random-secret'
export INITIAL_ADMIN_PASSWORD='replace-before-first-login'
python app.py
```

Never commit `.env`, databases, runtime data, credentials, private keys,
backups, logs, virtual environments, or local agent/cache output.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection string |
| `SESSION_SECRET` | Flask session signing secret |
| `INITIAL_ADMIN_PASSWORD` | First administrator password |
| `SERVER_PASSWORD_KEY` | Encryption key for stored panel secrets |
| `EVE_BACKUP_KEY` | Encryption key for Telegram backup files |
| `REDIS_URL` | Shared snapshots and background-job coordination |

See the [operations runbook](docs/OPERATIONS_RUNBOOK.md), [security architecture](docs/security/ARCHITECTURE.md),
and [documentation index](docs/README.md) for the complete configuration contract.

## Quality gates

```bash
python -m pytest -q
python scripts/release_check.py --profile ci
python scripts/ui_design_audit.py --check
python -m pytest -q tests/test_docs_index.py
git diff --check
```

CI also runs focused unit tests, latency SLOs, mutation-scale O(1) proofs,
full integration tests, CodeQL, forbidden-artifact checks, Gitleaks,
pip-audit, Trivy, Docker release guards, SBOM, and provenance attestations.

### Compatibility status

- **3.7 implementation:** complete.
- **3.7 automated/contract compatibility:** required and continuously tested.
- **3.7 real-panel acceptance:** waived by product decision for this release;
  intentionally not run.
- **3.8 compatibility:** automated coverage plus controlled real-panel
  acceptance.

## Documentation

- [Documentation index](docs/README.md)
- [Operations runbook](docs/OPERATIONS_RUNBOOK.md)
- [X-UI API and compatibility contract](3XUI_V3_API.md)
- [BNQO architecture](docs/bnqo/ARCHITECTURE.md)
- [Threat model](docs/security/THREAT_MODEL.md)
- [Release security](docs/RELEASE_SECURITY.md)
- [Performance evidence](docs/performance/BASELINE.md)
- [Changelog](CHANGELOG.md)

## Contributing and support

Create a focused branch from `main`, keep domain dependencies one-way, add
regression tests for behavior and compatibility contracts, run every quality
gate, and open a pull request with security and operational impact documented.

Report vulnerabilities through [SECURITY.md](SECURITY.md), not public issues.
For support and feature requests, open a GitHub issue with sanitized logs and
reproduction steps.
