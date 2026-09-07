import type { Metadata, Viewport } from 'next';
import { NextIntlClientProvider } from 'next-intl';
import { getLocale, getMessages, getTranslations } from 'next-intl/server';
import type { ReactNode } from 'react';

import AppFrame from '@/components/AppFrame';

import './globals.css';

/**
 * The document every route renders into.
 *
 *  * `<html lang>` is the locale resolved from Bitrix24's `LANG` (§8), which reaches
 *    the request config as the `x-ca-lang` header `middleware.ts` sets.
 *  * The intl context is provided here so that the pages - all of them Client
 *    Components, because they read the session token the fragment delivered - can use
 *    the one shared catalogue.
 *  * `AppFrame` owns the two iframe duties: capturing `#s=` (§4.6) and
 *    `BX24.fitWindow()` (§4.10).
 */

/**
 * Every response is per portal, per user and per request (§4.10), and the locale comes
 * from a request header, so nothing here may be statically rendered or cached.
 */
export const dynamic = 'force-dynamic';

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
};

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations('common');
  return {
    title: t('appName'),
    // The app is only ever reached from inside a Bitrix24 slider.
    robots: { index: false, follow: false },
  };
}

export default async function RootLayout({ children }: { children: ReactNode }) {
  const locale = await getLocale();
  const messages = await getMessages();

  return (
    <html lang={locale}>
      <body>
        <NextIntlClientProvider
          locale={locale}
          messages={messages}
          // The viewer's real timezone is the JWT `tz` claim (§4.6) and only arrives
          // with `GET /me`; date formatting therefore passes it explicitly
          // (`lib/format.ts`). UTC here is only next-intl's default for anything that
          // forgets to, which must never silently become the browser's zone.
          timeZone="UTC"
        >
          <AppFrame>{children}</AppFrame>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
