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
  //
  // `false`, not the `{appIsrStatus, buildActivity}` pair this used to be: Next 15.2
  // collapsed those two flags into one boolean and warns on the old shape.
  devIndicators: false,

  async redirects() {
    return [
      // The licence agreement has one address, /license, and no alias: the owner does not
      // want a second spelling of it in circulation. Only the privacy policy keeps its
      // longer form redirecting, because that one is a wording, not a spelling.
      { source: '/privacy-policy', destination: '/privacy', permanent: true },
    ];
  },
};

export default withNextIntl(nextConfig);
