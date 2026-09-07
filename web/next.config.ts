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
};

export default withNextIntl(nextConfig);
