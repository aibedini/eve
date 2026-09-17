# Quickstart — Validate 3x-ui 3.7.x / 3.8.x Compatibility

**Feature**: 001-3xui-37-38-compat

## Prerequisites

- EVE checkout on `feat/3xui-37-38-compat`
- Test interpreter: `.venv-test\\Scripts\\python.exe` (pytest 9.1.1)
- One disposable 3.8.x panel for controlled acceptance (never a production panel)
- No real 3.7.x panel is required; that acceptance step is waived by product decision

## 1. Unit + contract layer (no panel required)

```powershell
.venv-test\Scripts\python.exe -m pytest tests/test_3xui_compat.py -q
```

Expected: version normalisation (`v3.8.0`, `3.8`, `3.7.9`), family mapping,
whitelist-only profile selection, and `3.9.0`/`4.0.0`/`dev+`/empty never selecting
a certified profile.

## 2. Local disposable panel (3.8.x)

```powershell
# official Windows release build, extracted outside the repo
cd <build-tools>\xui-panels\v3.8.0\x-ui
.\x-ui.exe setting -port 20581 -webBasePath / -username admin -password <pw>
.\x-ui.exe setting -getApiToken -tokenName eve-test    # prints apiToken: <value>
.\x-ui.exe run                                          # keep running
```

Sanity checks:

```powershell
# local, internet-independent version source
GET http://127.0.0.1:20581/panel/api/server/status        -> obj.panelVersion
# corroborating source (needs the panel to reach GitHub)
GET http://127.0.0.1:20581/panel/api/server/getPanelUpdateInfo -> obj.currentVersion
```

## 3. The P0 preservation proof (the reason this feature exists)

```text
1. create a VLESS inbound + one client
2. set limitHwid = 2   (POST /clients/update/{email} with the sibling field)
3. read back            GET /clients/get/{email} -> obj.client.limitHwid == 2
4. run each EVE mutation path (renew, edit, enable, reset, rotate)
5. read back            -> obj.client.limitHwid MUST still be 2
```

For the controlled 3.8.x acceptance, a test that only inspects EVE's outbound
JSON is **not** acceptable evidence: the defect is a server-side default, so the
assertion must be on the **persisted** value. For 3.7.x, the required release
evidence is the automated/contract preservation matrix; real-panel execution is
intentionally excluded from this release's acceptance scope.

## 4. Version-gating proof

Drive detection with `3.5.2`, `3.6.1`, `3.7.0`, `3.7.9`, `3.8.0`, `3.8.1`,
`3.9.0`, `4.0.0`, `v3.8.0`, `dev+`, ``, `garbage`:

| Input | Expected profile | Expected state |
| --- | --- | --- |
| `3.7.0`, `3.7.9`, `v3.7.4` | xui_3_7 | supported |
| `3.8.0`, `3.8.1`, `v3.8.0` | xui_3_8 | supported |
| `3.5.2`, `3.6.1` | baseline_v3 | supported |
| `3.9.0`, `4.0.0` | baseline_v3 | certification_required |
| `dev+`, ``, garbage | baseline_v3 | unverified |

## 5. Auth-matrix proof

With `admin`, `monitor`, `node-sync`, expired, rotated and invalid tokens:

- admin ⇒ supported, version detected
- monitor / node-sync ⇒ `scope_insufficient`; **never** reported as legacy
- expired / rotated / invalid ⇒ `auth_invalid`; **never** reported as legacy, and
  the capability cache is not poisoned to `unsupported`

## 6. Regression

```powershell
.venv-test\Scripts\python.exe -m pytest -q
.venv-test\Scripts\python.exe scripts/release_check.py --json
.venv-test\Scripts\python.exe scripts/ui_design_audit.py --check
.venv-test\Scripts\python.exe -m pytest tests/test_docs_index.py -q
```

## 7. Definition of Done and verdict rules

Required release gates:

- 3.7 version-profile tests pass
- auth matrix passes
- `limitHwid` preservation passes
- field-preservation matrix passes
- lifecycle automation guard passes
- existing protocol regression tests pass
- full EVE suite passes
- `release_check` passes
- docs guard passes
- UI design audit passes
- `git diff --check` passes

Final compatibility verdict:

- Core 3.7 compatibility: **PASS — automated/contract verified**
- Real-panel acceptance: **NOT RUN — waived by product decision**
- Core 3.8 compatibility: **PASS — including controlled real-panel acceptance**

T048/T053 are **WAIVED / NOT REQUIRED FOR RELEASE**. Reason: product decision —
real 3.7 panel acceptance intentionally excluded from acceptance scope. This is
not a known gap and does not cap or downgrade the 3.7 core verdict. Extended
features (TUIC, AmneziaWG, HWID UI) are reported separately and never downgrade
a core verdict.
