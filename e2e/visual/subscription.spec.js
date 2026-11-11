// Visual contract for a live subscription page at every configured viewport.
const { test, expect } = require('@playwright/test');

const SUB_URL = process.env.EVE_E2E_SUB_URL || '';
const WIDTHS = [320, 360, 390, 428, 640, 768, 1024, 1280, 1440];
const ICON_LIMITS = [
  { selector: '.subscription-page .pwa-nav-item svg', max: 22 },
  { selector: '.subscription-page .fab-circle svg', max: 22 },
  { selector: '.subscription-page .renew-ch-btn svg', max: 18 },
  { selector: '.subscription-page .pwa-sheet-btn svg', max: 18 },
  { selector: '.subscription-page #support-sheet svg', max: 18 },
  { selector: '.subscription-page #toast > svg', max: 16 },
  { selector: '.subscription-page #qr-modal svg', max: 20 },
];

test.skip(!SUB_URL, 'EVE_E2E_SUB_URL is required for a live subscription page');

test('subscription remains readable and contained in both themes', async ({ page }) => {
  const width = page.viewportSize().width;
  expect(WIDTHS).toContain(width);
  const response = await page.goto(SUB_URL);
  expect(response.headers()['x-eve-build']).toBeTruthy();

  for (const theme of ['dark', 'light']) {
    await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
    for (const { selector, max } of ICON_LIMITS) {
      const boxes = await page.locator(selector).evaluateAll((nodes) => nodes.map((node) => {
        const rect = node.getBoundingClientRect();
        return { w: rect.width, h: rect.height };
      }));
      for (const box of boxes) {
        expect(box.w, `${selector} width at ${width}px`).toBeLessThanOrEqual(max + 2);
        expect(box.h, `${selector} height at ${width}px`).toBeLessThanOrEqual(max + 2);
        expect(box.w, `${selector} must not collapse`).toBeGreaterThan(0);
      }
    }

    for (const id of ['sub-inbound-toggle', 'renew-toggle-btn']) {
      const button = page.locator(`#${id}`);
      if (await button.getAttribute('aria-expanded') === 'false') await button.click();
    }
    await page.waitForTimeout(450);

    const layout = await page.evaluate(() => {
      const inside = (item, parent) => {
        const child = item.getBoundingClientRect();
        const outer = parent.getBoundingClientRect();
        return child.left >= outer.left - 2 && child.right <= outer.right + 2;
      };
      const packages = [...document.querySelectorAll('.pkg-renew-card')];
      const packageGrid = document.querySelector('.pkg-renew-grid');
      const servers = [...document.querySelectorAll('.sub-inbound-card')];
      const serverGrid = document.querySelector('.sub-inbound-grid');
      const ratio = (front, back) => {
        const rgb = (value) => value.match(/[\d.]+/g).slice(0, 3).map(Number);
        const light = (value) => rgb(value).map((channel) => {
          const n = channel / 255;
          return n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4;
        }).reduce((sum, n, i) => sum + n * [0.2126, 0.7152, 0.0722][i], 0);
        const a = light(front), b = light(back);
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      };
      return {
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
        packageColumns: packageGrid ? getComputedStyle(packageGrid).gridTemplateColumns.split(' ').length : 0,
        packageCount: packages.length,
        packagesInside: packages.every((item) => inside(item, packageGrid)),
        serverColumns: serverGrid ? getComputedStyle(serverGrid).gridTemplateColumns.split(' ').length : 0,
        serverCount: servers.length,
        serversInside: servers.every((item) => inside(item, serverGrid)),
        statusFits: document.getElementById('sub-inbound-body').clientHeight + 1 >= document.getElementById('sub-inbound-body').scrollHeight,
        renewFits: document.getElementById('renew-body').clientHeight + 1 >= document.getElementById('renew-body').scrollHeight,
        statusNameContrast: servers.length ? ratio(getComputedStyle(servers[0].querySelector('.sub-inbound-name')).color,
          getComputedStyle(servers[0]).backgroundColor) : null,
      };
    });

    expect(layout.scrollWidth, `horizontal overflow at ${width}px in ${theme}`).toBeLessThanOrEqual(layout.clientWidth + 1);
    expect(layout.statusFits, `server status clipped at ${width}px in ${theme}`).toBeTruthy();
    expect(layout.renewFits, `renewal clipped at ${width}px in ${theme}`).toBeTruthy();
    expect(layout.packagesInside).toBeTruthy();
    expect(layout.serversInside).toBeTruthy();
    if (layout.packageCount) expect(layout.packageColumns).toBe(width < 640 ? 2 : 3);
    if (layout.serverCount && width < 640) expect(layout.serverColumns).toBe(2);
    if (layout.statusNameContrast !== null) expect(layout.statusNameContrast).toBeGreaterThanOrEqual(4.5);
  }

  const meta = await page.locator('meta[name="eve-build"]').getAttribute('content');
  expect(meta).toBe(response.headers()['x-eve-build']);
});
