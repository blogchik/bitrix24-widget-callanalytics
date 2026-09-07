'use client';

/**
 * The bearer session, exactly as §4.6 defines it.
 *
 * The rules this module exists to enforce, all of them security rules:
 *
 *  * **The JWT arrives only in the URL fragment.** `handoff.html` writes
 *    `#s=<jwt>` (§4.4 step 8) because a fragment is never sent to a server, never
 *    lands in an access log and never appears in a `Referer`. We read it **once** and
 *    `history.replaceState` it away immediately, so a screenshot, a copied URL or a
 *    back navigation cannot leak it.
 *  * **It is never a query parameter and never a cookie** (ADR 0001-no-cookies). It
 *    lives in a module variable, with a `sessionStorage` copy so a reload inside the
 *    slider does not lose the session. `sessionStorage` is per tab and, in a framed
 *    third-party context, may be blocked outright - every access is therefore guarded
 *    and the app still works without it.
 *  * **Renewal goes through Bitrix24, never through us.** On expiry the SPA hands the
 *    backend a *fresh* `BX24.getAuth()` token plus the old JWT; the backend re-runs the
 *    open-time batch, re-decides access and mints a new JWT (§4.6). Access is therefore
 *    re-evaluated at least hourly.
 *
 * `exchange()` deliberately uses a bare `fetch` rather than `lib/api.ts`: the wrapper
 * calls back into this module on 401, and a cycle between the two would be a retry loop.
 */
import { getAuth, refreshAuth } from './bx24';

/** Fragment key written by `handoff.html`. */
const FRAGMENT_KEY = 's';

/** Per-tab copy so a reload inside the slider keeps the session. */
const STORAGE_KEY = 'ca.session.jwt';

/** §4.6. Its request body is excluded from all exception logging on the backend. */
const EXCHANGE_PATH = '/api/v1/session/exchange';

const EXCHANGE_TIMEOUT_MS = 20_000;

/** In-memory copy: the authoritative one. */
let token: string | null = null;

/** §4.6: the fragment is read exactly once per document. */
let fragmentRead = false;

function isBrowser(): boolean {
  return typeof window !== 'undefined';
}

function readStorage(): string | null {
  try {
    return window.sessionStorage.getItem(STORAGE_KEY);
  } catch {
    // Partitioned/blocked storage in a third-party frame. Memory only, then.
    return null;
  }
}

function writeStorage(value: string): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, value);
  } catch {
    /* see readStorage */
  }
}

function removeStorage(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* see readStorage */
  }
}

/**
 * Read `#s=<jwt>` once and erase it from the address bar.
 *
 * Safe to call repeatedly; only the first call touches the fragment. Returns the token
 * in force afterwards (the fragment's, or the `sessionStorage` copy on a reload).
 */
export function captureToken(): string | null {
  if (fragmentRead || !isBrowser()) {
    return token;
  }
  fragmentRead = true;

  const hash = window.location.hash;
  if (hash.length > 1) {
    let value: string | null = null;
    try {
      value = new URLSearchParams(hash.slice(1)).get(FRAGMENT_KEY);
    } catch {
      value = null;
    }
    if (value) {
      setToken(value);
      // Replace, not push: the token must not survive in the history entry either.
      // `search` is kept - DOMAIN/PROTOCOL/LANG/APP_SID are what the SDK and the CSP
      // need (§4.4 step 8, §4.10).
      try {
        window.history.replaceState(
          window.history.state,
          '',
          window.location.pathname + window.location.search,
        );
      } catch {
        // Older/blocked history API: fall back to clearing the hash value only.
        window.location.hash = '';
      }
    }
  }

  if (token === null) {
    token = readStorage();
  }
  return token;
}

/** The current session JWT, capturing it from the fragment on first use. */
export function getToken(): string | null {
  if (!fragmentRead) {
    return captureToken();
  }
  if (token === null && isBrowser()) {
    token = readStorage();
  }
  return token;
}

/** Install a token (from the fragment or from an exchange). */
export function setToken(value: string): void {
  token = value;
  if (isBrowser()) {
    writeStorage(value);
  }
}

/** Forget the session everywhere. Called when the backend rejects it for good. */
export function clearToken(): void {
  token = null;
  if (isBrowser()) {
    removeStorage();
  }
}

interface ExchangeResponse {
  /** The field name the backend uses; both spellings are accepted defensively. */
  token?: unknown;
  jwt?: unknown;
}

/** Concurrent 401s must produce one exchange, not one per in-flight request. */
let inFlight: Promise<string | null> | null = null;

async function runExchange(): Promise<string | null> {
  if (!isBrowser()) {
    return null;
  }

  // A *fresh* user token is the whole point: the backend re-runs the open-time batch
  // with it, so a stale one would only re-prove a stale identity (§4.6).
  const auth = (await getAuth()) ?? (await refreshAuth());
  if (!auth) {
    // No SDK, no parent frame, or Bitrix24 refused: the session cannot be renewed.
    return null;
  }

  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), EXCHANGE_TIMEOUT_MS);
  try {
    const response = await fetch(EXCHANGE_PATH, {
      method: 'POST',
      // No `Authorization` header: the JWT being exchanged is expired, and §4.6 puts it
      // in the body precisely so the endpoint can verify it while ignoring `exp`.
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ access_token: auth.access_token, jwt: token }),
      credentials: 'omit', // ADR 0001-no-cookies
      cache: 'no-store',
      signal: controller.signal,
    });

    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        // The portal or the user is no longer entitled; a retry would loop.
        clearToken();
      }
      return null;
    }

    const data = (await response.json()) as ExchangeResponse;
    const next = typeof data.token === 'string' ? data.token : data.jwt;
    if (typeof next !== 'string' || next.length === 0) {
      return null;
    }
    setToken(next);
    return next;
  } catch {
    // Network failure or timeout: the caller surfaces the original error instead.
    return null;
  } finally {
    window.clearTimeout(timer);
  }
}

/**
 * Renew the session with a fresh Bitrix24 token (§4.6).
 *
 * Returns the new JWT, or `null` when the session cannot be renewed - the caller then
 * renders a state page rather than retrying.
 */
export function exchange(): Promise<string | null> {
  if (inFlight) {
    return inFlight;
  }
  inFlight = runExchange().finally(() => {
    inFlight = null;
  });
  return inFlight;
}
