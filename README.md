<div align="center">

# ⚡ EVE

### Enterprise Control Plane for 3x-ui Operations

**Manage panels. Automate customer lifecycle. Control resellers. Deliver notifications. Observe everything.**

A production-oriented operations platform for teams running **Sanaei 3x-ui** and compatible X-UI infrastructure at scale.

<br />

[![Tests](https://github.com/aibedini/eve/actions/workflows/tests.yml/badge.svg)](https://github.com/aibedini/eve/actions/workflows/tests.yml)
[![Security](https://github.com/aibedini/eve/actions/workflows/security.yml/badge.svg)](https://github.com/aibedini/eve/actions/workflows/security.yml)
[![Docker](https://github.com/aibedini/eve/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/aibedini/eve/actions/workflows/docker-publish.yml)

![EVE](https://img.shields.io/badge/EVE-2.7.18-6f42c1?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square\&logo=python\&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Production-4169E1?style=flat-square\&logo=postgresql\&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-Realtime-DC382D?style=flat-square\&logo=redis\&logoColor=white)
![3x-ui](https://img.shields.io/badge/3x--ui-3.7.x%20%7C%203.8.x-success?style=flat-square)
![RTL](https://img.shields.io/badge/RTL-Persian%20Ready-blueviolet?style=flat-square)
![Jalali](https://img.shields.io/badge/Calendar-Jalali-informational?style=flat-square)

<br />

**[Quick Start](#-quick-start) · [Features](#-what-eve-can-do) · [3x-ui Compatibility](#-3x-ui-compatibility) · [Architecture](#-architecture) · [Security](#-security-by-design) · [Documentation](#-documentation)**

</div>

---

## 👋 Meet EVE

Running one X-UI panel is easy.

Running **many panels, thousands of clients, resellers, renewals, notifications, accounting, monitoring, security controls and background workers** reliably is a completely different problem.

**EVE is the operational layer built for that problem.**

It turns multiple 3x-ui installations into a unified control plane with centralized client lifecycle management, reseller commerce, automated messaging, observability, security controls and production-grade background processing.

> **One place to operate your entire X-UI infrastructure — without losing control of what happens underneath.**

---
# 📦 Deployment

EVE supports multiple installation models.

## Ubuntu / Debian

```bash
bash <(curl -Ls https://raw.githubusercontent.com/aibedini/eve/main/setup.sh)
```

> For production environments, review the installer and configure production secrets before exposing EVE to the network.

---

## 🐳 Docker

Build the offline-capable Docker bundle:

```bash
bash scripts/docker/build-offline-bundle.sh
```

See:

* [`DOCKER.md`](DOCKER.md)
* [`OFFLINE_INSTALL.md`](OFFLINE_INSTALL.md)
* [`OFFLINE_INSTALL_FA.md`](OFFLINE_INSTALL_FA.md)

---

## ✨ What EVE can do

| Area                           | Capability                                                                         |
| ------------------------------ | ---------------------------------------------------------------------------------- |
| 🖥️ **Multi-Panel Operations** | Manage multiple Sanaei 3x-ui and compatible X-UI servers from a single dashboard   |
| 👤 **Client Lifecycle**        | Create, edit, renew, enable, disable, reset, rotate and safely mutate clients      |
| 📊 **Traffic Intelligence**    | Track quota, consumption, expiry, activity and lifecycle state                     |
| 🔗 **Subscriptions**           | Generate and manage subscription links, QR codes and multi-inbound subscriptions   |
| 💳 **Reseller Platform**       | Wallets, packages, tariffs, ownership, permissions, receipts and financial ledgers |
| 📨 **Smart Messaging**         | SMS, Telegram and WhatsApp workflows for lifecycle and customer communication      |
| ⚡ **Realtime Updates**         | Redis snapshots, delta synchronization, adaptive refresh and SSE updates           |
| 🩺 **EVE Doctor**              | Operational diagnostics for panels, workers, queues, telemetry and infrastructure  |
| 🔐 **Security**                | MFA, WebAuthn, RBAC, encryption, TLS policy, audit trails and secret protection    |
| 🧠 **Lifecycle Automation**    | Durable state transitions and notification workflows tied to real service state    |
| 🌐 **Localized Operations**    | Persian/RTL UX, Jalali dates and Asia/Tehran-aware operations                      |
| 📦 **Flexible Deployment**     | Native Linux, Docker and offline / restricted-network installation                 |

---

# 🚀 Core Features

## 🖥️ Unified Multi-Panel Control

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

## 👤 Safe Client Lifecycle Management

Client operations are not treated as simple JSON edits.

EVE maintains lifecycle semantics around operations such as:

```text
Create
   ↓
Active
   ↓
Near Expiry / Low Traffic
   ↓
Renew / Extend / Refill
   ↓
Active
   ↓
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

### 🛡️ Mutation Safety

EVE protects fields owned by newer versions of 3x-ui.

For example, on supported 3.7/3.8 panels, unrelated client updates preserve the authoritative:

```text
limitHwid
```

value instead of accidentally resetting the operator's device limit.

If EVE cannot safely read a field that must be preserved, the mutation **fails closed** rather than silently damaging panel configuration.

---

# 🔄 Lifecycle Intelligence

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

## 📨 Durable Notification Pipeline

Lifecycle notifications use a durable event pipeline instead of relying only on periodic scanning.

EVE provides:

### 🧾 Durable observed state

Each service has a canonical observed state.

### 📬 Durable notification outbox

Transitions produce persistent notification events.

### 🔒 Idempotency

Deterministic event identities prevent multiple workers from independently creating duplicate logical events.

### 🕐 Delivery leasing

Workers lease events before delivery to reduce duplicate sends.

### ♻️ Reconciliation

Periodic scanning remains available as a safety net if a realtime transition is missed.

### 🚫 Stale reminder cancellation

When a customer renews, EVE advances the lifecycle generation and retires notifications associated with the previous lifecycle.

That means a queued **"your service has expired"** reminder should not remain valid after the same service has already been renewed.

---

# 📱 Messaging & Customer Engagement

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

# 💳 Reseller & Commerce Platform

EVE includes a built-in commerce layer for reseller-based operations.

### 💰 Wallets

Maintain prepaid reseller balances with durable financial history.

### 📦 Package Marketplace

Create volume/time packages for reseller purchase.

### 🧮 Flexible Pricing

Support:

* Day-based pricing
* GB-based pricing
* Custom reseller tariffs
* Gift traffic
* Frozen transaction pricing

### 🔐 Ownership

Control which reseller owns or can operate each service.

### 🌍 Server Visibility

Restrict reseller access to selected servers.

### 🧾 Financial Records

Support operational records including:

* Wallet transactions
* Deposits
* Receipts
* Statements
* CSV exports
* Jalali financial views

Financial transactions are recorded durably rather than being reconstructed from current balances.

---

# 🔗 Subscription Management

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

# 🧠 3x-ui Compatibility

EVE does not assume every 3x-ui release behaves identically.

Version-specific behavior is centralized inside:

```text
panel/services/xui_compat.py
```

Panel versions are normalized into compatibility families and receive only behavior explicitly certified for that family.

## Compatibility Matrix

| 3x-ui Version   | EVE Profile   |       Status | Notes                                                                                                    |
| --------------- | ------------- | -----------: | -------------------------------------------------------------------------------------------------------- |
| Legacy / pre-v3 | `legacy`      |  🟡 Retained | Existing legacy compatibility                                                                            |
| `3.3.x – 3.6.x` | `baseline_v3` |  🟡 Baseline | Existing v3 behavior preserved                                                                           |
| **`3.7.x`**     | `xui_3_7`     | 🟢 Certified | Scoped API tokens, `limitHwid`, lifecycle automation awareness, AmneziaWG-era API behavior               |
| **`3.8.x`**     | `xui_3_8`     | 🟢 Certified | 3.7 behavior + 3.8 authentication semantics, dynamic subscription paths and 3.8-era protocol/API changes |
| `3.9.x+`        | `baseline_v3` | ⚪ Unverified | Safe baseline + `future_version_uncertified` warning                                                     |
| Unknown         | `baseline_v3` | ⚪ Unverified | No version guessing; explicit warning                                                                    |

## 🔎 Local-First Version Detection

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

## 🔑 3x-ui Authentication Awareness

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

# ⚡ Realtime Data Plane

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

## 🧭 Monotonic Fetch Ordering

Multiple requests to the same panel may finish out of order.

EVE assigns monotonic fetch sequences so an older, slower request cannot overwrite data from a newer request.

```text
Fetch #101 ────────────────┐
                          │ returns late ❌ rejected
Fetch #102 ────────┐       │
                   └───────┴─ ✅ accepted
```

This protects dashboard and notification state from stale panel responses.

---

# 🩺 EVE Doctor

Operational health should be observable — not guessed.

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

# 📊 Observability

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

# 🔐 Security by Design

EVE treats infrastructure credentials and customer data as production secrets.

Security controls include:

### 🔑 Authentication

* Rate-limited authentication
* Strong password hashing
* Secure sessions
* Secure cookies
* MFA
* WebAuthn
* Step-up authentication

### 🛂 Authorization

* Permission-based RBAC
* Reseller scopes
* Ownership checks
* Allowed-server policies
* Superadmin boundaries

### 🔒 Secret Protection

* Encrypted panel credentials
* Key versioning
* Private-key protection
* Encrypted backup support
* TLS enforcement policies

### 🧾 Auditability

Sensitive operations are recorded in a tamper-evident audit trail.

Audit entries include operational metadata and are protected using a hash chain so unexpected modification or deletion can be detected.

### 📁 Upload Security

Uploaded application files are validated by actual file characteristics rather than trusting extensions alone.

---

# 🚄 Performance Engineering

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

# 🏗️ Architecture

```mermaid
flowchart TB

    UI["🌐 Browser / Dashboard"]
    TG["🤖 Telegram"]
    API["🔌 API Clients"]

    UI --> WEB
    TG --> WEB
    API --> WEB

    WEB["Flask Control Plane<br/>Auth · RBAC · Routes"]

    WEB --> SERVICES
    WEB --> CACHE

    SERVICES["Domain Services<br/>Lifecycle · Billing · Ownership<br/>Subscriptions · Backup"]

    SERVICES --> XUI
    SERVICES --> DB

    XUI["🖥️ 3x-ui / X-UI Panels"]

    DB[("🐘 PostgreSQL")]
    CACHE[("⚡ Redis")]

    JOBS["⚙️ Background Plane<br/>Refresh · Messaging · Usage<br/>Schedulers · Watchdog"]

    JOBS --> DB
    JOBS --> CACHE
    JOBS --> XUI

    MSG["📨 GMweb / Telegram / WhatsApp"]

    JOBS --> MSG
```

---

## 🧩 Code Organization

New domain logic follows one-way dependency boundaries under `panel/`.

```text
panel/
├── core/
│   ├── Redis
│   ├── locks
│   ├── snapshots
│   ├── phones
│   └── transport primitives
│
├── models/
│   ├── core
│   ├── finance
│   ├── telegram
│   └── operations
│
├── adapters/
│   └── xui.py
│
├── services/
│   ├── lifecycle
│   ├── subscriptions
│   ├── billing
│   ├── ownership
│   ├── backup
│   └── BNQO
│
├── routes/
│   └── authenticated domain blueprints
│
├── jobs/
│   ├── refresh
│   ├── messaging
│   ├── usage
│   ├── schedulers
│   └── watchdog
│
└── migrate.py
```

The application bootstrap and compatibility boundary remain integrated with `app.py` while domain-specific code continues moving toward explicit modules.

---

# 🧱 Production Runtime

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

# 🌍 Persian-First Operations

EVE includes first-class support for environments where Persian UX is not an afterthought.

Features include:

* RTL interfaces
* Persian text flows
* Jalali dates
* Jalali financial statements
* Tehran timezone-aware workflows
* Localized messaging and operational views

---


# 🧑‍💻 Development

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

# ⚙️ Important Configuration

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

# ✅ Quality Gates

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

# 🧪 Reliability Principles

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

# 📚 Documentation

EVE keeps implementation details and operational contracts close to the code.

| Topic                    | Documentation                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------ |
| 📘 Documentation Index   | [`docs/README.md`](docs/README.md)                                                   |
| 🛠️ Operations           | [`docs/OPERATIONS_RUNBOOK.md`](docs/OPERATIONS_RUNBOOK.md)                           |
| 🧠 3x-ui Compatibility   | [`3XUI_V3_API.md`](3XUI_V3_API.md)                                                   |
| 🔐 Security Architecture | [`docs/security/ARCHITECTURE.md`](docs/security/ARCHITECTURE.md)                     |
| ⚠️ Threat Model          | [`docs/security/THREAT_MODEL.md`](docs/security/THREAT_MODEL.md)                     |
| 🔑 MFA & Sessions        | [`docs/security/MFA.md`](docs/security/MFA.md)                                       |
| 🧾 Audit Trail           | [`docs/security/AUDIT_LOG.md`](docs/security/AUDIT_LOG.md)                           |
| 🚄 Performance           | [`docs/performance/BASELINE.md`](docs/performance/BASELINE.md)                       |
| ⚡ Delta Sync             | [`docs/performance/DELTA_SYNC.md`](docs/performance/DELTA_SYNC.md)                   |
| 📡 SSE                   | [`docs/performance/SSE.md`](docs/performance/SSE.md)                                 |
| 📊 Usage Intelligence    | [`docs/architecture/USAGE_INTELLIGENCE.md`](docs/architecture/USAGE_INTELLIGENCE.md) |
| 📨 GMweb Contract        | [`docs/GMWEB_CONTRACT.md`](docs/GMWEB_CONTRACT.md)                                   |
| 🔄 SMS Lifecycle         | [`docs/SMS_LIFECYCLE_INVALIDATION.md`](docs/SMS_LIFECYCLE_INVALIDATION.md)           |
| 🧠 State Transitions     | [`docs/TELEMETRY_STATE_TRANSITIONS.md`](docs/TELEMETRY_STATE_TRANSITIONS.md)         |
| 🎨 UI Design System      | [`docs/UI_DESIGN_SYSTEM.md`](docs/UI_DESIGN_SYSTEM.md)                               |
| 📦 Release Security      | [`docs/RELEASE_SECURITY.md`](docs/RELEASE_SECURITY.md)                               |
| 📝 Changelog             | [`CHANGELOG.md`](CHANGELOG.md)                                                       |

---

# 🛡️ Security Reporting

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

# 🤝 Contributing

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

# 🗺️ Project Philosophy

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

## ⚡ EVE

### Operate the infrastructure.

### Automate the lifecycle.

### Keep the state trustworthy.

<br />

**Built for serious X-UI operations.**

[Documentation](docs/README.md) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md)

</div>
