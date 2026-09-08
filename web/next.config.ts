import createNextIntlPlugin from 'next-intl/plugin';
import type { NextConfig } from 'next';

// next-intl in non-routing mode: one request config, no locale path segment (§8).
const withNextIntl = createNextIntlPlugin('./src/i18n/config.ts');

const nextConfig: NextConfig = {
  // The web container is served by the host Caddy, so ship the minimal
  // self-contained server instead of a full node_modules tree (§2).
  output: 'standalone',
  reactStrictMode: true,
  // §4.10: nothing here may add X-Frame-Options; framing is controlled per request
  // by middleware.ts through Content-Security-Policy: frame-ancestors.
  poweredByHeader: false,
  // Dev-only: the floating build/route indicator is a fixed-position overlay that
  // sits on top of the app inside the Bitrix24 iframe and shows up in the Playwright
  // UI audit as an undersized, off-viewport target that is not ours. It has no
  // production equivalent, so turning it off costs nothing and stops the harness
  // measuring Next.js instead of us.
  devIndicators: { appIsrStatus: false, buildActivity: false },
};

export default withNextIntl(nextConfig);
