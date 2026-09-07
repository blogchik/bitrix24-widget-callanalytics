'use client';

/**
 * The BX24 JS SDK, wrapped so that nothing in the app can throw because of it (§4.10).
 *
 * Three facts drive the shape of this module:
 *
 *  1. **The SDK is only usable after `BX24.init()` has fired.** Everything else here
 *     awaits {@link ready}.
 *  2. **`BX24.init` never fires without `APP_SID` in the query string** (§4.4 step 8:
 *     that is exactly why `handoff.html` forwards Bitrix24's original query verbatim).
 *     A missing `APP_SID` is therefore not a transient failure but a routing bug, and
 *     it has to be loud in the console instead of hanging forever on a promise.
 *  3. **The app also renders outside Bitrix24** - the `/` route, a moderator pasting
 *     the URL, a preview. Every wrapper below degrades to a no-op there rather than
 *     throwing, because §4.11 makes a broken frame a moderation rejection.
 *
 * The SDK script is injected at most once per document.
 */

/** What `BX24.getAuth()` returns. The `access_token` here is the *user's*, not ours. */
export interface Bx24Auth {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  domain: string;
  member_id: string;
}

/** What `BX24.placement.info()` returns; `options` is the parsed `PLACEMENT_OPTIONS`. */
export interface Bx24PlacementInfo {
  placement: string;
  options: Record<string, unknown>;
}

interface Bx24Api {
  init(callback: () => void): void;
  fitWindow(callback?: () => void): void;
  resizeWindow(width: number, height: number, callback?: () => void): void;
  getAuth(): Bx24Auth | false;
  refreshAuth(callback: (auth: Bx24Auth | false) => void): void;
  openPath(path: string, callback?: (result: unknown) => void): void;
  placement: { info(): Bx24PlacementInfo };
}

declare global {
  interface Window {
    BX24?: Bx24Api;
  }
}

/** Same URL the server-rendered `install.html` loads; protocol-relative, as documented. */
const SDK_SRC = '//api.bitrix24.com/api/v1/';
const SDK_ELEMENT_ID = 'bx24-sdk';

/** Generous: the SDK is a third-party script and the portal may be slow. */
const INIT_TIMEOUT_MS = 10_000;
const CALLBACK_TIMEOUT_MS = 10_000;

/** Raised by {@link ready}; every wrapper in this module catches it. */
export class Bx24UnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'Bx24UnavailableError';
  }
}

function isBrowser(): boolean {
  return typeof window !== 'undefined' && typeof document !== 'undefined';
}

/**
 * Is `APP_SID` present in the query string?
 *
 * Case-insensitive, like every other Bitrix24 parameter read in this app.
 */
export function hasAppSid(): boolean {
  if (!isBrowser()) {
    return false;
  }
  const params = new URLSearchParams(window.location.search);
  for (const key of params.keys()) {
    if (key.toLowerCase() === 'app_sid') {
      return true;
    }
  }
  return false;
}

/** True when the document is embedded in another window at all. */
export function isFramed(): boolean {
  if (!isBrowser()) {
    return false;
  }
  try {
    return window.self !== window.top;
  } catch {
    // A cross-origin parent throws on access - which is itself proof of being framed.
    return true;
  }
}

/**
 * Should we even try to talk to Bitrix24?
 *
 * `false` on the `/` route opened directly, where injecting a third-party script would
 * buy nothing and the page's whole job is to say "open this app from Bitrix24".
 */
export function isAvailable(): boolean {
  return isBrowser() && (hasAppSid() || isFramed());
}

let sdkPromise: Promise<void> | null = null;

function loadSdk(): Promise<void> {
  if (sdkPromise) {
    return sdkPromise;
  }
  sdkPromise = new Promise<void>((resolve, reject) => {
    if (window.BX24) {
      resolve();
      return;
    }
    const existing = document.getElementById(SDK_ELEMENT_ID);
    const script =
      existing instanceof HTMLScriptElement ? existing : document.createElement('script');
    script.addEventListener('load', () => resolve());
    script.addEventListener('error', () =>
      reject(new Bx24UnavailableError('BX24: could not load ' + SDK_SRC)),
    );
    if (!existing) {
      script.id = SDK_ELEMENT_ID;
      script.src = SDK_SRC;
      script.async = true;
      document.head.appendChild(script);
    }
  });
  return sdkPromise;
}

let readyPromise: Promise<void> | null = null;

/**
 * Resolves once `BX24.init()` has called back; rejects when the SDK is unusable.
 *
 * The rejection is cached and pre-handled, so calling `ready()` from several places
 * cannot produce an unhandled rejection. Callers inside this module use `withApi()`;
 * callers outside it should `await ready().catch(...)`.
 */
export function ready(): Promise<void> {
  if (readyPromise) {
    return readyPromise;
  }
  readyPromise = (async () => {
    if (!isBrowser()) {
      throw new Bx24UnavailableError('BX24: not a browser environment');
    }
    if (!isAvailable()) {
      throw new Bx24UnavailableError(
        'BX24: the page is not embedded in Bitrix24 (no APP_SID and no parent frame); ' +
          'the SDK was not loaded.',
      );
    }
    if (!hasAppSid()) {
      // Framed but without APP_SID: init() will silently never fire (§4.4 step 8).
      console.error(
        'BX24: APP_SID is missing from the query string, so BX24.init() can never talk ' +
          'to the parent frame. The handoff must forward the original Bitrix24 query ' +
          'string verbatim (architecture.md §4.4 step 8).',
      );
    }
    await loadSdk();
    const api = window.BX24;
    if (!api) {
      throw new Bx24UnavailableError('BX24: the SDK loaded but window.BX24 is undefined');
    }
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        const message =
          'BX24.init() did not fire within ' +
          INIT_TIMEOUT_MS +
          ' ms. Without APP_SID in the query string the SDK never reaches the parent ' +
          'frame, so fitWindow/getAuth/openPath stay inert (architecture.md §4.4 step 8).';
        console.error('BX24: ' + message);
        reject(new Bx24UnavailableError(message));
      }, INIT_TIMEOUT_MS);
      try {
        api.init(() => {
          window.clearTimeout(timer);
          resolve();
        });
      } catch (error) {
        window.clearTimeout(timer);
        const detail = error instanceof Error ? error.message : String(error);
        reject(new Bx24UnavailableError('BX24.init() threw: ' + detail));
      }
    });
  })();
  // Mark the cached promise as handled; every consumer below has its own catch.
  void readyPromise.catch(() => undefined);
  return readyPromise;
}

/** The initialised SDK, or `null` when it is unusable. Never throws. */
async function withApi(): Promise<Bx24Api | null> {
  try {
    await ready();
  } catch {
    return null;
  }
  return window.BX24 ?? null;
}

/** Resize the slider to the rendered content (§4.10). No-op outside Bitrix24. */
export async function fitWindow(): Promise<void> {
  const api = await withApi();
  try {
    api?.fitWindow();
  } catch {
    // The parent frame can disappear mid-flight (slider closed); never surface it.
  }
}

/** Explicit size, for the rare case `fitWindow()` cannot measure (§4.10). */
export async function resizeWindow(width: number, height: number): Promise<void> {
  const api = await withApi();
  try {
    api?.resizeWindow(Math.max(0, Math.round(width)), Math.max(0, Math.round(height)));
  } catch {
    /* see fitWindow */
  }
}

/**
 * The current user's Bitrix24 token pair, or `null`.
 *
 * Used only to prove the viewer to our own backend (§4.6 session exchange); the app
 * never calls Bitrix24 REST from the browser.
 */
export async function getAuth(): Promise<Bx24Auth | null> {
  const api = await withApi();
  if (!api) {
    return null;
  }
  try {
    const auth = api.getAuth();
    return auth === false ? null : auth;
  } catch {
    return null;
  }
}

/** Ask Bitrix24 for a fresh user token, then read it back. `null` when unavailable. */
export async function refreshAuth(): Promise<Bx24Auth | null> {
  const api = await withApi();
  if (!api) {
    return null;
  }
  try {
    return await new Promise<Bx24Auth | null>((resolve) => {
      const timer = window.setTimeout(() => resolve(null), CALLBACK_TIMEOUT_MS);
      api.refreshAuth((auth) => {
        window.clearTimeout(timer);
        resolve(auth === false ? null : auth);
      });
    });
  } catch {
    return null;
  }
}

/**
 * Navigate the parent Bitrix24 window, e.g. `/crm/deal/details/123/` (§4.10).
 *
 * Returns `false` when the SDK is unavailable, so the caller can fall back to plain
 * text instead of rendering a link that does nothing.
 */
export async function openPath(path: string): Promise<boolean> {
  const api = await withApi();
  if (!api) {
    return false;
  }
  try {
    await new Promise<void>((resolve) => {
      const timer = window.setTimeout(() => resolve(), CALLBACK_TIMEOUT_MS);
      api.openPath(path, () => {
        window.clearTimeout(timer);
        resolve();
      });
    });
    return true;
  } catch {
    return false;
  }
}

/**
 * Where Bitrix24 thinks it embedded us.
 *
 * The backend already routed on the POSTed `PLACEMENT` and put it in the JWT; this is
 * the browser-side cross-check §10 step 3 asks to verify on every placement.
 */
export async function placementInfo(): Promise<Bx24PlacementInfo | null> {
  const api = await withApi();
  if (!api) {
    return null;
  }
  try {
    return api.placement.info();
  } catch {
    return null;
  }
}
