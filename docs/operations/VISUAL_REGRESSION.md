# Visual regression: the Subscription page

## Why

The reported bug was visual and intermittent: oversized Support/QR icons and collapsed columns,
sometimes. The static/deploy half is fixed and guarded (`docs/performance/STATIC_ASSETS.md`,
`docs/operations/BUILD_IDENTITY.md`); this suite guards the visual half so a stylesheet change
that breaks the page at one width is caught rather than reported by a customer.

## What it asserts

At each of **360, 390, 640, 768, 1024, 1280 and 1440 px** (`e2e/visual/playwright.config.js`
creates one project per width):

1. **icon geometry** - every scoped icon rule (bottom nav, fab, renew-channel buttons, support
   sheet, toast, QR modal) stays within its declared size, and never collapses to zero;
2. **no horizontal overflow** - `documentElement.scrollWidth` may not exceed the viewport, and the
   first offending elements are named in the failure message;
3. **stable column layout** - the package cards keep the column count the breakpoint expects;
4. **build agreement** - the `eve-build` meta tag equals the `X-Eve-Build` response header, which
   is the same version-skew guard the deploy contract uses.

## Running it

```bash
npm i -D @playwright/test
npx playwright install chromium
EVE_E2E_SUB_URL="https://eve.example/s/1/<subscription-token>" npx playwright test -c e2e/visual
```

Without `EVE_E2E_SUB_URL` every test **skips** (it does not fail), so the suite can live in the
repository before a CI job provisions a page. A CI job should:

1. deploy a throwaway instance (or point at staging),
2. create one account and read its subscription token,
3. export `EVE_E2E_SUB_URL` and run the command above,
4. upload `test-results/` on failure (the config keeps a trace when a test fails).

## Status in this repository

The spec, the configuration and the width list are committed, and
`tests/test_subscription_visual_harness.py` asserts they keep covering all seven widths and the
three assertion groups. **The browser run itself has not been executed in this development
environment**: Playwright is not installed here and the browsers are a separate download, so the
suite is ready for CI rather than proven locally. The static half of the same guarantees (scoped
icon sizes, no new global selector, content-versioned assets) is enforced by
`tests/test_subscription_scope.py` and `tests/test_static_deploy_contract.py`, which do run here.

## Related

* `docs/performance/STATIC_ASSETS.md` - the version-identity investigation and the fix
* `docs/operations/BUILD_IDENTITY.md` - the deployment contract and the diagnosis checklist
* `tests/design_global_selector_baseline.json` - the frozen at-risk global selector set
