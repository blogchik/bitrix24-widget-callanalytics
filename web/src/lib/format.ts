/**
 * Display formatting. Pure functions only - no React, no DOM, no network.
 *
 * The code -> label maps come from the verified field semantics in
 * `docs/bitrix24-api-research.md`: `CALL_TYPE` is `1 outgoing, 2 incoming,
 * 3 incoming with redirection, 4 callback, 5 informational`. The app shows two of those,
 * because a portal asks "did it come in or go out", not "by which mechanism": a
 * redirected call still came in, and a callback is the system dialling out. `5` answers
 * neither question and is left ungrouped rather than filed under a direction it does not
 * have - the cell renders a dash and the raw code stays in its title.
 *
 * No map here holds a sentence: they return catalogue keys (§8), so every label stays
 * translatable and the one message source keeps its monopoly on user-visible text.
 */

/** Namespace prefix for `CALL_TYPE` labels in `messages/<locale>.json`. */
const DIRECTION_PREFIX = 'call.direction.';

/** The `CALL_TYPE` codes behind each direction (`services/stats.py::_DIRECTIONS`). */
const DIRECTIONS: Readonly<Record<string, ReadonlySet<string>>> = {
  incoming: new Set(['2', '3']),
  outgoing: new Set(['1', '4']),
};

/** The two directions, in the order the filter offers them. */
export const DIRECTION_GROUPS = ['incoming', 'outgoing'] as const;

export type DirectionGroup = (typeof DIRECTION_GROUPS)[number];

/**
 * A `CALL_TYPE` as one of the two directions, or `null` for a code that is neither.
 *
 * `null` is a real answer and not a gap: `5` (informational) is not a conversation with
 * a customer, and an unknown future code has no direction we can honestly claim. Both
 * render as a dash with the raw code in the cell's title.
 */
export function directionOf(
  callType: number | string | null | undefined,
): DirectionGroup | null {
  const value = callType === null || callType === undefined ? '' : String(callType).trim();
  if (!value) {
    return null;
  }
  return DIRECTION_GROUPS.find((group) => DIRECTIONS[group]?.has(value)) ?? null;
}

/** Catalogue key for a direction (§8). */
export function directionKey(group: DirectionGroup): string {
  return DIRECTION_PREFIX + group;
}

/**
 * An employee's name with their internal extension after it (§7).
 *
 * One function because the parenthesis rule is a display decision that three views share
 * - the filter, the call table and the comparison chart - and three copies of it is three
 * places for it to drift. An extension longer than {@link EXTENSION_MAX_CHARS} is dropped
 * rather than shown: `UF_PHONE_INNER` is free text and a portal that stores a whole phone
 * number in it would widen every row that carries one.
 */
export const EXTENSION_MAX_CHARS = 8;

export function withExtension(
  name: string | null | undefined,
  extension: string | null | undefined,
): string | null {
  const label = (name ?? '').trim();
  if (!label) {
    return null;
  }
  const trimmed = (extension ?? '').trim();
  return trimmed && trimmed.length <= EXTENSION_MAX_CHARS ? `${label} (${trimmed})` : label;
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
