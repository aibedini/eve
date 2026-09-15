# Contract — Panel Version Detection

**Feature**: 001-3xui-37-38-compat

## Sources (in priority order)

### S1 — authoritative, local, preferred

```http
GET {base}{webPath}/panel/api/server/status
Authorization: Bearer <admin token>
```

Response: `{"success":true,"obj":{ ..., "panelVersion":"3.8.0", ... }}`

- Read `obj.panelVersion`.
- Verified present in `internal/web/service/server.go` in **both** v3.7.0 and
  v3.8.0, and continuously since v3.3.1.
- Requires no outbound internet from the panel.
- EVE already performs this request and already extracts the field as
  `xui_version`; the feature normalises it rather than adding a request.

### S2 — corroborating, internet-dependent

```http
GET {base}{webPath}/panel/api/server/getPanelUpdateInfo
Authorization: Bearer <admin token>
```

Response (success): `{"success":true,"obj":{"channel":"stable","currentVersion":"3.8.0","latestVersion":"v3.8.0","updateAvailable":false}}`

Response (panel cannot reach GitHub): `{"success":false}` — **no `obj`, no version**.

- Read `obj.currentVersion`.
- MUST NOT be the only source: its failure must not degrade a version that S1
  already resolved (FR-003).
- MAY confirm an S1 result (`confidence = corroborated`).

## Outcome table

| S1 panelVersion | S2 currentVersion | Result |
| --- | --- | --- |
| `3.7.4` | any / fails | xui_3_7, confidence `authoritative` |
| `3.8.1` | any / fails | xui_3_8, confidence `authoritative` |
| `3.5.2` | any | baseline_v3, certified |
| `3.9.0` / `4.0.0` | any | baseline_v3 + `future_version_uncertified` warning |
| absent / unparseable | `3.8.0` | xui_3_8, confidence `corroborated` |
| absent / unparseable | absent / fails | baseline_v3 + `panel_version_unknown`, unverified |
| request fails (transport) | not attempted | baseline_v3 + `panel_version_unknown`; **never guessed** |

## Negative guarantees

- N1: A failed or ambiguous detection NEVER selects `xui_3_7` or `xui_3_8`.
- N2: No destructive mutation depends on a guessed version.
- N3: Capability-probe results never promote a version.
- N4: A 401/403 from any source is reported as an auth condition and never cached
  as `unsupported`.
