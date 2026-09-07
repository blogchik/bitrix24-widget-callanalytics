/**
 * Load the dashboard the way Bitrix24 does - session token in the URL fragment,
 * DOMAIN/PROTOCOL/LANG/APP_SID in the query - and report what a viewer actually sees.
 */
import { chromium } from 'playwright';

const token = process.argv[2];
const out = process.argv[3] ?? 'dashboard.png';
const path = process.argv[4] ?? '/dashboard';
const lang = process.argv[5] ?? 'ru';

const query = `DOMAIN=smoke.bitrix24.kz&PROTOCOL=1&LANG=${lang}&APP_SID=smoke123`;
const url = `http://127.0.0.1:3000${path}?${query}#s=${token}`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1400 } });

const errors = [];
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text());
});
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
page.on('requestfailed', (r) => errors.push(`requestfailed: ${r.url()} ${r.failure()?.errorText}`));

// The SPA calls the API on the same origin; in dev the API is a separate port.
await page.route('**/api/v1/**', async (route) => {
  const target = new URL(route.request().url());
  target.protocol = 'http:';
  target.host = '127.0.0.1:8000';
  const response = await route.fetch({ url: target.toString() });
  await route.fulfill({ response });
});

await page.goto(url, { waitUntil: 'networkidle', timeout: 45000 });
await page.waitForTimeout(2500);

await page.screenshot({ path: out, fullPage: true });

const text = await page.evaluate(() => document.body.innerText);
console.log('--- visible text (first 1600 chars) ---');
console.log(text.slice(0, 1600));
console.log('--- svg marks:', await page.locator('svg rect, svg path, svg line').count());
console.log('--- table rows:', await page.locator('table tbody tr').count());
console.log('--- console errors:', errors.length);
for (const e of errors.slice(0, 12)) console.log('   ', e);

await browser.close();
