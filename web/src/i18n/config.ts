/**
 * next-intl wiring over the SHARED locale definition (§8).
 *
 * Two rules from §8 shape this file:
 *
 *  * **One locale definition.** `src/i18n/locales.json` (locale list + the `kz->ru`,
 *    `uz->ru` fallbacks) is read here and by `api/app/i18n.py`; neither side keeps a
 *    second copy. `resolveLocale()` below is a line-for-line mirror of
 *    `app.i18n.resolve_locale`, so a portal that renders `state.html` server-side and
 *    then lands on the SPA sees the same language.
 *  * **Non-routing mode.** The locale is Bitrix24's `LANG` query parameter (forwarded
 *    verbatim by `handoff.html`, §4.4 step 8), never a URL segment: the app is opened
 *    inside an iframe at a fixed path and there is no place to put `/ru/`. `LANG`
 *    reaches this module as the `x-ca-lang` request header that `middleware.ts` sets
 *    after validating it, because a Server Component layout cannot read `searchParams`.
 *
 * Loading never throws: §4.11 makes a blank frame a moderation rejection, so a missing
 * catalogue degrades to the default locale's bundle rather than to an error page.
 */
import type { AbstractIntlMessages } from 'next-intl';
import { getRequestConfig } from 'next-intl/server';
import { headers } from 'next/headers';

import localesConfig from './locales.json';

/** Locales that have a `messages/<code>.json`, in declaration order. */
export const LOCALES: readonly string[] = localesConfig.locales;

/** Used when `LANG` is absent entirely - the app's primary market is ru. */
export const DEFAULT_LOCALE: string = localesConfig.default;

/** `kz -> ru`, `uz -> ru`: those portals are Russian-speaking in practice (§8). */
export const FALLBACK: Readonly<Record<string, string | undefined>> =
  localesConfig.fallback as Record<string, string | undefined>;

/** §8: anything the fallback map does not name falls back to English. */
const UNKNOWN_LOCALE = 'en';

/**
 * Request header carrying Bitrix24's `LANG`.
 *
 * `middleware.ts` sets it from the (validated) query parameter. It is a plain two
 * letter code, never a token: §4.6 keeps the JWT out of every header we emit or read.
 */
export const LANG_HEADER = 'x-ca-lang';

/** Query parameter Bitrix24 uses; `handoff.html` forwards it verbatim. */
export const LANG_PARAM = 'LANG';

/**
 * Bitrix24's `LANG` (or any tag) -> one of {@link LOCALES}.
 *
 * Mirrors `app.i18n.resolve_locale`: exact match wins, then the shared fallback map,
 * then English, and an absent value becomes `locales.json`'s `default`.
 */
export function resolveLocale(lang: string | null | undefined): string {
  if (!lang) {
    return DEFAULT_LOCALE;
  }
  // "ru-RU", "RU", "ru_RU" all arrive from one cabinet or another.
  const code = lang.trim().toLowerCase().replace(/_/g, '-').split('-', 1)[0] ?? '';
  if (LOCALES.includes(code)) {
    return code;
  }
  const mapped = FALLBACK[code];
  if (mapped !== undefined && LOCALES.includes(mapped)) {
    return mapped;
  }
  return LOCALES.includes(UNKNOWN_LOCALE) ? UNKNOWN_LOCALE : DEFAULT_LOCALE;
}

type Messages = AbstractIntlMessages;

async function loadMessages(locale: string): Promise<Messages> {
  // Template literal import: the bundler turns `messages/` into a context module, so
  // every declared locale ships without an explicit import per language (§8: "adding
  // Uzbek = drop uz.json, add one entry to locales.json").
  const bundle = (await import(`../../messages/${locale}.json`)) as { default: Messages };
  return bundle.default;
}

export default getRequestConfig(async () => {
  const requestHeaders = await headers();
  const locale = resolveLocale(requestHeaders.get(LANG_HEADER));

  let messages: Messages;
  try {
    messages = await loadMessages(locale);
  } catch {
    // A declared locale without a bundle is a build mistake, not a reason to blank the
    // frame (§4.11). Fall back to the default catalogue and let the CI key check fail.
    // eslint-disable-next-line no-console
    console.error(`i18n: no message bundle for "${locale}", falling back to "${DEFAULT_LOCALE}"`);
    messages = await loadMessages(DEFAULT_LOCALE);
  }

  return { locale, messages };
});
