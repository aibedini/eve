# Contract — Panel Compatibility Profiles

**Feature**: 001-3xui-37-38-compat
**Status**: normative for this feature

## Profile selection (total function)

```text
select_profile(version) ->
    if version is not parseable                  -> baseline_v3  (state: unverified)
    family = (version.major, version.minor)
    if family == (3, 7)                          -> xui_3_7
    if family == (3, 8)                          -> xui_3_8
    otherwise                                    -> baseline_v3  (state: certification_required)
```

**Guarantees**

- G1: `select_profile` is total — every input yields a profile.
- G2: exactly one family selects each non-baseline profile.
- G3: no input outside `{(3,7), (3,8)}` selects `xui_3_7` or `xui_3_8`.
- G4: patch component never affects selection.
- G5: an optional leading `v` never affects selection.
- G6: an unparseable or absent version never selects a non-baseline profile.
- G7: the baseline profile is a superset of today's pre-feature behaviour — i.e.
  selecting it changes no request EVE already issues to a supported panel.

## Behaviour gating table

| Behaviour | baseline_v3 | xui_3_7 | xui_3_8 |
| --- | --- | --- | --- |
| Send `X-Requested-With: XMLHttpRequest` on the probe | no | **yes** | no |
| Treat 401 as auth-invalid (not legacy) | no | yes (via hint header) | yes |
| Treat 403 as scope-insufficient (not legacy) | no | yes | yes |
| Surface scoped/expiring-token contract | no | yes | yes |
| Surface panel-side lifecycle automation | no | yes | yes |
| Prefer panel-advertised subscription path | no | no | **yes** |
| Advertise `amneziawg` capability | no | yes | yes |
| Advertise `tuic` capability | no | no | **yes** |

> Device-limit preservation is **not** in this table by design. It is gated on the
> panel exposing the field (observed contract), not on the profile — see
> research D3. This is deliberate and is the safer reading of the gating rule.

## Non-regression contract for the baseline profile

For any panel that today resolves to "legacy" or to an already-supported v3
family, the exact HTTP method, path, headers and body EVE issues before this
feature MUST remain identical after it, except for the additive read described in
`panel-client-preservation.md` (which is a new read, never a modified write).
