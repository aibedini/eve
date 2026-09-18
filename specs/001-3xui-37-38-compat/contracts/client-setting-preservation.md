# Contract — Client Setting Preservation

**Feature**: 001-3xui-37-38-compat

## The upstream hazard (exact shape)

`POST {base}{webPath}/panel/api/clients/update/{email}`

```go
// internal/web/controller/client.go — identical in v3.7.0 and v3.8.0
var req struct {
    model.Client
    LimitHwid int `json:"limitHwid"`   // sibling; absent ⇒ 0
}
```

`internal/database/model/model.go`: `model.Client` has **no** `limitHwid` field.

`internal/web/service/client_crud.go:806` → `internal/web/service/client_hwid.go:296`:

```go
UpdateColumn("limit_hwid", limit)   // unconditional
```

**Therefore: omitting `limitHwid` writes `0` (unlimited).** Empirically confirmed
on a live 3.8.0 panel: `2 → 0`.

## Required EVE behaviour

### Read (before an unrelated mutation)

```http
GET {base}{webPath}/panel/api/clients/get/{email}
```

→ `obj.client.limitHwid` (integer).

This is the only per-client authoritative read; it is the same endpoint EVE
already uses in `_v3_get_client`. `/clients/list/paged` also exposes the field
but is paginated and is therefore not used for a single-client mutation.

### Write (every v3 client mutation)

| Case | Payload |
| --- | --- |
| Panel record exposes `limitHwid` and the operator did not ask to change it | include the read value verbatim |
| Panel record exposes `limitHwid` and the operator asked to change it | include the requested value |
| Panel record does NOT expose `limitHwid` | omit the field entirely |
| Authoritative read failed | **do not send a payload that would clear it**; fail the mutation with an actionable error |

### Forbidden

- `limitHwid = 0` as a default.
- `limitHwid` derived from any config value, profile flag or guess.
- Treating a read value of `0` as "unset" — `0` is a legitimate operator choice
  ("no device limit") and must round-trip as `0`.

## Covered mutation paths (all must satisfy this contract)

`renew` · `change volume / edit` · `enable` · `disable / re-enable` ·
`reset traffic` · `rotate` · `admin / superadmin edit` · `add client` (where the
panel already holds the identity).

## Field-preservation matrix (unrelated fields that must survive)

Verified present in the payload/response contracts of the certified families:

`resetDay`, `resetMax`, `trafficReset`, `trafficResetDay`, `limitHwid`,
`disableFlow`, `allowedIPsByInbound`, `forwardedPorts`, `keepAlive`, `flow`,
`subId`, `comment`, `enable`, `expiryTime`, `totalGB`.

> `settings.clients[]` from `/inbounds/list` was verified to carry `resetDay`,
> `resetMax`, `trafficReset`, `trafficResetDay` but **not** `limitHwid` — which is
> precisely why the extra authoritative read exists for that one field.

## Secrets

EVE MUST NOT read back or resubmit fields the upstream contract treats as
write-only or secret-bearing (node API tokens, panel credentials). Preservation
applies only to the client-record fields listed above.
