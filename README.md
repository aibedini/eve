<div align="center">

# âš¡ EVE

### Enterprise Control Plane for 3x-ui Operations

**Manage panels. Automate customer lifecycle. Control resellers. Deliver notifications. Observe everything.**

A production-oriented operations platform for teams running **Sanaei 3x-ui** and compatible X-UI infrastructure at scale.

<br />

[![Tests](https://github.com/aibedini/eve/actions/workflows/tests.yml/badge.svg)](https://github.com/aibedini/eve/actions/workflows/tests.yml)
[![Security](https://github.com/aibedini/eve/actions/workflows/security.yml/badge.svg)](https://github.com/aibedini/eve/actions/workflows/security.yml)
[![Docker](https://github.com/aibedini/eve/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/aibedini/eve/actions/workflows/docker-publish.yml)

![EVE](https://img.shields.io/badge/EVE-2.7.17-6f42c1?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square\&logo=python\&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Production-4169E1?style=flat-square\&logo=postgresql\&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-Realtime-DC382D?style=flat-square\&logo=redis\&logoColor=white)
![3x-ui](https://img.shields.io/badge/3x--ui-3.7.x%20%7C%203.8.x-success?style=flat-square)
![RTL](https://img.shields.io/badge/RTL-Persian%20Ready-blueviolet?style=flat-square)
![Jalali](https://img.shields.io/badge/Calendar-Jalali-informational?style=flat-square)

<br />

**[Quick Start](#-quick-start) Â· [Features](#-what-eve-can-do) Â· [3x-ui Compatibility](#-3x-ui-compatibility) Â· [Architecture](#-architecture) Â· [Security](#-security-by-design) Â· [Documentation](#-documentation)**

</div>

---

## ðŸ‘‹ Meet EVE

Running one X-UI panel is easy.

Running **many panels, thousands of clients, resellers, renewals, notifications, accounting, monitoring, security controls and background workers** reliably is a completely different problem.

**EVE is the operational layer built for that problem.**

It turns multiple 3x-ui installations into a unified control plane with centralized client lifecycle management, reseller commerce, automated messaging, observability, security controls and production-grade background processing.

> **One place to operate your entire X-UI infrastructure â€” without losing control of what happens underneath.**

---
# ðŸ“¦ Deployment

EVE supports multiple installation models.

## Ubuntu / Debian

```bash
bash <(curl -Ls https://raw.githubusercontent.com/aibedini/eve/main/setup.sh)
```

> For production environments, review the installer and configure production secrets before exposing EVE to the network.

---

## ðŸ³ Docker

Build the offline-capable Docker bundle:

```bash
bash scripts/docker/build-offline-bundle.sh
```

See:

* [`DOCKER.md`](DOCKER.md)
* [`OFFLINE_INSTALL.md`](OFFLINE_INSTALL.md)
* [`OFFLINE_INSTALL_FA.md`](OFFLINE_INSTALL_FA.md)

---

## âœ¨ What EVE can do

| Area                           | Capability                                                                         |
| ------------------------------ | ---------------------------------------------------------------------------------- |
| ðŸ–¥ï¸ **Multi-Panel Operations** | Manage multiple Sanaei 3x-ui and compatible X-UI servers from a single dashboard   |
| ðŸ‘¤ **Client Lifecycle**        | Create, edit, renew, enable, disable, reset, rotate and safely mutate clients      |
| ðŸ“Š **Traffic Intelligence**    | Track quota, consumption, expiry, activity and lifecycle state                     |
| ðŸ”— **Subscriptions**           | Generate and manage subscription links, QR codes and multi-inbound subscriptions   |
| ðŸ’³ **Reseller Platform**       | Wallets, packages, tariffs, ownership, permissions, receipts and financial ledgers |
| ðŸ“¨ **Smart Messaging**         | SMS, Telegram and WhatsApp workflows for lifecycle and customer communication      |
| âš¡ **Realtime Updates**         | Redis snapshots, delta synchronization, adaptive refresh and SSE updates           |
| ðŸ©º **EVE Doctor**              | Operational diagnostics for panels, workers, queues, telemetry and infrastructure  |
| ðŸ” **Security**                | MFA, WebAuthn, RBAC, encryption, TLS policy, audit trails and secret protection    |
| ðŸ§  **Lifecycle Automation**    | Durable state transitions and notification workflows tied to real service state    |
| ðŸŒ **Localized Operations**    | Persian/RTL UX, Jalali dates and Asia/Tehran-aware operations                      |
| ðŸ“¦ **Flexible Deployment**     | Native Linux, Docker and offline / restricted-network installation                 |

---

# ðŸš€ Core Features

## ðŸ–¥ï¸ Unified Multi-Panel Control

Operate your infrastructure without jumping between panel tabs.

EVE provides a centralized operational view across multiple servers and panels.

It understands:

* Servers
* Inbounds
* Clients
* Traffic
* Expiry
* Quotas
* Subscription links
* QR codes
* Connection state
* Panel health
* Panel compatibility

Panel operations are isolated per server so activity on one infrastructure node does not unnecessarily block the others.

---

## ðŸ‘¤ Safe Client Lifecycle Management

Client operations are not treated as simple JSON edits.

EVE maintains lifecycle semantics around operations such as:

```text
Create
   â†“
Active
   â†“
Near Expiry / Low Traffic
   â†“
Renew / Extend / Refill
   â†“
Active
   â†“
Expired / Depleted
```

Common actions include:

* Client creation
* Renewal
* Expiry modification
* Quota changes
* Traffic reset
* Enable / disable
* Credential rotation
* Subscription regeneration
* QR generation
* Multi-inbound assignments

### ðŸ›¡ï¸ Mutation Safety

EVE protects fields owned by newer versions of 3x-ui.

For example, on supported 3.7/3.8 panels, unrelated client updates preserve the authoritative:

```text
limitHwid
```

value instead of accidentally resetting the operator's device limit.

If EVE cannot safely read a field that must be preserved, the mutation **fails closed** rather than silently damaging panel configuration.

---

# ðŸ”„ Lifecycle Intelligence

EVE does more than periodically scan accounts.

Modern EVE releases maintain a durable view of the **last observed service state**.

```mermaid
flowchart LR
    A[3x-ui Panel] --> B[Telemetry Fetch]
    B --> C[Canonical Service State]
    C --> D{State Changed?}

    D -->|No| E[Update Snapshot]
    D -->|Yes| F[Durable Transition Event]

    F --> G[Notification Outbox]
    G --> H[Delivery Worker]
    H --> I[GMweb / Telegram / WhatsApp]
```

A service transition can represent events such as:

* Low remaining volume
* Volume exhausted
* Near expiry
* Expired
* Renewed
* Reactivated

The first observation establishes a baseline instead of generating a notification storm.

---

## ðŸ“¨ Durable Notification Pipeline

Lifecycle notifications use a durable event pipeline instead of relying only on periodic scanning.

EVE provides:

### ðŸ§¾ Durable observed state

Each service has a canonical observed state.

### ðŸ“¬ Durable notification outbox

Transitions produce persistent notification events.

### ðŸ”’ Idempotency

Deterministic event identities prevent multiple workers from independently creating duplicate logical events.

### ðŸ• Delivery leasing

Workers lease events before delivery to reduce duplicate sends.

### â™»ï¸ Reconciliation

Periodic scanning remains available as a safety net if a realtime transition is missed.

### ðŸš« Stale reminder cancellation

When a customer renews, EVE advances the lifecycle generation and retires notifications associated with the previous lifecycle.

That means a queued **"your service has expired"** reminder should not remain valid after the same service has already been renewed.

---

# ðŸ“± Messaging & Customer Engagement

EVE can automate communication across multiple channels.

### SMS

Integration with **GMweb** supports lifecycle messaging and delivery workflows.

### Telegram

Telegram bots can support:

* Purchases
* Trial access
* Emergency access
* Membership gates
* Receipts
* Customer support
* Reseller operations

### WhatsApp

Compatible gateway integrations support WhatsApp messaging workflows.

### Messaging controls

EVE includes operational protections such as:

* Shared cooldowns
* Quiet hours
* Daily limits
* Hourly limits
* Recipient pacing
* Opt-out tags
* Age cutoffs
* Rate-limit backoff
* Retry handling
* Circuit breakers
* Searchable delivery history

---

# ðŸ’³ Reseller & Commerce Platform

EVE includes a built-in commerce layer for reseller-based operations.

### ðŸ’° Wallets

Maintain prepaid reseller balances with durable financial history.

### ðŸ“¦ Package Marketplace

Create volume/time packages for reseller purchase.

### ðŸ§® Flexible Pricing

Support:

* Day-based pricing
* GB-based pricing
* Custom reseller tariffs
* Gift traffic
* Frozen transaction pricing

### ðŸ” Ownership

Control which reseller owns or can operate each service.

### ðŸŒ Server Visibility

Restrict reseller access to selected servers.

### ðŸ§¾ Financial Records

Support operational records including:

* Wallet transactions
* Deposits
* Receipts
* Statements
* CSV exports
* Jalali financial views

Financial transactions are recorded durably rather than being reconstructed from current balances.

---

# ðŸ”— Subscription Management

EVE manages customer subscription delivery while respecting differences between 3x-ui versions.

Supported workflows include:

* Native protocol links
* Subscription URLs
* JSON subscriptions
* Clash-compatible subscriptions
* Multi-inbound subscriptions
* QR codes
* Subscription metadata caching

For certified 3.8 panels, EVE can use authoritative panel subscription paths instead of assuming the panel still uses a default `/sub/` path.

When panel metadata is unavailable, fallback behavior is visible through **EVE Doctor** rather than silently hidden.

---

# ðŸ§  3x-ui Compatibility

EVE does not assume every 3x-ui release behaves identically.

Version-specific behavior is centralized inside:

```text
panel/services/xui_compat.py
```

Panel versions are normalized into compatibility families and receive only behavior explicitly certified for that family.

## Compatibility Matrix

| 3x-ui Version   | EVE Profile   |       Status | Notes                                                                                                    |
| --------------- | ------------- | -----------: | -------------------------------------------------------------------------------------------------------- |
| Legacy / pre-v3 | `legacy`      |  ðŸŸ¡ Retained | Existing legacy compatibility                                                                            |
| `3.3.x â€“ 3.6.x` | `baseline_v3` |  ðŸŸ¡ Baseline | Existing v3 behavior preserved                                                                           |
| **`3.7.x`**     | `xui_3_7`     | ðŸŸ¢ Certified | Scoped API tokens, `limitHwid`, lifecycle automation awareness, AmneziaWG-era API behavior               |
| **`3.8.x`**     | `xui_3_8`     | ðŸŸ¢ Certified | 3.7 behavior + 3.8 authentication semantics, dynamic subscription paths and 3.8-era protocol/API changes |
| `3.9.x+`        | `baseline_v3` | âšª Unverified | Safe baseline + `future_version_uncertified` warning                                                     |
| Unknown         | `baseline_v3` | âšª Unverified | No version guessing; explicit warning                                                                    |

## ðŸ”Ž Local-First Version Detection

EVE does not need GitHub access from every managed panel just to determine the panel version.

The primary source is the panel itself:

```http
GET /panel/api/server/status
```

using:

```text
obj.panelVersion
```

Remote update metadata is treated as corroborating information rather than the source of truth.

---

## ðŸ”‘ 3x-ui Authentication Awareness

Modern 3x-ui versions distinguish authentication and authorization failures.

EVE understands the difference between:

| Response | Meaning                                     |
| -------- | ------------------------------------------- |
| `2xx`    | Successful request                          |
| `401`    | Invalid, expired or rotated credential      |
| `403`    | Valid authentication but insufficient scope |
| `404`    | Route/capability may not exist              |

Authentication failures are **never automatically interpreted as "old panel."**

This prevents a broken API token from causing EVE to silently switch to an incompatible legacy API path.

---

# âš¡ Realtime Data Plane

EVE is designed around bounded, incremental synchronization instead of continuously rebuilding the entire world.

The runtime includes:

* Redis-backed server snapshots
* Delta synchronization
* Per-server refresh coordination
* Adaptive polling
* Shared refresh/watch state
* Bounded panel access
* Usage rollups
* SSE live updates
* Background queues

---

## ðŸ§­ Monotonic Fetch Ordering

Multiple requests to the same panel may finish out of order.

EVE assigns monotonic fetch sequences so an older, slower request cannot overwrite data from a newer request.

```text
Fetch #101 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”
                          â”‚ returns late âŒ rejected
Fetch #102 â”€â”€â”€â”€â”€â”€â”€â”€â”       â”‚
                   â””â”€â”€â”€â”€â”€â”€â”€â”´â”€ âœ… accepted
```

This protects dashboard and notification state from stale panel responses.

---

# ðŸ©º EVE Doctor

Operational health should be observable â€” not guessed.

EVE Doctor exposes diagnostics for areas such as:

* Panel reachability
* Panel compatibility
* Authentication state
* Telemetry freshness
* Worker heartbeat
* Notification queues
* Retry exhaustion
* Redis availability
* Database health
* Background processing
* Subscription fallback state
* Infrastructure drift

Diagnostic endpoints expose operational state while avoiding sensitive values such as:

* Passwords
* API tokens
* Private keys
* Phone numbers
* Message bodies

---

# ðŸ“Š Observability

EVE includes operational visibility across the application stack.

Examples include:

* Request correlation IDs
* HTTP metrics
* Background-worker health
* Panel coverage
* Queue depth
* Oldest event age
* Retry counters
* Delivery status
* Panel freshness
* Audit verification
* Database checks
* Disk checks
* Static asset checks

Every response can participate in request tracing through:

```http
X-Request-ID
```

---

# ðŸ” Security by Design

EVE treats infrastructure credentials and customer data as production secrets.

Security controls include:

### ðŸ”‘ Authentication

* Rate-limited authentication
* Strong password hashing
* Secure sessions
* Secure cookies
* MFA
* WebAuthn
* Step-up authentication

### ðŸ›‚ Authorization

* Permission-based RBAC
* Reseller scopes
* Ownership checks
* Allowed-server policies
* Superadmin boundaries

### ðŸ”’ Secret Protection

* Encrypted panel credentials
* Key versioning
* Private-key protection
* Encrypted backup support
* TLS enforcement policies

### ðŸ§¾ Auditability

Sensitive operations are recorded in a tamper-evident audit trail.

Audit entries include operational metadata and are protected using a hash chain so unexpected modification or deletion can be detected.

### ðŸ“ Upload Security

Uploaded application files are validated by actual file characteristics rather than trusting extensions alone.

---

# ðŸš„ Performance Engineering

Performance behavior is documented and tested as an engineering contract.

EVE includes work around:

* Per-server snapshot caching
* Delta refresh
* Adaptive polling
* Query budgets
* Database indexes
* Connection pooling
* Response serialization
* Subscription caching
* Scoped refresh locks
* Bounded panel concurrency
* Mutation latency
* O(1) panel-scale mutation behavior
* Live SSE updates

Performance documentation and measured baselines live under:

```text
docs/performance/
```

---

# ðŸ—ï¸ Architecture

```mermaid
flowchart TB

    UI["ðŸŒ Browser / Dashboard"]
    TG["ðŸ¤– Telegram"]
    API["ðŸ”Œ API Clients"]

    UI --> WEB
    TG --> WEB
    API --> WEB

    WEB["Flask Control Plane<br/>Auth Â· RBAC Â· Routes"]

    WEB --> SERVICES
    WEB --> CACHE

    SERVICES["Domain Services<br/>Lifecycle Â· Billing Â· Ownership<br/>Subscriptions Â· Backup"]

    SERVICES --> XUI
    SERVICES --> DB

    XUI["ðŸ–¥ï¸ 3x-ui / X-UI Panels"]

    DB[("ðŸ˜ PostgreSQL")]
    CACHE[("âš¡ Redis")]

    JOBS["âš™ï¸ Background Plane<br/>Refresh Â· Messaging Â· Usage<br/>Schedulers Â· Watchdog"]

    JOBS --> DB
    JOBS --> CACHE
    JOBS --> XUI

    MSG["ðŸ“¨ GMweb / Telegram / WhatsApp"]

    JOBS --> MSG
```

---

## ðŸ§© Code Organization

New domain logic follows one-way dependency boundaries under `panel/`.

```text
panel/
â”œâ”€â”€ core/
â”‚   â”œâ”€â”€ Redis
â”‚   â”œâ”€â”€ locks
â”‚   â”œâ”€â”€ snapshots
â”‚   â”œâ”€â”€ phones
â”‚   â””â”€â”€ transport primitives
â”‚
â”œâ”€â”€ models/
â”‚   â”œâ”€â”€ core
â”‚   â”œâ”€â”€ finance
â”‚   â”œâ”€â”€ telegram
â”‚   â””â”€â”€ operations
â”‚
â”œâ”€â”€ adapters/
â”‚   â””â”€â”€ xui.py
â”‚
â”œâ”€â”€ services/
â”‚   â”œâ”€â”€ lifecycle
â”‚   â”œâ”€â”€ subscriptions
â”‚   â”œâ”€â”€ billing
â”‚   â”œâ”€â”€ ownership
â”‚   â”œâ”€â”€ backup
â”‚   â””â”€â”€ BNQO
â”‚
â”œâ”€â”€ routes/
â”‚   â””â”€â”€ authenticated domain blueprints
â”‚
â”œâ”€â”€ jobs/
â”‚   â”œâ”€â”€ refresh
â”‚   â”œâ”€â”€ messaging
â”‚   â”œâ”€â”€ usage
â”‚   â”œâ”€â”€ schedulers
â”‚   â””â”€â”€ watchdog
â”‚
â””â”€â”€ migrate.py
```

The application bootstrap and compatibility boundary remain integrated with `app.py` while domain-specific code continues moving toward explicit modules.

---

# ðŸ§± Production Runtime

A typical production deployment separates interactive requests from background processing.

```mermaid
flowchart LR

    NGINX["Reverse Proxy"] --> GUNI["Gunicorn"]
    GUNI --> APP["EVE Web"]

    APP --> PG[("PostgreSQL")]
    APP --> REDIS[("Redis")]

    REDIS --> WORKERS["Background Workers"]
    WORKERS --> PANELS["3x-ui Panels"]
    WORKERS --> GATEWAYS["Messaging Gateways"]

```

### Main components

| Component         | Responsibility                                    |
| ----------------- | ------------------------------------------------- |
| Gunicorn          | Web/control-plane requests                        |
| PostgreSQL        | Durable application state                         |
| Redis             | Shared snapshots, coordination and realtime state |
| Refresh workers   | Panel synchronization                             |
| Messaging workers | Notification delivery                             |
| Usage workers     | Traffic and usage processing                      |
| Schedulers        | Periodic operations                               |
| Watchdog          | Runtime health monitoring                         |

---

# ðŸŒ Persian-First Operations

EVE includes first-class support for environments where Persian UX is not an afterthought.

Features include:

* RTL interfaces
* Persian text flows
* Jalali dates
* Jalali financial statements
* Tehran timezone-aware workflows
* Localized messaging and operational views

---


# ðŸ§‘â€ðŸ’» Development

## Requirements

```text
Python 3.11+
PostgreSQL
Redis
```

Clone the repository:

```bash
git clone https://github.com/aibedini/eve.git
cd eve
```

Create the virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

Install locked dependencies:

```bash
pip install --require-hashes -r requirements.lock
```

Configure the environment:

```bash
export DATABASE_URL='postgresql://user:password@localhost/eve'
export SESSION_SECRET='replace-with-a-random-secret'
export INITIAL_ADMIN_PASSWORD='replace-before-first-login'
```

Start EVE:

```bash
python app.py
```

---

# âš™ï¸ Important Configuration

| Variable                 | Description                               |
| ------------------------ | ----------------------------------------- |
| `DATABASE_URL`           | PostgreSQL connection URL                 |
| `SESSION_SECRET`         | Flask session-signing secret              |
| `INITIAL_ADMIN_PASSWORD` | Initial administrator password            |
| `SERVER_PASSWORD_KEY`    | Encryption key for stored panel secrets   |
| `EVE_BACKUP_KEY`         | Encryption key for protected backup files |
| `REDIS_URL`              | Redis coordination and snapshot store     |

Never commit:

```text
.env
credentials
database dumps
runtime databases
private keys
backup archives
logs containing secrets
virtual environments
local agent/cache output
```

---

# âœ… Quality Gates

Before a release, EVE can be validated with:

```bash
python -m pytest -q

python scripts/release_check.py --profile ci

python scripts/ui_design_audit.py --check

python -m pytest -q tests/test_docs_index.py

git diff --check
```

CI also includes controls and checks such as:

* Unit tests
* Integration tests
* Compatibility contract tests
* Performance/SLO checks
* Mutation-scale tests
* CodeQL
* Gitleaks
* `pip-audit`
* Trivy
* Forbidden-artifact detection
* Docker release guards
* SBOM generation
* Provenance attestations

---

# ðŸ§ª Reliability Principles

Several invariants guide EVE's design.

### 1. Never guess compatibility

Unknown panel versions fall back safely and produce an explicit warning.

### 2. Never silently destroy operator settings

Critical upstream-owned values are read authoritatively before mutation.

### 3. Never trust stale telemetry over newer telemetry

Per-server monotonic fetch ordering prevents stale overwrite.

### 4. Never send lifecycle messages from stale assumptions

Notification delivery revalidates lifecycle state before sending.

### 5. Renewal changes the lifecycle generation

Messages created for an old generation become invalid after renewal.

### 6. Prefer durable state over process-local memory

Important workflow coordination survives process boundaries.

### 7. Security failures must be actionable

Authentication, authorization, transport and API compatibility failures remain distinguishable.

---

# ðŸ“š Documentation

EVE keeps implementation details and operational contracts close to the code.

| Topic                    | Documentation                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------ |
| ðŸ“˜ Documentation Index   | [`docs/README.md`](docs/README.md)                                                   |
| ðŸ› ï¸ Operations           | [`docs/OPERATIONS_RUNBOOK.md`](docs/OPERATIONS_RUNBOOK.md)                           |
| ðŸ§  3x-ui Compatibility   | [`3XUI_V3_API.md`](3XUI_V3_API.md)                                                   |
| ðŸ” Security Architecture | [`docs/security/ARCHITECTURE.md`](docs/security/ARCHITECTURE.md)                     |
| âš ï¸ Threat Model          | [`docs/security/THREAT_MODEL.md`](docs/security/THREAT_MODEL.md)                     |
| ðŸ”‘ MFA & Sessions        | [`docs/security/MFA.md`](docs/security/MFA.md)                                       |
| ðŸ§¾ Audit Trail           | [`docs/security/AUDIT_LOG.md`](docs/security/AUDIT_LOG.md)                           |
| ðŸš„ Performance           | [`docs/performance/BASELINE.md`](docs/performance/BASELINE.md)                       |
| âš¡ Delta Sync             | [`docs/performance/DELTA_SYNC.md`](docs/performance/DELTA_SYNC.md)                   |
| ðŸ“¡ SSE                   | [`docs/performance/SSE.md`](docs/performance/SSE.md)                                 |
| ðŸ“Š Usage Intelligence    | [`docs/architecture/USAGE_INTELLIGENCE.md`](docs/architecture/USAGE_INTELLIGENCE.md) |
| ðŸ“¨ GMweb Contract        | [`docs/GMWEB_CONTRACT.md`](docs/GMWEB_CONTRACT.md)                                   |
| ðŸ”„ SMS Lifecycle         | [`docs/SMS_LIFECYCLE_INVALIDATION.md`](docs/SMS_LIFECYCLE_INVALIDATION.md)           |
| ðŸ§  State Transitions     | [`docs/TELEMETRY_STATE_TRANSITIONS.md`](docs/TELEMETRY_STATE_TRANSITIONS.md)         |
| ðŸŽ¨ UI Design System      | [`docs/UI_DESIGN_SYSTEM.md`](docs/UI_DESIGN_SYSTEM.md)                               |
| ðŸ“¦ Release Security      | [`docs/RELEASE_SECURITY.md`](docs/RELEASE_SECURITY.md)                               |
| ðŸ“ Changelog             | [`CHANGELOG.md`](CHANGELOG.md)                                                       |

---

# ðŸ›¡ï¸ Security Reporting

Please **do not disclose vulnerabilities through public GitHub issues**.

Follow the responsible disclosure process described in:

[`SECURITY.md`](SECURITY.md)

When sharing diagnostic information, always remove:

```text
Passwords
API tokens
Session cookies
Private keys
Subscription credentials
Database URLs
Phone numbers
Customer-identifying data
```

---

# ðŸ¤ Contributing

Contributions should preserve EVE's operational and compatibility guarantees.

Before submitting a change:

1. Create a focused branch from `main`.
2. Keep dependency flow between domains one-way.
3. Add regression coverage for changed behavior.
4. Add compatibility tests when touching X-UI integration.
5. Document security impact.
6. Document operational impact.
7. Run the quality gates.
8. Open a focused pull request.

Changes affecting panel mutation, authentication, lifecycle state, messaging or financial data should include explicit regression coverage.

---

# ðŸ—ºï¸ Project Philosophy

EVE is built around a simple idea:

> **Infrastructure automation should make operations safer, not merely faster.**

That means preferring:

**observable state over assumptions**
**durable events over timers**
**explicit compatibility over version guessing**
**safe failure over silent mutation**
**measured performance over intuition**
**auditable actions over invisible automation**

---

<div align="center">

## âš¡ EVE

### Operate the infrastructure.

### Automate the lifecycle.

### Keep the state trustworthy.

<br />

**Built for serious X-UI operations.**

[Documentation](docs/README.md) Â· [Changelog](CHANGELOG.md) Â· [Security](SECURITY.md)

</div>
