/**
 * UI audit: load each view at each breakpoint, screenshot it, and report the
 * measurable defects a screenshot alone will not show.
 *
 *   node ../tools/ui-audit.mjs "<TOKEN>" <outDir> [route] [lang]
 *
 * What it reports per viewport, because these are the ones that make an interface
 * look unfinished and none of them are visible in a static image:
 *   - horizontal overflow of the document and of every element
 *   - text clipped by its own box (scrollWidth > clientWidth on a leaf)
 *   - interactive targets under 44x44 CSS px
 *   - elements whose content is taller than their box (vertical clipping)
 *   - console errors and failed requests
 */
import { chromium } from 'playwright';

const token = process.argv[2];
const outDir = process.argv[3] ?? '.';
const route = process.argv[4] ?? '/dashboard';
const lang = process.argv[5] ?? 'ru';

// `touch` decides two things at once: it makes `(pointer: coarse)` match, so the token
// layer raises --ca-control-h to 44px, and it selects which target floor we hold the page
// to. A blanket 44px rule is wrong for a mouse: a 32px segmented button with 8px of
// separation is correct on a desktop and only reads as a defect to a harness that cannot
// tell a finger from a cursor. So the floor travels with the pointer.
const TOUCH_FLOOR = 44;
const MOUSE_FLOOR = 30;

const VIEWPORTS = [
  { name: '375-phone', width: 375, height: 780, touch: true },
  { name: '768-tablet', width: 768, height: 900, touch: true },
  { name: '1024-slider', width: 1024, height: 900, touch: false },
  { name: '1440-wide', width: 1440, height: 950, touch: false },
];

const query = `DOMAIN=smoke.bitrix24.kz&PROTOCOL=1&LANG=${lang}&APP_SID=audit123`;

//: The single-origin stack from docker-compose.audit.yml.
const BASE = process.env.AUDIT_BASE ?? 'http://127.0.0.1:8080';

const browser = await chromium.launch();
const report = [];

for (const vp of VIEWPORTS) {
  const page = await browser.newPage({
    viewport: { width: vp.width, height: vp.height },
    hasTouch: vp.touch,
  });
  const errors = [];
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text().slice(0, 120)));
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message.slice(0, 120)}`));
  page.on('requestfailed', (r) => {
    const u = r.url();
    // React StrictMode mounts every effect twice in dev, so the first fetch of each pair is
    // aborted by its own AbortController the moment the second starts. Playwright reports
    // that as a failed request; it is the cleanup working, not a broken endpoint.
    const why = r.failure()?.errorText ?? '';
    if (why.includes('ABORTED')) return;
    if (!u.includes('api.bitrix24.com')) errors.push(`failed: ${u.slice(0, 80)} (${why})`);
  });

  // No request rewriting: BASE serves the SPA and the API on one origin, the way our
  // Caddy does in production. Rewriting in the harness produced findings the real app
  // does not have (an intercepted /calls fetch that never resolved).
  await page.goto(`${BASE}${route}?${query}#s=${token}`, {
    waitUntil: 'networkidle',
    timeout: 45000,
  });
  await page.waitForTimeout(2200);

  const findings = await page.evaluate((floor) => {
    const doc = document.documentElement;
    const out = {
      pageOverflow: doc.scrollWidth - doc.clientWidth,
      overflowing: [],
      clippedText: [],
      smallTargets: [],
      verticalClip: [],
    };
    /** True when some ancestor scrolls horizontally, i.e. this content is reachable. */
    const inScroller = (el) => {
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const o = getComputedStyle(p).overflowX;
        if (o === 'auto' || o === 'scroll') return true;
      }
      return false;
    };

    const describe = (el) => {
      const cls = (el.className || '').toString().split(/\s+/).filter(Boolean).slice(0, 3).join('.');
      const txt = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 34);
      return `${el.tagName.toLowerCase()}${cls ? '.' + cls : ''}${txt ? ` «${txt}»` : ''}`;
    };
    // Next.js injects its error/build overlay into the live DOM. It is not our
    // markup and has no production equivalent, so measuring it produces findings
    // that cannot be fixed in this repo.
    const isDevOverlay = (el) =>
      el.closest('nextjs-portal, [data-nextjs-dialog-overlay], [data-nextjs-toast]') !== null;

    for (const el of document.querySelectorAll('body *')) {
      if (isDevOverlay(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const style = getComputedStyle(el);
      if (style.visibility === 'hidden' || style.display === 'none') continue;

      // Past the viewport only counts when nothing between here and the root scrolls
      // sideways. A deliberate horizontal scroller - a segmented track, a wide table - holds
      // content beyond the fold on purpose, and that content is reachable; flagging it makes
      // the one defect that matters (content clipped with no way to reach it) unfindable in
      // the noise. The page-level `pageOverflow` figure above is what catches a real one.
      if ((r.right > doc.clientWidth + 1 || r.left < -1) && !inScroller(el)) {
        out.overflowing.push(`${describe(el)} [x ${Math.round(r.left)}→${Math.round(r.right)}]`);
      }
      const leaf = el.children.length === 0 && (el.textContent || '').trim();
      if (leaf && el.scrollWidth > el.clientWidth + 1 && style.overflow !== 'visible') {
        out.clippedText.push(`${describe(el)} [${el.scrollWidth}>${el.clientWidth}]`);
      }
      if (el.scrollHeight > el.clientHeight + 1 && style.overflowY === 'hidden') {
        out.verticalClip.push(`${describe(el)} [${el.scrollHeight}>${el.clientHeight}]`);
      }
      // `cursor: pointer` is inherited, so every span, svg and path inside a button
      // matches it. Measuring those reports a 16px icon as an undersized target when the
      // button around it is 44px - a finding that cannot be acted on, and that hides the
      // real ones. Only the outermost interactive element is the target.
      const interactive =
        ['BUTTON', 'A', 'SELECT', 'INPUT', 'TEXTAREA'].includes(el.tagName) ||
        ['button', 'link', 'option', 'tab', 'switch', 'checkbox', 'radio'].includes(
          el.getAttribute('role') ?? '',
        );
      const tappable =
        interactive ||
        (style.cursor === 'pointer' &&
          el.parentElement?.closest(
            'button, a, select, input, textarea, [role="button"], [role="option"], [role="tab"]',
          ) === null);
      if (tappable && (r.width < floor || r.height < floor)) {
        out.smallTargets.push(`${describe(el)} [${Math.round(r.width)}×${Math.round(r.height)}]`);
      }
    }
    const uniq = (a) => [...new Set(a)];
    out.overflowing = uniq(out.overflowing).slice(0, 12);
    out.clippedText = uniq(out.clippedText).slice(0, 12);
    out.smallTargets = uniq(out.smallTargets).slice(0, 14);
    out.verticalClip = uniq(out.verticalClip).slice(0, 8);
    return out;
  }, vp.touch ? TOUCH_FLOOR : MOUSE_FLOOR);

  const file = `${outDir}/${route.replace(/\W+/g, '_')}-${vp.name}.png`;
  await page.screenshot({ path: file, fullPage: true });
  report.push({
    viewport: vp.name,
    file,
    floor: vp.touch ? TOUCH_FLOOR : MOUSE_FLOOR,
    errors: [...new Set(errors)].slice(0, 6),
    ...findings,
  });
  await page.close();
}

await browser.close();

for (const r of report) {
  console.log(`\n=== ${r.viewport} ===`);
  console.log(`  page horizontal overflow: ${r.pageOverflow}px ${r.pageOverflow > 0 ? '<-- SCROLLS SIDEWAYS' : 'ok'}`);
  const section = (label, items) => {
    if (!items.length) return;
    console.log(`  ${label} (${items.length}):`);
    for (const i of items) console.log(`    - ${i}`);
  };
  section('elements past the viewport', r.overflowing);
  section('text clipped by its own box', r.clippedText);
  section('vertically clipped', r.verticalClip);
  section(`targets under ${r.floor}px (${r.floor === 44 ? 'touch' : 'mouse'})`, r.smallTargets);
  section('console errors', r.errors);
}
