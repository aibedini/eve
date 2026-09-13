// Visual regression for the Subscription page (report: oversized Support/QR icons and collapsed
// columns, intermittently).
//
// The assertions encode the three symptoms:
//   1. no icon is larger than its container expects (the reported blow-up),
//   2. the page never scrolls horizontally at any phone/tablet/desktop width,
//   3. the package grid keeps a stable column count at each breakpoint.
//
// It needs a real browser and a real page:
//   npm i -D @playwright/test && npx playwright install chromium
//   EVE_E2E_SUB_URL="https://eve.example/s/1/<token>" npx playwright test -c e2e/visual
// Without EVE_E2E_SUB_URL the suite skips instead of failing, so it can live in the repo before a
// CI job provisions a page. See docs/operations/VISUAL_REGRESSION.md.
const { test, expect } = require('@playwright/test');

const SUB_URL = process.env.EVE_E2E_SUB_URL || '';
const WIDTHS = [360, 390, 640, 768, 1024, 1280, 1440];

// The page's own scoped rules (templates/subscription.html): nothing may exceed these by more
// than a small tolerance for borders/zoom rounding.
const ICON_LIMITS = [
  { selector: '.subscription-page .pwa-nav-item svg', max: 22 },
  { selector: '.subscription-page .fab-circle svg', max: 22 },
  { selector: '.subscription-page .renew-ch-btn svg', max: 18 },
  { selector: '.subscription-page .pwa-sheet-btn svg', max: 18 },
  { selector: '.subscription-page #support-sheet svg', max: 18 },
  { selector: '.subscription-page #toast > svg', max: 16 },
  { selector: '.subscription-page #qr-modal svg', max: 20 },
];

test.describe('subscription page layout', () => {
  test.skip(!SUB_URL, 'set EVE_E2E_SUB_URL to a live subscription page to run this suite');

  for (const width of WIDTHS) {
    test(`holds its layout at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      const response = await page.goto(SUB_URL, { waitUntil: 'domcontentloaded' });
      expect(response.headers()['x-eve-build']).toBeTruthy();

      // 1. icon geometry: an oversized icon is the reported symptom, so assert an upper bound
      //    rather than an exact size (zoom and borders may add a pixel).
      for (const { selector, max } of ICON_LIMITS) {
        const boxes = await page.locator(selector).evaluateAll((nodes) =>
          nodes.map((node) => {
            const rect = node.getBoundingClientRect();
            return { w: rect.width, h: rect.height };
          }));
        for (const box of boxes) {
          expect(box.w, `${selector} width at ${width}px`).toBeLessThanOrEqual(max + 2);
          expect(box.h, `${selector} height at ${width}px`).toBeLessThanOrEqual(max + 2);
          expect(box.w, `${selector} must not collapse`).toBeGreaterThan(0);
        }
      }

      // 2. no horizontal overflow anywhere on the page.
      const overflow = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
        offenders: Array.from(document.querySelectorAll('*'))
          .filter((node) => node.getBoundingClientRect().right > window.innerWidth + 2)
          .slice(0, 5)
          .map((node) => `${node.tagName}.${node.className}`.slice(0, 80)),
      }));
      expect(overflow.scrollWidth, `horizontal overflow at ${width}px: ${overflow.offenders}`)
        .toBeLessThanOrEqual(overflow.clientWidth + 1);

      // 3. the package cards keep a stable column count for the breakpoint.
      const columns = await page.evaluate(() => {
        const cards = Array.from(document.querySelectorAll('.pkg-renew-card, .pkg-card'));
        const tops = new Set(cards.map((card) => Math.round(card.getBoundingClientRect().top)));
        return { cards: cards.length, rows: tops.size };
      });
      if (columns.cards > 0) {
        const expectedRows = width >= 1024 ? 1 : (width >= 640 ? 1 : columns.cards);
        expect(columns.rows, `unexpected card rows at ${width}px`)
          .toBeLessThanOrEqual(Math.max(expectedRows, 1));
      }

      // The build the page reports must match the build its assets report, which is the
      // version-skew guard in docs/operations/BUILD_IDENTITY.md.
      const meta = await page.locator('meta[name="eve-build"]').getAttribute('content');
      expect(meta).toBe(response.headers()['x-eve-build']);
    });
  }
});
