'use client';

/**
 * The typed client for the call endpoints of `/api/v1` (§4.5-§4.8, §5.7, §9).
 *
 * Everything here goes through `lib/api.ts`, which owns the bearer header and the
 * retry-once-through-the-session-exchange rule; this module only knows the *shapes*, and
 * they are the shapes `api/app/api/calls.py` actually answers with:
 *
 *  * **`GET /calls`** - 50 rows a page, `call_start_date DESC` with `bx_id DESC` as the
 *    tiebreaker, paged by a 1-based `page` number. The filter half of the query string is
 *    parsed by the *same* `services/stats.py::parse_filters` the dashboard uses, which is
 *    what stops the charts and the rows under them from describing different periods.
 *  * **`POST /calls/{id}/play-url`** - the five-minute, one-call playback grant of §4.6.
 *    An `<audio src>` cannot carry an `Authorization` header, so the grant travels as
 *    `?t=` on `GET /calls/{id}/record`. For a non-admin viewer the endpoint additionally
 *    wants the viewer's own Bitrix24 token in the body (§9 step 3: Bitrix24's separate
 *    "listen to recordings" right is enforced by Bitrix24 itself, by fetching the
 *    recording as the viewer); it says so with `viewer_token_required` and the SPA posts
 *    again with `BX24.getAuth()`.
 *  * **`POST /calls/{id}/refresh`** - §5.7 rule 3: playback answered 403/404, so the
 *    cached row is stale and the next sync visit re-reads it. Best effort, rate-limited
 *    per portal on the server.
 *
 * Nothing about a recording's real location ever reaches this module: §3 decision 21 and
 * §9 keep `call_record_url` server-side, and the row projection below has no field for it.
 */
import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, apiFetch } from './api';
import { getAuth, refreshAuth } from './bx24';
import { crmEntityPath, directionKey, resultKey } from './format';
import type { ResultGroup } from './viz';

// ---------------------------------------------------------------------------------
// Row and page shapes (mirroring `api/app/api/calls.py`)
// ---------------------------------------------------------------------------------

/** The `employees` cache join (§7). `name` is "Last First Second", or `null`. */
export interface CallEmployee {
  id: number;
  name?: string | null;
  /** §7: dismissed users still own historical calls; the table badges them. */
  active?: boolean | null;
  found?: boolean | null;
}

/** The parts of a `BX24.openPath('/crm/<type>/details/<id>/')` link (§4.10). */
export interface CallCrm {
  type?: string | null;
  id?: number | null;
  activity_id?: number | null;
}

/** The telephony line: `rest_app_id` `null` is built-in telephony (§3). */
export interface CallLine {
  rest_app_id?: number | null;
  name?: string | null;
}

/** One row of `GET /calls`. Every field optional but `id` and the start date. */
export interface CallRow {
  /** `calls.id`, the surrogate key: the id `play-url` and `refresh` take. */
  id: number;
  /** ISO-8601. Rendered in the viewer's timezone (JWT `tz`), never the browser's. */
  call_start_date: string;
  employee_id?: number | null;
  employee?: CallEmployee | null;
  /** Raw `CALL_TYPE` as delivered (§3); an unknown code renders as "unknown". */
  call_type?: number | string | null;
  phone_number?: string | null;
  portal_number?: string | null;
  crm?: CallCrm | null;
  duration?: number | null;
  /** §3's generated column: `answered` | `missed` | `not_connected`. */
  result_group?: string | null;
  /** The raw `CALL_FAILED_CODE`: `200`, `486`, `603-S`, `OTHER`, or something new. */
  failed_code?: string | null;
  line?: CallLine | null;
  has_record?: boolean | null;
  record_duration?: number | null;
}

/** The period the server actually aggregated, echoed back so the page can label itself. */
export interface PeriodEcho {
  preset: string | null;
  from: string | null;
  to: string | null;
  days: number | null;
  timezone: string | null;
}

/** One page of {@link CallRow}, normalised by {@link normalisePage}. */
export interface CallsPage {
  rows: CallRow[];
  page: number;
  hasMore: boolean;
  /** §9's `RECORDING_MODE`: `off` until the spike is answered, then `proxy`. */
  recordingMode: string | null;
  period: PeriodEcho | null;
}

/** A repeatable filter parameter: one value, several, or none. */
export type FilterValue = string | number | readonly (string | number)[] | null | undefined;

/**
 * The query string `GET /calls` understands (`services/stats.py::parse_filters`).
 *
 * The field names are the parameter names, and they are deliberately the same names
 * `components/Filters.tsx` already keeps in `DashboardFilters`, so the dashboard can pass
 * its filter state through with a spread instead of a translation layer that could drift.
 *
 * One rule is easy to get wrong and is handled for the caller in {@link callsSearchParams}:
 * `from`/`to` are read by the server **only** under `period=custom`. A query that sends
 * dates without saying `custom` is silently answered for the default preset - seven days -
 * which looks like missing data rather than like a mistake.
 */
export interface CallsQuery {
  /** `today` | `7d` | `30d` | `custom`. Defaults to `custom` when dates are present. */
  period?: string | null;
  /** Inclusive local day, `YYYY-MM-DD`, in the viewer's timezone. */
  from?: string | null;
  /** Inclusive local day, `YYYY-MM-DD`, in the viewer's timezone. */
  to?: string | null;
  /** `portal_user_id`. */
  employee?: FilterValue;
  /** Raw `call_type`. */
  direction?: FilterValue;
  /** A `result_group` value. */
  result?: FilterValue;
  /** `rest_app_id`, or the literal `builtin`. */
  line?: FilterValue;
  /**
   * A substring of the counterparty number.
   *
   * Matched on digits alone at both ends (`api/app/api/calls.py::_table_facets`), so what
   * the table renders - regrouped with spaces by {@link formatPhone} - can be pasted back
   * in and still match the row it came from.
   */
  search?: string | null;
}

/** §2: "GET /calls (50/page)". Fixed server-side; the client only counts with it. */
export const PAGE_SIZE = 50;

/** `config.MAX_PERIOD_DAYS` (§10 step 5): a custom period is capped at 366 days. */
export const MAX_PERIOD_DAYS = 366;

/** The literal the `line` filter uses for built-in telephony (`rest_app_id IS NULL`). */
export const BUILTIN_LINE = 'builtin';

// ---------------------------------------------------------------------------------
// Periods
// ---------------------------------------------------------------------------------

/** The period presets the CRM tab offers. Longer than the dashboard's on purpose. */
export type PeriodId = '30d' | '90d' | '12m';

const PERIOD_DAYS: Readonly<Record<PeriodId, number>> = {
  '30d': 30,
  '90d': 90,
  '12m': MAX_PERIOD_DAYS,
};

/** `YYYY-MM-DD` for "today" as the *viewer's* portal sees it (JWT `tz`, §4.6). */
export function todayInZone(timeZone?: string | null): string {
  const now = new Date();
  try {
    // `en-CA` is ISO-shaped by definition, which is why it is the formatter of choice
    // here rather than a hand-rolled offset calculation.
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: timeZone ?? undefined,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(now);
  } catch {
    // A misconfigured portal timezone must not blank the page (§4.11).
    return now.toISOString().slice(0, 10);
  }
}

function shiftDays(day: string, delta: number): string {
  const parsed = Date.parse(`${day}T00:00:00Z`);
  if (Number.isNaN(parsed)) {
    return day;
  }
  return new Date(parsed + delta * 86_400_000).toISOString().slice(0, 10);
}

/**
 * A preset as the inclusive `[from, to]` day pair the API takes.
 *
 * The offset is `days - 1` because both ends are inclusive: "30 days" is today plus the
 * twenty-nine before it. `12m` is exactly `MAX_PERIOD_DAYS`, so it is the longest period
 * the server will accept rather than one day past it (`period_too_long`).
 */
export function periodRange(
  period: PeriodId,
  timeZone?: string | null,
): { from: string; to: string } {
  const to = todayInZone(timeZone);
  const days = Math.min(PERIOD_DAYS[period] ?? PERIOD_DAYS['30d'], MAX_PERIOD_DAYS);
  return { from: shiftDays(to, -(days - 1)), to };
}

// ---------------------------------------------------------------------------------
// Fetching
// ---------------------------------------------------------------------------------

function pushAll(params: URLSearchParams, key: string, value: FilterValue): void {
  const values = Array.isArray(value) ? value : value === null || value === undefined ? [] : [value];
  for (const entry of values) {
    const text = String(entry).trim();
    if (text) {
      params.append(key, text);
    }
  }
}

/**
 * The query string for one page.
 *
 * `period=custom` is filled in whenever dates are present and no preset was named: the
 * server reads `from`/`to` under that preset only, and a caller that forgot it would get
 * a silently different period rather than an error.
 */
export function callsSearchParams(query: CallsQuery, page: number = 1): URLSearchParams {
  const params = new URLSearchParams();
  const hasDates = Boolean(query.from && query.to);
  const period = (query.period ?? (hasDates ? 'custom' : '')).trim();
  if (period) {
    params.set('period', period);
  }
  if (period === 'custom' && hasDates) {
    params.set('from', query.from as string);
    params.set('to', query.to as string);
  }
  pushAll(params, 'employee', query.employee);
  pushAll(params, 'direction', query.direction);
  pushAll(params, 'result', query.result);
  pushAll(params, 'line', query.line);
  // `search` of the three spellings the server accepts (`search` / `q` / `phone`): one
  // name on the wire is one string to find when a request has to be explained.
  const search = (query.search ?? '').trim();
  if (search) {
    params.set('search', search);
  }
  if (page > 1) {
    params.set('page', String(page));
  }
  return params;
}

/** A stable identity for a query, so effects re-run on content and not on identity. */
export function callsQueryKey(query: CallsQuery): string {
  return callsSearchParams(query).toString();
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function asRows(value: unknown): CallRow[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter((row): row is CallRow => {
    const record = asRecord(row);
    return record !== null && typeof record.id === 'number';
  });
}

function periodOf(value: unknown): PeriodEcho | null {
  const body = asRecord(value);
  if (!body) {
    return null;
  }
  return {
    preset: str(body.preset),
    from: str(body.from),
    to: str(body.to),
    days: num(body.days),
    timezone: str(body.timezone),
  };
}

/** The `GET /calls` envelope -> {@link CallsPage}, defensively. */
export function normalisePage(raw: unknown, requestedPage: number): CallsPage {
  const body = asRecord(raw) ?? {};
  const rows = asRows(body.rows ?? body.items ?? raw);
  return {
    rows,
    page: num(body.page) ?? requestedPage,
    // `has_more` rather than the `total` the server also sends: this table pages by
    // appending and never draws a numbered pager, so a count it would not render is a
    // count it does not need to carry through four types to get here.
    hasMore: typeof body.has_more === 'boolean' ? body.has_more : rows.length >= PAGE_SIZE,
    recordingMode: str(body.recording_mode),
    period: periodOf(body.period),
  };
}

/** One page of `GET /api/v1/calls`. */
export async function fetchCalls(
  query: CallsQuery,
  page: number = 1,
  signal?: AbortSignal,
): Promise<CallsPage> {
  const params = callsSearchParams(query, page);
  const suffix = params.toString();
  const raw = await apiFetch<unknown>(`/calls${suffix ? `?${suffix}` : ''}`, { signal });
  return normalisePage(raw, page);
}

/** Rows already on screen plus the new page, de-duplicated by id. */
function appendUnique(existing: readonly CallRow[], incoming: readonly CallRow[]): CallRow[] {
  const seen = new Set(existing.map((row) => row.id));
  const merged = existing.slice();
  for (const row of incoming) {
    if (!seen.has(row.id)) {
      seen.add(row.id);
      merged.push(row);
    }
  }
  return merged;
}

/** What {@link useCalls} hands the table. */
export interface CallsResource {
  items: CallRow[];
  error: unknown;
  /** The first page is in flight; the table renders a skeleton. */
  loading: boolean;
  /** A "load more" page is in flight; the rows on screen stay put. */
  loadingMore: boolean;
  hasMore: boolean;
  /** §9's mode, as the server reports it on every page. */
  recordingMode: string | null;
  period: PeriodEcho | null;
  loadMore: () => void;
  reload: () => void;
}

interface PageState {
  key: string;
  items: CallRow[];
  page: number;
  hasMore: boolean;
  recordingMode: string | null;
  period: PeriodEcho | null;
}

const EMPTY_STATE = (key: string): PageState => ({
  key,
  items: [],
  page: 0,
  hasMore: false,
  recordingMode: null,
  period: null,
});

/**
 * `GET /calls` as a paging hook.
 *
 * "Load more" **appends**. A table that replaced its rows would lose the reader's place
 * every time, and the expanded player with it. Changing the query (period, filter) starts
 * a new list instead: that is a different question, not more of the same answer.
 */
export function useCalls(query: CallsQuery, enabled: boolean = true): CallsResource {
  const key = callsQueryKey(query);

  const queryRef = useRef(query);
  queryRef.current = query;

  const [state, setState] = useState<PageState>(() => EMPTY_STATE(key));
  const stateRef = useRef(state);
  stateRef.current = state;

  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(enabled);
  const [loadingMore, setLoadingMore] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      setState(EMPTY_STATE(key));
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setState(EMPTY_STATE(key));
    setError(null);
    setLoading(true);
    fetchCalls(queryRef.current, 1, controller.signal)
      .then((page) => {
        if (!mounted.current || controller.signal.aborted) {
          return;
        }
        setState({
          key,
          items: page.rows,
          page: page.page,
          hasMore: page.hasMore,
          recordingMode: page.recordingMode,
          period: page.period,
        });
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
  }, [key, enabled, attempt]);

  const loadMore = useCallback(() => {
    const current = stateRef.current;
    if (!current.hasMore || loading || loadingMore) {
      return;
    }
    const next = current.page + 1;
    setLoadingMore(true);
    setError(null);
    fetchCalls(queryRef.current, next)
      .then((page) => {
        if (!mounted.current) {
          return;
        }
        setState((previous) => {
          // The query moved while the page was in flight: this answer is to a question
          // nobody is asking any more.
          if (previous.key !== current.key) {
            return previous;
          }
          return {
            key: previous.key,
            items: appendUnique(previous.items, page.rows),
            page: page.page,
            hasMore: page.hasMore && page.rows.length > 0,
            recordingMode: page.recordingMode ?? previous.recordingMode,
            period: page.period ?? previous.period,
          };
        });
      })
      .catch((cause: unknown) => {
        if (mounted.current) {
          setError(cause);
        }
      })
      .finally(() => {
        if (mounted.current) {
          setLoadingMore(false);
        }
      });
  }, [loading, loadingMore]);

  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  return {
    items: state.items,
    error,
    loading,
    loadingMore,
    hasMore: state.hasMore,
    recordingMode: state.recordingMode,
    period: state.period,
    loadMore,
    reload,
  };
}

// ---------------------------------------------------------------------------------
// Playback (§4.6, §5.7, §9)
// ---------------------------------------------------------------------------------

/** The `<audio src>` and how long the grant behind it lives. */
export interface PlaybackSource {
  src: string;
  expiresIn: number | null;
  duration: number | null;
}

/** §9's shipping state: `RECORDING_MODE=off`, answered as a 409 machine code. */
export const RECORDING_OFF_CODE = 'recording_disabled';

/** The call exists but carries no audio (or none we can still reach). */
export const RECORDING_MISSING_CODES: ReadonlySet<string> = new Set([
  'record_missing',
  'call_not_found',
]);

/**
 * §9 step 3: a non-admin viewer must be proven to Bitrix24 with their *own* token.
 *
 * "Call Recording: Listen" is a permission REST does not expose, so the only honest
 * enforcement is to fetch the recording as the viewer. The endpoint asks for the token
 * with this code and the SPA answers it with `BX24.getAuth()`.
 */
export const VIEWER_TOKEN_REQUIRED = 'viewer_token_required';

/**
 * Mint the `?t=` playback grant for one call (§4.6).
 *
 * `viewerToken` is posted only when the server asked for it: the body is excluded from
 * every log (§6), and an administrator's playback needs no viewer token at all because
 * the portal token already has full telephony access (§11 assumption 4).
 */
export async function mintPlaybackSource(
  callId: number,
  viewerToken?: string | null,
  signal?: AbortSignal,
): Promise<PlaybackSource> {
  const raw = await apiFetch<unknown>(`/calls/${callId}/play-url`, {
    method: 'POST',
    body: viewerToken ? { access_token: viewerToken } : {},
    signal,
  });
  const body = asRecord(raw) ?? {};
  const url = str(body.url);
  if (!url) {
    // A 200 with no URL is a contract break, not a situation a user is in; it renders
    // the generic "something went wrong" copy like any other unknown code.
    throw new ApiError('recording_unavailable', 200);
  }
  return { src: url, expiresIn: num(body.expires_in), duration: num(body.duration) };
}

/**
 * The viewer's own Bitrix24 token, for the retry after `viewer_token_required`.
 *
 * `null` outside Bitrix24 or when the SDK refuses - the player then says the recording
 * cannot be played here rather than retrying forever.
 */
export async function viewerAccessToken(): Promise<string | null> {
  const auth = (await getAuth()) ?? (await refreshAuth());
  return auth?.access_token ?? null;
}

/**
 * §5.7 rule 3: tell the worker this row is stale after playback answered 403/404.
 *
 * Best effort by design - it is a hint to the next sync visit (and rate-limited per
 * portal on the server), and a failed hint must never become a second error message
 * stacked on the one the user already sees.
 */
export async function requestCallRefresh(callId: number): Promise<boolean> {
  try {
    await apiFetch<unknown>(`/calls/${callId}/refresh`, { method: 'POST', body: {} });
    return true;
  } catch {
    return false;
  }
}

/**
 * Where "open in Bitrix24" should land for a call.
 *
 * The CRM card when the row names one - those are documented `BX24.openPath` targets
 * (research doc: `/crm/deal/details/{id}/` and friends). Otherwise the Telephony section,
 * which every portal has: §9 has not pinned a per-call deep link yet, and an invented
 * path would only open Bitrix24's own "path not available" slider.
 */
export function recordingFallbackPath(call: CallRow): string {
  return crmEntityPath(call.crm?.type, call.crm?.id) ?? '/telephony/';
}

// ---------------------------------------------------------------------------------
// Display helpers
// ---------------------------------------------------------------------------------

/** §3's `result_group`, re-derived client-side if a projection ever omits it. */
export function resultGroupOf(call: CallRow): ResultGroup {
  const given = (call.result_group ?? '').trim();
  if (given === 'answered' || given === 'missed' || given === 'not_connected') {
    return given;
  }
  const code = (call.failed_code ?? '').trim();
  if (code === '200') {
    return 'answered';
  }
  return code === '304' ? 'missed' : 'not_connected';
}

/** A label and its tooltip. */
export interface Described {
  label: string;
  /** `undefined` when there is nothing more to say - never an empty tooltip. */
  title?: string;
}

/**
 * `CALL_FAILED_CODE` -> what the cell shows and what its tooltip says.
 *
 * §3 stores the code raw and enumerates nothing, so there are exactly three cases and
 * none of them is an empty cell:
 *
 *  * a code we have a word for -> the word, with the code in the tooltip;
 *  * a code we do not (a `603-S` from a new build, a provider's own string) -> **the code
 *    itself**, with "undefined result" in the tooltip;
 *  * no code at all -> the "undefined result" word, and no tooltip.
 */
export function describeResult(call: CallRow, translate: Copy): Described {
  const raw = (call.failed_code ?? '').trim();
  const unknownWord = translate('call.result.unknown');
  if (!raw) {
    return { label: unknownWord };
  }
  const key = resultKey(raw);
  if (key === 'call.result.unknown') {
    return { label: raw, title: unknownWord };
  }
  return { label: translate(key), title: raw };
}

/** `CALL_TYPE` -> the translated word, with the raw value in the tooltip. */
export function describeDirection(call: CallRow, translate: Copy): Described {
  const raw = call.call_type === null || call.call_type === undefined ? '' : String(call.call_type);
  return { label: translate(directionKey(call.call_type)), title: raw || undefined };
}

/** The employee cell: a cached name, or "User #id" for a placeholder / missing id (§7). */
export function describeEmployee(call: CallRow, translate: Copy): Described | null {
  const id = call.employee?.id ?? call.employee_id ?? null;
  if (id === null || id === undefined) {
    // A statistics row with no `PORTAL_USER_ID` belongs to nobody - §4.7's `own`
    // predicate excludes it for the same reason - so an empty cell is the honest answer.
    return null;
  }
  const name = (call.employee?.name ?? '').trim();
  if (!name) {
    return { label: translate('app.calls.table.employeeUnknown', { id }), title: `#${id}` };
  }
  return { label: name, title: `#${id}` };
}

/** The line cell's secondary line: the integration's name, or nothing for built-in. */
export function lineLabel(call: CallRow): string | null {
  const name = (call.line?.name ?? '').trim();
  return name || null;
}

// ---------------------------------------------------------------------------------
// Copy (§8)
// ---------------------------------------------------------------------------------

/**
 * The strings the call table, the player and the settings page introduce, in the shape
 * they take in `web/messages/<locale>.json`.
 *
 * §8 keeps ONE message source and this is not a second one: {@link useCopy} reads the
 * catalogue first and falls back here only for a key the catalogue does not carry yet, so
 * no portal renders a dotted key path while the translations land (§4.11 makes that a
 * moderation rejection). Adding these keys to `messages/en.json` and `messages/ru.json`
 * retires the whole map - it exists to be deleted, and it is laid out as the patch that
 * deletes it.
 */
export const NEW_MESSAGE_KEYS: Readonly<Record<string, string>> = {
  'app.calls.table.title': 'Calls',
  'app.calls.table.caption': 'One row per call, newest first',
  'app.calls.table.time': 'Time',
  'app.calls.table.timeOrder': 'Newest first',
  'app.calls.table.employee': 'Employee',
  'app.calls.table.direction': 'Direction',
  'app.calls.table.number': 'Number',
  'app.calls.table.crm': 'CRM',
  'app.calls.table.duration': 'Duration',
  'app.calls.table.result': 'Result',
  'app.calls.table.recording': 'Recording',
  'app.calls.table.empty': 'No calls in this period.',
  'app.calls.table.emptyImporting':
    'No calls in this period yet - the call history is still being imported.',
  'app.calls.table.loadMore': 'Load more',
  'app.calls.table.loadingMore': 'Loading...',
  'app.calls.table.shown': '{shown} calls',
  'app.calls.table.allShown': 'All calls in this period are shown.',
  'app.calls.table.employeeUnknown': 'User #{id}',
  'app.calls.table.dismissed': 'dismissed',
  'app.calls.table.play': 'Play the recording',
  'app.calls.table.hide': 'Hide the player',
  'app.calls.table.noRecording': 'No recording',
  'app.calls.player.loading': 'Preparing the recording...',
  'app.calls.player.offTitle': 'Recordings are played in Bitrix24',
  'app.calls.player.offBody':
    'This portal plays call recordings in Bitrix24 itself. Open the call there to listen to it.',
  'app.calls.player.open': 'Open in Bitrix24',
  'app.calls.player.openFailed':
    'Bitrix24 did not open the page. Look for the call in Bitrix24 under Telephony.',
  'app.calls.player.missing': 'This call has no recording any more.',
  'app.calls.player.stale':
    'The recording did not play. It may have been deleted in Bitrix24. A fresh link has been requested - try again in a moment.',
  'app.calls.player.failed':
    'The recording is still unavailable. Open the call in Bitrix24 to check whether it still exists.',
  'app.calls.player.noViewerToken':
    'Bitrix24 did not confirm your session, so the recording cannot be played here. Open the call in Bitrix24 instead.',
  'app.calls.player.retry': 'Try again',
  'app.calls.player.unsupported': 'This browser cannot play the recording.',
  'app.period.label': 'Period',
  'app.period.30d': '30 days',
  'app.period.90d': '90 days',
  'app.period.12m': '12 months',
  'app.settings.portalSection': 'Portal',
  'app.settings.placementsSection': 'CRM tab placements',
  'app.settings.qualitySection': 'Sync health',
  'app.settings.capabilitiesSection': 'Portal capabilities',
  'app.settings.tokenOwner': 'Authorised by',
  'app.settings.tokenAge': 'Authorisation age',
  'app.settings.tokenAgeDays': '{days} days',
  'app.settings.tokenRefreshed': 'Last renewed',
  'app.settings.tokenAdminVerified': 'Administrator rights last proven',
  'app.settings.tokenExpiring':
    'This authorisation expires about {days} days from now. Use "Re-authorise" to renew it.',
  'app.settings.reauthorize': 'Re-authorise',
  'app.settings.reauthorizeHint':
    'Re-authorises the app with your own Bitrix24 session. Only an administrator can do this.',
  'app.settings.reauthorizeOk': 'The app was re-authorised.',
  'app.settings.reauthorizeFailed':
    'Re-authorisation failed. Close this window, open the app from Bitrix24 again and retry.',
  'app.settings.reauthorizeNoAuth':
    'Bitrix24 did not provide a session for this window. Close it and open the app again.',
  'app.settings.rebind': 'Re-bind',
  'app.settings.rebindHint':
    'Re-binds the CRM tab in every language the app ships with. Safe to repeat.',
  'app.settings.rebindOk': 'The placements were re-bound.',
  'app.settings.rebindFailed': 'The placements could not be re-bound. Try again in a minute.',
  'app.settings.placementBound': 'Bound',
  'app.settings.placementFailed': 'Not bound',
  'app.settings.placementUnknown': 'Not attempted',
  'app.settings.quarantined': 'Quarantined rows',
  'app.settings.quarantinedHint':
    'Rows Bitrix24 returned that could not be read. Sync is not blocked by them, but a non-zero count is worth reporting.',
  'app.settings.failures': 'Consecutive failures',
  'app.settings.throttle': 'Rate-limit hits',
  'app.settings.nextRun': 'Next sync attempt',
  'app.settings.lastError': 'Last error',
  'app.settings.lastErrorNone': 'None',
  'app.settings.appVersion': 'App version',
  'app.settings.domain': 'Portal address',
  'app.settings.installedAt': 'Installed',
  'app.settings.lastAdminOpen': 'Last opened by an administrator',
  'app.settings.working': 'Working...',
  'app.settings.statusUnavailable':
    'The detailed status service did not answer, so this page shows the summary from your session only.',
  'app.settings.capabilityOn': 'yes',
  'app.settings.capabilityOff': 'no',
};

/** A translate function: the catalogue when it has the key, the fallback when not. */
export type Copy = (key: string, values?: Record<string, string | number>) => string;

function interpolate(template: string, values?: Record<string, string | number>): string {
  if (!values) {
    return template;
  }
  return template.replace(/\{(\w+)\}/g, (match: string, name: string) => {
    const value = values[name];
    return value === undefined ? match : String(value);
  });
}

/**
 * The one translate entry point these views use.
 *
 * It is `useTranslations()` with a seatbelt: a key the catalogue already carries is
 * translated exactly as anywhere else in the app (`call.result.*`, `crm.entity.*`,
 * `app.sync.*` all come out of the catalogue), and a key that has not been translated yet
 * renders its English default from {@link NEW_MESSAGE_KEYS} instead of the dotted path
 * next-intl would otherwise print into the frame.
 */
export function useCopy(): Copy {
  const t = useTranslations();
  return useCallback(
    (key: string, values?: Record<string, string | number>) => {
      try {
        const has = (t as unknown as { has?: (candidate: string) => boolean }).has;
        if (typeof has !== 'function' || has.call(t, key)) {
          return t(key, values);
        }
      } catch {
        // A missing namespace or a malformed ICU message: fall through to the default.
      }
      const fallback = NEW_MESSAGE_KEYS[key];
      return fallback === undefined ? key : interpolate(fallback, values);
    },
    [t],
  );
}
