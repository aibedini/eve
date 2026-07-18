# Telegram Sales and Support Roadmap

This file is the durable implementation roadmap for Eve's Telegram channel.
It records completed work and keeps paused phases out of the active development
path without losing their design decisions.

## Product rules

- A Telegram identity is linked to a durable Eve customer account by verified
  mobile number.
- Service ownership remains one-to-one by default. Additional ownership must be
  proved by subscription link or approved manually by an administrator.
- Every reseller may eventually use the central bot or a dedicated bot/token.
- Telegram connectivity must support direct access, SOCKS/HTTP proxy failover,
  and managed local Xray egress.
- Manual card-to-card approval is the initial payment method. Payment providers
  must be added behind a provider interface instead of changing the order flow.

## Core sales and customer phases

- [x] Customer onboarding, language selection, and verified phone identity
- [x] Existing-service discovery and subscription-link ownership proof
- [x] Service list, details, connection link, and renewal request
- [x] Package-first purchase, optional server choice, and account-name policies
- [x] Manual receipt submission, administrator approval, and provisioning retry
- [x] Per-package/per-server inbound allocation for legacy and 3x-ui v3 servers
- [x] Customer purchase history and live order status
- [ ] Reseller-specific bot instances, branding, packages, cards, and permissions
- [ ] Payment provider interface for Iranian gateways, crypto, and future methods
- [x] Controlled trial and emergency-access policies with durable abuse limits
- [x] Customer notifications for expiry, low volume, renewal, and order events
- [x] Audit, rate-limit, fraud controls, and operational reporting

## Active phase — reseller bot foundation

- [x] Independent long-poll worker per bot so one reseller cannot delay another
- [x] Pooled HTTP/SOCKS connections and short route circuit breaker
- [x] Customer updates take priority over periodic SLA scans
- [x] Reseller bot create/edit UI, token verification, branding, and central-bot fallback
- [x] Reseller-scoped packages, bank cards, server access, and operator permissions
- [x] Per-bot runtime controls, health reporting, and safe disable/delete lifecycle
- [x] Promo engine with code/automatic discounts, referrals, and channel-join rewards

## Support phases — paused after 2.4.57

Completed:

- [x] Durable support conversations with text and attachments
- [x] Web inbox and Telegram private/group/topic replies
- [x] Customer ticket history and current status
- [x] Claim/assignment, priority, operator ownership, and first-response SLA
- [x] De-duplicated warning and automatic escalation to urgent

Resume later:

- [ ] Operator workload and response-time reports
- [ ] Saved replies, internal notes, tags, and ticket merge
- [ ] Business hours, holidays, and SLA calendars
- [ ] Customer satisfaction and post-close feedback
- [ ] Retention, export, privacy, and attachment lifecycle policies
