# 3x-ui Endpoint Registry

> Reference for the 3x-ui panel API endpoints used by EVE, plus routes added
> through v3.11.0. Methods below describe the v3.11.0 contract unless marked
> legacy.
> Used by EVE's version-aware routing (`panel/services/panel_capabilities.py`,
> `panel/services/xui_compat.py`, `panel/adapters/xui.py`).

## Version timeline

| Family | Tag range | Key changes |
|--------|-----------|-------------|
| 2.x | v2.0.0 – v2.5.4 | Inbound-based client management only |
| 3.0 | v3.0.0 | Major rewrite; inbound API still primary |
| 3.1 | v3.1.0 – v3.2.x | First-class `/panel/api/clients/*` endpoints |
| 3.3 | v3.3.0 – v3.3.6 | `nodePending` in update response (from 3.3.1) |
| 3.5 | v3.5.0 – v3.6.x | `bulkEnable`/`bulkDisable`/`bulkAdjust` |
| 3.7 | v3.7.0 – v3.7.x | Scoped/expiring API tokens, `limitHwid` |
| 3.8 | v3.8.0 – v3.8.x | Bearer 401 rejection, random sub paths, TUIC |
| 3.9 | v3.9.0 – v3.9.x | Inbound update no longer mutates clients; native attach/detach required; renewal preview added |
| 3.10 | v3.10.0 – v3.10.x | MASQUE and XDRIVE; existing panel API routes retained |
| 3.11 | v3.11.0 – v3.11.x | Per-client host rules, subId lookup, kernel AmneziaWG readiness |
| 3.12+ | v3.12.0+ | Uncertified — EVE uses baseline + probe |

## Authentication

### Cookie session (all versions)

```
POST /login
Body: username=<user>&password=<pass>
Response: Set-Cookie: 3x-ui=<session>; ...
```

All subsequent requests carry the session cookie. The `/panel/api/*` POST
endpoints also require a CSRF-like header when the panel's middleware is
active; EVE sends `X-Requested-With: XMLHttpRequest` on every request.

### Bearer token (v3.7+)

```
Authorization: Bearer <token>
```

Scoped tokens were introduced in v3.7.0 (`ApiToken.Scope`, `ApiToken.ExpiresAt`
in `internal/web/controller/api.go`). Token scope restricts which
`/panel/api/*` routes are accessible.

| Behaviour | v3.7 | v3.8+ |
|-----------|------|-------|
| Rejected Bearer | 404 (without `X-Requested-With`) | 401 |
| `X-Requested-With` needed | Yes | No |
| Expiring tokens | Yes | Yes |

## Endpoint catalogue

### Server / system

| Method | Path | Since | Notes |
|--------|------|-------|-------|
| POST | `/login` | v2.0 | Returns session cookie |
| GET | `/panel/api/server/status` | v2.0 | CPU, memory, disk, uptime, Xray state |
| GET | `/panel/api/server/getXrayVersion` | v2.0 | Running Xray core version |
| POST | `/panel/api/server/installXray/:version` | v2.0 | Install/update Xray core |
| POST | `/panel/api/server/stopXrayService` | v2.0 | Stop Xray process |
| POST | `/panel/api/server/restartXrayService` | v2.0 | Restart Xray process |
| GET | `/panel/api/server/getConfigJson` | v3.0 | Current Xray JSON config |
| GET | `/panel/api/server/getDb` | v3.0 | Database backup download |
| POST | `/panel/api/server/importDB` | v3.0 | Database restore (multipart) |
| GET | `/panel/api/server/getNewX25519Cert` | v3.0 | Generate X25519 key pair |
| GET | `/panel/api/server/getPanelUpdateInfo` | v3.5 | Latest release info from GitHub |
| POST | `/panel/api/server/logs/:count` | v3.0 | Service log lines |

### Inbound management

| Method | Path | Since | Notes |
|--------|------|-------|-------|
| GET | `/panel/api/inbounds/list` | v2.0 | All inbounds with embedded clients |
| GET | `/panel/api/inbounds/get/:id` | v2.0 | Single inbound by ID |
| POST | `/panel/api/inbounds/add` | v2.0 | Create inbound |
| POST | `/panel/api/inbounds/update/:id` | v2.0 | Update inbound; v3.9+ ignores client-list edits |
| POST | `/panel/api/inbounds/del/:id` | v2.0 | Delete inbound |
| POST | `/panel/api/inbounds/onlines` | legacy | Online client list on older panels |
| POST | `/panel/api/inbounds/resetAllTraffics` | v2.0 | Zero all traffic counters |
| POST | `/panel/api/inbounds/resetAllClientTraffics/:id` | legacy | Zero traffic for all clients in inbound |
| POST | `/panel/api/inbounds/delDepletedClients/:id` | legacy | Remove over-quota clients from inbound |
| POST | `/panel/api/inbounds/addClient` | legacy | Add client to inbound |
| POST | `/panel/api/inbounds/updateClient/:uuid` | legacy | Update client within inbound |
| POST | `/panel/api/inbounds/:id/resetClientTraffic/:email` | legacy | Reset one client's traffic |
| POST | `/panel/api/inbounds/:id/delClient/:uuid` | legacy | Delete client from inbound |
| GET | `/panel/api/inbounds/createbackup` | legacy | Trigger backup creation |
| POST | `/panel/api/inbounds/setEnable/:id` | ≤v3.8 | Change inbound enable state |

### Client management (v3.1+ — first-class API)

> These endpoints are the primary EVE interface for v3.1+ panels.
> EVE probes `/panel/api/clients/get/__eve_capability_probe__` to detect
> whether the first-class API exists.

| Method | Path | Since | Notes |
|--------|------|-------|-------|
| GET | `/panel/api/clients/get/:email` | v3.1 | Single client by email |
| POST | `/panel/api/clients/add` | v3.1 | Add client (body: `{id, inboundIds, ...}`) |
| POST | `/panel/api/clients/update/:email` | v3.1 | Update client fields |
| POST | `/panel/api/clients/del/:email` | v3.1 | Delete client |
| POST | `/panel/api/clients/resetTraffic/:email` | v3.1 | Zero client traffic counters |
| GET | `/panel/api/clients/traffic/:email` | v3.1 | Client traffic stats |
| POST | `/panel/api/clients/onlines` | v3.3 | Online client list (alternative to inbounds/onlines) |
| POST | `/panel/api/clients/:email/attach` | v3.5 | Attach client to additional inbound(s) |
| POST | `/panel/api/clients/:email/detach` | v3.5 | Detach client from inbound(s) |
| POST | `/panel/api/clients/bulkEnable` | v3.5 | Enable multiple clients at once |
| POST | `/panel/api/clients/bulkDisable` | v3.5 | Disable multiple clients at once |
| POST | `/panel/api/clients/bulkAdjust` | v3.5 | Adjust traffic/expiry for multiple clients |

### Additional routes through v3.11.0

These are the routes added after the v3.8.0 baseline in the tagged upstream
controllers. No existing registered controller route was removed. EVE's client
management uses the existing `/clients/get/:email`, `/clients/update/:email`,
`/clients/:email/attach` and `/clients/:email/detach` contracts; these extra
routes serve panel functions that EVE does not currently operate.

| Method | Path | Since | Purpose |
|--------|------|-------|---------|
| POST | `/panel/api/clients/renewalPreview` | v3.9 | Preview upstream renewal schedule |
| GET | `/panel/api/clients/get/subId/:subId` | v3.11 | Find clients by subscription ID |
| GET | `/panel/api/hosts/raw` | v3.11 | List host rules with numeric IDs |
| GET | `/panel/api/inbounds/amneziawgKernelReadiness` | v3.11 | Check kernel datapath readiness |
| POST | `/panel/api/setting/tgBotCapabilities` | v3.11 | Report Telegram bot capabilities |
| GET | `/sponsors` | v3.9 | Public sponsor listing |
| GET | `/sponsors/logo/:name` | v3.9 | Public sponsor image |

The v3.9 release changed `/panel/api/inbounds/update/:id`: it no longer edits
an inbound's client membership or client fields. EVE uses the first-class
client add endpoint even for a single inbound and native attach/detach
and verifies the returned client list if an older-panel fallback runs. A
successful HTTP response alone is not evidence that membership changed.

### Subscription (all versions)

| Method | Path | Since | Notes |
|--------|------|-------|-------|
| GET | `/sub/:token` | v2.0 | Client subscription link (Xray config) |
| GET | `/sub/:token/info` | v3.0 | Subscription metadata (traffic, expiry) |

v3.8+ uses randomised subscription base paths on fresh installs
(`internal/web/service/setting.go`). EVE discovers the actual path from
the panel settings.

## Client record shape (v3.1+)

The JSON body for `/panel/api/clients/update/:email` and the response from
`/panel/api/clients/get/:email`:

```json
{
  "id": "uuid",
  "email": "user@example.com",
  "enable": true,
  "expiryTime": 1700000000000,
  "totalGB": 107374182400,
  "up": 0,
  "down": 0,
  "flow": "",
  "subId": "sub-token",
  "limitIp": 2,
  "limitHwid": 0,
  "reset": 0,
  "tgId": "",
  "comment": "",
  "inboundIds": [1, 2]
}
```

### Field notes

| Field | Version notes |
|-------|---------------|
| `limitHwid` | Added in v3.7. Controls hardware-ID-based device limiting. |
| `reset` / `resetDay` | Panel-side lifecycle automation. When non-zero, the panel resets traffic on a schedule. EVE detects and warns about this. |
| `nodePending` | Present in the update *response* from v3.3.1+. Indicates the config change is queued for Xray restart. |
| `inboundIds` | Array for multi-inbound clients (v3.5+ attach/detach). |

## EVE's version-aware routing

EVE maps each detected panel version to a `RenewStrategy` (see
`panel/services/panel_capabilities.py`):

| Strategy | Version range | API used | Key behaviour |
|----------|---------------|----------|---------------|
| `LEGACY_INBOUND` | v2.x, v3.0.x | `/panel/api/inbounds/*` | Client embedded in inbound JSON |
| `V3_EARLY` | v3.1 – v3.2 | `/panel/api/clients/*` | No bulk, no nodePending |
| `V3_NODE_PENDING` | v3.3 – v3.4 | `/panel/api/clients/*` | Update response has `nodePending` |
| `V3_BULK_ENABLE` | v3.5 – v3.6 | `/panel/api/clients/*` | `bulkEnable`/`bulkAdjust` available |
| `V3_SCOPED` | v3.7 | `/panel/api/clients/*` | Scoped tokens, `limitHwid` preservation |
| `V3_CURRENT` | v3.8 | `/panel/api/clients/*` | Bearer 401, partial-write awareness |
| `UNKNOWN_FUTURE` | v3.9+ | Probe-only | Inherits nothing; uses only proven capabilities |
| `BLOCKED` | Any | None | Auth/scope/transport failure; no mutation allowed |

### Evidence discipline

The `KNOWN_BEHAVIOUR_CEILING` in `panel_capabilities.py` is `(3, 11)`.
Versions above this ceiling **never inherit** the newest certified profile.
They start with baseline capabilities and only gain features that the runtime
probe proves exist. This prevents silent breakage when a new 3x-ui version
changes or removes an endpoint.

### Certified families

Families `(3, 7)` through `(3, 11)` carry explicit profiles in `xui_compat.py`.
Families `(3, 1)` through `(3, 6)`
use the baseline profile, which is proven-correct legacy behaviour.

## Adding support for a new version

1. Clone the upstream tag and audit `internal/web/controller/` for endpoint
   changes (new routes, renamed paths, changed request/response shapes).
2. Add a new `PanelCompatibilityProfile` in `xui_compat.py` with evidence
   comments linking each flag to the source file and line.
3. Add the family to `CERTIFIED_FAMILIES` and raise `KNOWN_BEHAVIOUR_CEILING`.
4. Add a new `RenewStrategy` variant if the renewal flow changed.
5. Update `capabilities_from_version()` in `panel_capabilities.py`.
6. Write targeted tests at Tier 1 for the new profile's capability record.

## Upstream source reference

Repository: https://github.com/MHSanaei/3x-ui

Key source paths:

| Path | Purpose |
|------|---------|
| `internal/web/controller/inbound.go` | Inbound CRUD + legacy client endpoints |
| `internal/web/controller/client.go` | First-class client endpoints (v3.1+) |
| `internal/web/controller/api.go` | Token auth, scope enforcement (v3.7+) |
| `internal/web/controller/server.go` | Server status, Xray management |
| `internal/database/model/model.go` | Protocol constants, client model |
| `internal/web/service/setting.go` | Panel settings, subscription paths |
| `internal/web/routes.go` | Route registration |
