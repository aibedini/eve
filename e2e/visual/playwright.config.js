// Playwright configuration for the Subscription page visual regression.
// Run: npx playwright test -c e2e/visual   (after: npx playwright install chromium)
const { defineConfig, devices } = require('@playwright/test');

const WIDTHS = [360, 390, 640, 768, 1024, 1280, 1440];

module.exports = defineConfig({
  testDir: __dirname,
  timeout: 60000,
  retries: 0,
  reporter: [['list']],
  use: {
    ignoreHTTPSErrors: true,
    trace: 'retain-on-failure',
  },
  // One project per width the report names, so a failure states the width in its title.
  projects: WIDTHS.map((width) => ({
    name: `w${width}`,
    use: {
      ...devices['Desktop Chrome'],
      viewport: { width, height: 900 },
    },
  })),
});
