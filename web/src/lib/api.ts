'use client';

/**
 * The only way the SPA talks to `/api/v1/*` (§4.6, §4.7, §4.8).
 *
 *  * **`Authorization: Bearer <jwt>`, always; a URL, never.** The token is not a query
 *    parameter, not a cookie and not a header we log - Caddy's access log even strips
 *    query strings so that a mistake here could not persist (§4.10).
 *  * **Retry exactly once, through the session exchange.** A 401 means the hour-long
 *    JWT expired inside a long-lived slider; §4.6 renews it with a fresh
 *    `BX24.getAuth()` token and the backend re-decides access while doing so. A 409
 *    `context_missing` (§4.8) is the same move for a different reason: the cached CRM
 *    context is older than the session, and the exchange re-resolves it with the
 *    current user's token. One retry, never a loop.
 *  * **Machine codes in, translated sentences out.** §8: the backend answers
 *    `{"code": "no_stats_permission"}` and the SPA decides which of the §4.11 states
 *    that is. No API error string is ever shown to a user.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { isStateKind, type StateKind } from '@/components/StateCard';

import { clearToken, exchange, getToken } from './session';

export const API_BASE = '/api/v1';

const REQUEST_TIMEOUT_MS = 30_000;

/** Machine codes this module invents for failures that never reach the backend. */
export const CODE_NETWORK = 'network';
export const CODE_TIMEOUT = 'timeout';
export const CODE_SERVER = 'server';
export const CODE_INVALID_SESSION = 'invalid_session';
export const CODE_UNKNOWN = 'unknown';

/** A failed `/api/v1/*` call, carrying the backend's machine code (§8). */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  /**
   * The rest of the error body, when the server sent one.
   *
   * Several §8 codes carry numbers the user needs in order to act: `period_too_long` names
   * its limit, and `deal_scan_too_large` names the deal count, the cap and the period
   * length — "too large" without them is a refusal nobody can work with. The body was
   * already parsed to read `code`; keeping the rest costs nothing and was previously
   * discarded, which quietly made those messages unwritable.
   *
   * It is portal-shaped data from our own API, never rendered raw: a page reads named
   * fields out of it and passes them to ICU placeholders.
   */
  readonly details?: Readonly<Record<string, unknown>>;

  constructor(
    code: string,
    status: number,
    message?: string,
    details?: Readonly<Record<string, unknown>>,
  ) {
    super(message ?? code);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

/** The error body minus its `code`, or undefined when there was nothing else in it. */
function detailsFromBody(body: unknown): Readonly<Record<string, unknown>> | undefined {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    return undefined;
  }
  const rest: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(body as Record<string, unknown>)) {
    if (key !== 'code') {
      rest[key] = value;
    }
  }
  return Object.keys(rest).length > 0 ? rest : undefined;
}

/**
 * Machine code -> §4.11 state, for codes that are not already a state kind.
 *
 * `api/app/api/session.py` answers a §4.4 step-5 decision with the state kind *as* the
 * code (`retry`, `scope`, `method_missing`, `crm_no_access`), so most of the mapping is
 * the identity and lives in {@link presentError}; this table is only for the codes that
 * read differently on the wire than on the page. Everything mapped here renders with
 * the *mandated* copy of that state, so the sentence a user sees after a failed fetch
 * is the one the server-rendered page would have shown. Codes with no state of their
 * own get `errors.<code>` (below).
 */
const STATE_FOR_CODE: Readonly<Record<string, StateKind>> = {
  no_stats_permission: 'denied',
  portal_inactive: 'not_installed',
  insufficient_scope: 'scope',
  reauth_required: 'reauth',
  rate_limited: 'retry',
  query_limit_exceeded: 'retry',
  operation_time_limit: 'retry',
};

/** Codes that have their own sentence in the catalogue under `errors.<code>`. */
const OWN_MESSAGE_CODES: ReadonlySet<string> = new Set([
  CODE_INVALID_SESSION,
  CODE_NETWORK,
  CODE_TIMEOUT,
  CODE_SERVER,
  'context_missing',
  // §4.12. All four are about THIS request rather than about the session or the portal, so
  // none of them has a §4.11 state to borrow: the reader has to narrow the period, sign in
  // to Bitrix24 again, or drop a parameter, and only a sentence can say which.
  'deal_scan_too_large',
  'deal_report_failed',
  'viewer_token_required',
  'unsupported_filter',
]);

export interface ErrorPresentation {
  /**
   * Values for the ICU placeholders in `bodyKey`, taken from the error body (§8).
   *
   * Present only for the `errors.<code>` sentences that have placeholders at all —
   * `deal_scan_too_large` names the deal count, the cap and the period length, and a
   * refusal without those numbers is one nobody can act on.
   */
  bodyValues?: Record<string, string | number>;
  /** Which §4.11 state to render. */
  kind: StateKind;
  /** Catalogue key for the heading. */
  titleKey: string;
  /** Catalogue key for the body. */
  bodyKey: string;
  /** Machine code, for the console and for tests - never rendered. */
  code: string;
}

/** Turn any thrown value into "which state page, in which words" (§4.11). */
export function presentError(error: unknown): ErrorPresentation {
  const code = error instanceof ApiError ? error.code : CODE_UNKNOWN;
  const kind = isStateKind(code) ? code : STATE_FOR_CODE[code];
  if (kind) {
    return { kind, titleKey: `state.${kind}.title`, bodyKey: `state.${kind}.body`, code };
  }
  if (OWN_MESSAGE_CODES.has(code)) {
    return {
      kind: 'error',
      titleKey: 'state.error.title',
      bodyKey: `errors.${code}`,
      bodyValues: messageValues(error),
      code,
    };
  }
  return { kind: 'error', titleKey: 'state.error.title', bodyKey: 'state.error.body', code };
}

/**
 * The scalar fields of an error body, for the ICU placeholders of `errors.<code>`.
 *
 * Only strings and finite numbers cross: a placeholder is rendered into a sentence, and an
 * object or an array there would either throw inside the formatter or print `[object
 * Object]` at the reader. Everything else is dropped, and {@link safeTranslate} covers the
 * case where that leaves a placeholder unfilled.
 */
function messageValues(error: unknown): Record<string, string | number> | undefined {
  if (!(error instanceof ApiError) || !error.details) {
    return undefined;
  }
  const values: Record<string, string | number> = {};
  for (const [key, value] of Object.entries(error.details)) {
    if (typeof value === 'string' || (typeof value === 'number' && Number.isFinite(value))) {
      values[key] = value;
    }
  }
  return Object.keys(values).length > 0 ? values : undefined;
}

/** Pull the machine code out of whatever error envelope the backend used. */
function codeFromBody(body: unknown, status: number): string {
  if (body && typeof body === 'object') {
    const record = body as Record<string, unknown>;
    if (typeof record.code === 'string' && record.code) {
      return record.code;
    }
    const detail = record.detail;
    if (typeof detail === 'string' && detail) {
      return detail;
    }
    if (detail && typeof detail === 'object') {
      const nested = (detail as Record<string, unknown>).code;
      if (typeof nested === 'string' && nested) {
        return nested;
      }
    }
  }
  if (status === 401) {
    return CODE_INVALID_SESSION;
  }
  return status >= 500 ? CODE_SERVER : CODE_UNKNOWN;
}

async function parseBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

interface RequestOptions {
  method?: string;
  /** Serialised as JSON. Never contains the JWT: that travels in the header only. */
  body?: unknown;
  signal?: AbortSignal;
}

async function send(path: string, token: string, options: RequestOptions): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  const external = options.signal;
  const onAbort = () => controller.abort();
  external?.addEventListener('abort', onAbort);

  const headers: Record<string, string> = { authorization: `Bearer ${token}` };
  if (options.body !== undefined) {
    headers['content-type'] = 'application/json';
  }

  try {
    return await fetch(`${API_BASE}${path}`, {
      method: options.method ?? 'GET',
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: 'omit', // ADR 0001-no-cookies
      cache: 'no-store',
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
    external?.removeEventListener('abort', onAbort);
  }
}

/**
 * One authenticated call to the JSON API.
 *
 * Throws {@link ApiError}; pass the result to {@link presentError} to decide what the
 * user sees.
 */
export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let token = getToken();
  if (!token) {
    // A reload that lost the fragment and the sessionStorage copy: the SPA can still
    // rebuild a session from the SDK, which is the same move as an expiry (§4.6).
    token = await exchange();
    if (!token) {
      throw new ApiError(CODE_INVALID_SESSION, 401);
    }
  }

  let response: Response;
  try {
    response = await send(path, token, options);
  } catch (error) {
    const aborted = error instanceof DOMException && error.name === 'AbortError';
    throw new ApiError(aborted ? CODE_TIMEOUT : CODE_NETWORK, 0);
  }

  if (response.ok) {
    return (await parseBody(response)) as T;
  }

  let body = await parseBody(response);
  let code = codeFromBody(body, response.status);

  // §4.6 expiry and §4.8 stale CRM context are both cured by one session exchange.
  const renewable =
    response.status === 401 || (response.status === 409 && code === 'context_missing');
  if (renewable) {
    const renewed = await exchange();
    if (!renewed) {
      clearToken();
      throw new ApiError(
        response.status === 401 ? CODE_INVALID_SESSION : code,
        response.status,
        undefined,
        detailsFromBody(body),
      );
    }
    try {
      response = await send(path, renewed, options);
    } catch (error) {
      const aborted = error instanceof DOMException && error.name === 'AbortError';
      throw new ApiError(aborted ? CODE_TIMEOUT : CODE_NETWORK, 0);
    }
    if (response.ok) {
      return (await parseBody(response)) as T;
    }
    body = await parseBody(response);
    code = codeFromBody(body, response.status);
  }

  throw new ApiError(code, response.status, undefined, detailsFromBody(body));
}

// --- GET /me ------------------------------------------------------------------------

/** §4.7: Bitrix24's four permission levels collapsed to three. */
export type AccessLevel = 'all' | 'own' | 'denied';

/** The `ent` claim of §4.6, as the API hands it back. */
export interface MeEntity {
  t: string;
  id: number;
}

/**
 * The control-plane sync summary `GET /me` carries so the dashboard needs no second
 * round trip (§4.11's "importing history" banner, §5.8's stalled-token banner).
 */
export interface MeSync {
  token_status?: string | null;
  backfill_status?: string | null;
  backfill_done?: number | null;
  backfill_total?: number | null;
  /** True while history is still coming: an empty period is "not yet", not "none". */
  importing?: boolean | null;
  last_incremental_at?: string | null;
  /** Admin-only: support vocabulary a regular employee cannot act on. */
  last_error_code?: string | null;
}

/**
 * `GET /api/v1/me` - the session as the backend sees it (`api/app/api/session.py`).
 *
 * It is the principal, flat: §4.6's claims plus the sync summary. There is no employee
 * name here and no portal record - `GET /me` deliberately touches no call data and no
 * customer data, so the placeholder views identify the viewer by id.
 */
export interface Me {
  user_id: number;
  is_admin: boolean;
  access: AccessLevel;
  /** Already resolved through the shared fallback map (§8). */
  locale?: string | null;
  timezone?: string | null;
  placement?: string | null;
  entity?: MeEntity | null;
  issued_at?: number | null;
  /**
   * Present only for `acc='denied'`: the machine code plus the catalogue key of the
   * mandated sentence, so the server picks the copy and the SPA only renders it (§8).
   */
  no_access?: { code: string; copy_key: string } | null;
  sync?: MeSync | null;
}

/**
 * Which catalogue key carries the "no access to call statistics" sentence.
 *
 * §4.7's text is mandated, and the server names the key it wants rendered
 * (`no_access.copy_key`). Only a `state.*` key is honoured, so a future backend change
 * cannot make this render an arbitrary message.
 */
export function deniedBodyKey(me: Me): string {
  const key = me.no_access?.copy_key;
  return typeof key === 'string' && key.startsWith('state.') ? key : 'state.denied.body';
}

export function fetchMe(signal?: AbortSignal): Promise<Me> {
  return apiFetch<Me>('/me', { signal });
}

export interface Resource<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/**
 * `GET /me` as a hook, with the loading/error states the placeholder pages render.
 *
 * Milestone 5 replaces the page bodies, not this: the plumbing (bearer, retry-once,
 * translated failure) is what the real views build on.
 */
export function useMe(): Resource<Me> {
  const [data, setData] = useState<Me | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchMe(controller.signal)
      .then((value) => {
        if (mounted.current) {
          setData(value);
        }
      })
      .catch((cause: unknown) => {
        if (mounted.current && !controller.signal.aborted) {
          setError(cause);
        }
      })
      .finally(() => {
        if (mounted.current) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [attempt]);

  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  return { data, error, loading, reload };
}
