/**
 * Display formatting. Pure functions only - no React, no DOM, no network.
 *
 * The code -> label maps come from the verified field semantics in
 * `docs/bitrix24-api-research.md`: `CALL_TYPE` is `1 outgoing, 2 incoming,
 * 3 incoming with redirection, 4 callback, 5 informational`, and `CALL_FAILED_CODE` is
 * a *string* whose documented values include `603-S` and `OTHER`. Neither map holds a
 * sentence: they return catalogue keys (§8), so every label stays translatable and the
 * one message source keeps its monopoly on user-visible text.
 */

/** Namespace prefix for `CALL_TYPE` labels in `messages/<locale>.json`. */
const DIRECTION_PREFIX = 'call.direction.';

/** Namespace prefix for `CALL_FAILED_CODE` labels. */
const RESULT_PREFIX = 'call.result.';

/** Documented `CALL_TYPE` values; anything else is rendered as "unknown". */
const DIRECTIONS: ReadonlySet<string> = new Set(['1', '2', '3', '4', '5']);

/** Documented `CALL_FAILED_CODE` values (research doc, "(a) CALL_FAILED_CODE values"). */
const RESULT_CODES: ReadonlySet<string> = new Set([
  '200',
  '304',
  '402',
  '403',
  '404',
  '423',
  '480',
  '484',
  '486',
  '503',
  '603',
  '603-S',
  'OTHER',
]);

/** Catalogue key for a `CALL_TYPE`. */
export function directionKey(callType: number | string | null | undefined): string {
  const value = callType === null || callType === undefined ? '' : String(callType).trim();
  return DIRECTION_PREFIX + (DIRECTIONS.has(value) ? value : 'unknown');
}

/** Catalogue key for a `CALL_FAILED_CODE`. Codes are strings: `603-S` is real. */
export function resultKey(failedCode: string | number | null | undefined): string {
  const value = failedCode === null || failedCode === undefined ? '' : String(failedCode).trim();
  const upper = value.toUpperCase();
  return RESULT_PREFIX + (RESULT_CODES.has(upper) ? upper : 'unknown');
}

/** `200` is the only success code; everything else is a failure of some kind. */
export function isSuccessfulCall(failedCode: string | number | null | undefined): boolean {
  return String(failedCode ?? '').trim() === '200';
}

/**
 * Seconds -> `m:ss`, or `h:mm:ss` past an hour.
 *
 * `CALL_DURATION` arrives as a string of seconds and is 0 for an unanswered call, so
 * `0:00` is a meaningful value and is not blanked.
 */
export function formatDuration(seconds: number | string | null | undefined): string {
  const total = typeof seconds === 'string' ? Number(seconds) : seconds;
  if (total === null || total === undefined || !Number.isFinite(total) || total < 0) {
    return '—';
  }
  const whole = Math.floor(total);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const rest = whole % 60;
  const pad = (value: number) => String(value).padStart(2, '0');
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${minutes}:${pad(rest)}`;
}

/**
 * `PHONE_NUMBER` for display.
 *
 * Deliberately country-agnostic: it keeps a leading `+`, drops formatting noise and
 * groups only the last seven digits (`... 123 45 67`), which reads correctly in every
 * market this app targets without pretending to know a national numbering plan. An
 * internal extension (three or four digits) is returned untouched.
 */
export function formatPhone(raw: string | null | undefined): string {
  if (!raw) {
    return '—';
  }
  const trimmed = raw.trim();
  if (!trimmed) {
    return '—';
  }
  const plus = trimmed.startsWith('+');
  const digits = trimmed.replace(/\D/g, '');
  if (!digits) {
    return trimmed; // a SIP address or an alias: show it verbatim
  }
  if (digits.length < 9) {
    return (plus ? '+' : '') + digits;
  }
  const head = digits.slice(0, digits.length - 7);
  const tail = digits.slice(digits.length - 7);
  return `${plus ? '+' : ''}${head} ${tail.slice(0, 3)} ${tail.slice(3, 5)} ${tail.slice(5, 7)}`;
}

/** Bitrix24 CRM entity type -> the slider path segment. */
const CRM_PATH_SEGMENTS: Readonly<Record<string, string>> = {
  LEAD: 'lead',
  DEAL: 'deal',
  CONTACT: 'contact',
  COMPANY: 'company',
};

/**
 * The path for `BX24.openPath()` (§4.10), e.g. `/crm/deal/details/123/`.
 *
 * `null` for an entity type we have no route for - the caller then renders plain text
 * instead of a link that would open nothing.
 */
export function crmEntityPath(
  entityType: string | null | undefined,
  entityId: number | null | undefined,
): string | null {
  if (!entityType || entityId === null || entityId === undefined || !Number.isFinite(entityId)) {
    return null;
  }
  const segment = CRM_PATH_SEGMENTS[entityType.trim().toUpperCase()];
  if (!segment) {
    return null;
  }
  return `/crm/${segment}/details/${Math.trunc(entityId)}/`;
}

/** Catalogue key for a CRM entity type label. */
export function crmEntityKey(entityType: string | null | undefined): string {
  const upper = (entityType ?? '').trim().toUpperCase();
  return 'crm.entity.' + (upper in CRM_PATH_SEGMENTS ? upper : 'unknown');
}

/** Grouped integer in the viewer's locale (`12 300`, `12,300`). */
export function formatCount(value: number | null | undefined, locale: string): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—';
  }
  try {
    return new Intl.NumberFormat(locale).format(value);
  } catch {
    return String(value);
  }
}

/**
 * ISO timestamp -> local text in the *viewer's* timezone (the JWT `tz` claim, §4.6).
 *
 * Never the browser's timezone: a portal in another region must read its own clock.
 */
export function formatDateTime(
  value: string | null | undefined,
  locale: string,
  timeZone: string | null | undefined,
): string {
  if (!value) {
    return '—';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '—';
  }
  try {
    return new Intl.DateTimeFormat(locale, {
      dateStyle: 'medium',
      timeStyle: 'short',
      timeZone: timeZone ?? undefined,
    }).format(date);
  } catch {
    // An unknown IANA zone from a misconfigured portal must not blank the page.
    return date.toISOString().replace('T', ' ').slice(0, 16);
  }
}
