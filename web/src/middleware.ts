/**
 * Per-request framing policy for the Bitrix24 iframe (§4.10).
 *
 * This file is the only thing standing between the SPA and being framed by an
 * arbitrary origin, so it is deliberately paranoid and deliberately small:
 *
 *  * `Content-Security-Policy: frame-ancestors 'self' <scheme>://<DOMAIN>` is built
 *    from the `DOMAIN`/`PROTOCOL` query parameters that `handoff.html` forwards
 *    verbatim (§4.4 step 8) - but only after re-validating them here. They are
 *    attacker-controllable text that ends up inside a response header, so anything
 *    that is not a bare RFC hostname (a scheme, a path, a space, a comma, a second
 *    colon, an out-of-range port) fails CLOSED to `frame-ancestors 'none'`.
 *  * **Never `X-Frame-Options`.** It has no origin list, so a single stray header
 *    anywhere blanks the app inside Bitrix24. The middleware also deletes it, in case
 *    a proxy in front of the container ever adds one.
 *  * Without a valid `DOMAIN` the page still renders - `frame-ancestors 'none'` plus
 *    the "Open this app from Bitrix24" copy of `/` - because §4.11 makes a blank frame
 *    a moderation rejection.
 *
 * It also forwards Bitrix24's `LANG` to the server render as `x-ca-lang`: next-intl
 * runs in non-routing mode (§8) and a Server Component layout cannot read
 * `searchParams`, so the locale has to travel as a request header.
 */
import { NextResponse, type NextRequest } from 'next/server';

/**
 * RFC hostname, anchored, no port (the port is split off first).
 *
 * Mirrors `_validate_hostname` in `api/app/bitrix/forms.py` and `_HOST_RE` in
 * `api/app/handlers/render.py`, so the SPA and the server-rendered pages agree on
 * which portals may frame us.
 */
const HOST_RE =
  /^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/;

/** §4.2: `LANG ~ ^[a-z]{2}$`. Anything else is dropped, not "sanitised". */
const LANG_RE = /^[a-z]{2}$/;

/** §8 / `src/i18n/config.ts`: the header the request config reads the locale from. */
const LANG_HEADER = 'x-ca-lang';

const CSP_NO_FRAMING = "frame-ancestors 'none'";

/**
 * Case-insensitive query lookup.
 *
 * Bitrix24 mixes cases across cabinets (`api/app/bitrix/forms.py::_ci_index` does the
 * same on the body); first occurrence wins, deterministically.
 */
function param(url: URL, name: string): string | null {
  const wanted = name.toLowerCase();
  for (const [key, value] of url.searchParams.entries()) {
    if (key.toLowerCase() === wanted) {
      return value;
    }
  }
  return null;
}

/**
 * The `frame-ancestors` value for one portal, or `'none'`.
 *
 * `'none'` is the answer whenever we cannot prove which portal is embedding us: an
 * absent or malformed `DOMAIN`, or a `PROTOCOL` outside `{0,1}`. Guessing an origin
 * here would let any site frame the app.
 */
function frameAncestors(domainRaw: string | null, protocolRaw: string | null): string {
  if (protocolRaw !== null && protocolRaw !== '0' && protocolRaw !== '1') {
    return CSP_NO_FRAMING;
  }
  if (!domainRaw) {
    return CSP_NO_FRAMING;
  }

  let host = domainRaw.trim().toLowerCase();
  let port = '';
  if (host.includes(':')) {
    const cut = host.lastIndexOf(':');
    const tail = host.slice(cut + 1);
    // Anything that is not `host:digits` (a scheme, an IPv6 literal) is not a port at
    // all and must fail as a malformed hostname.
    if (!/^[0-9]{1,5}$/.test(tail)) {
      return CSP_NO_FRAMING;
    }
    const value = Number(tail);
    if (value < 1 || value > 65535) {
      return CSP_NO_FRAMING;
    }
    host = host.slice(0, cut);
    port = `:${tail}`;
  }
  if (!HOST_RE.test(host)) {
    return CSP_NO_FRAMING;
  }

  // PROTOCOL=0 is a legitimate on-premise configuration (§4.11), so http is allowed
  // here even though our own origin is always https.
  const scheme = protocolRaw === '0' ? 'http' : 'https';
  return `frame-ancestors 'self' ${scheme}://${host}${port}`;
}

export function middleware(request: NextRequest): NextResponse {
  const url = request.nextUrl;

  // Forward the validated LANG to the next-intl request config (§8).
  const requestHeaders = new Headers(request.headers);
  requestHeaders.delete(LANG_HEADER); // never trust an inbound copy
  const lang = param(url, 'LANG');
  if (lang && LANG_RE.test(lang.toLowerCase())) {
    requestHeaders.set(LANG_HEADER, lang.toLowerCase());
  }

  const response = NextResponse.next({ request: { headers: requestHeaders } });

  response.headers.set(
    'Content-Security-Policy',
    frameAncestors(param(url, 'DOMAIN'), param(url, 'PROTOCOL')),
  );
  // §4.10: the framing control is the CSP directive above and nothing else.
  response.headers.delete('X-Frame-Options');
  // The CSP varies with the query string, and the page is rendered per portal and per
  // user; a shared cache holding one portal's copy would frame-block another (§4.10).
  response.headers.set('Cache-Control', 'no-store');
  // The query string carries APP_SID and DOMAIN; nothing may leak them onwards (§4.6).
  response.headers.set('Referrer-Policy', 'no-referrer');
  response.headers.set('X-Content-Type-Options', 'nosniff');

  return response;
}

export const config = {
  // Every document response, including `/` and `/state/<kind>`. Static assets are
  // excluded: they are not framed documents and each hop through middleware costs.
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
