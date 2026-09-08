/**
 * Calendar-date arithmetic for the filter controls (§4.11, §10 step 5).
 *
 * **Everything here is a calendar date, never an instant.** A `YYYY-MM-DD` in this app
 * means "that day in the *viewer's* timezone" (§4.6 `tz`), which is what the server
 * aggregates by. The one mistake this module exists to make impossible is the
 * `new Date(...).toISOString().slice(0, 10)` round trip: west of UTC that turns local
 * midnight into the previous day, and the dashboard then quietly loses a day of calls at
 * one end of every period. So the dates below are built from **local** year/month/day
 * parts, and every `Date` used as scratch space is anchored at **local noon**, which is
 * the only hour no DST transition can move across a day boundary.
 *
 * Two consequences worth knowing before using this:
 *
 *  * `YYYY-MM-DD` is zero-padded and fixed-width, so `a < b` on the strings *is* date
 *    comparison. Nothing here wraps that in a `compare()` - the operator is correct.
 *  * The functions are total: given something unparsable they return the input rather
 *    than `NaN` or a throw, because a date control must never be able to blank the page
 *    (§4.11).
 */

/** A calendar date as `YYYY-MM-DD`. Zero-padded, so string order is date order. */
export type IsoDate = string;

const ISO_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;

/** `true` when `value` is a well-formed, really-existing calendar date. */
export function isIsoDate(value: string | null | undefined): value is IsoDate {
  return typeof value === 'string' && fromIso(value) !== null;
}

/** A `Date` (read in local time) as `YYYY-MM-DD`. Never touches UTC. */
export function toIso(date: Date): IsoDate {
  const pad = (value: number): string => String(value).padStart(2, '0');
  return `${String(date.getFullYear()).padStart(4, '0')}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/**
 * `YYYY-MM-DD` -> a local-noon `Date`, or `null` if that day does not exist.
 *
 * Noon, not midnight: Brazil and others have historically moved the clock *at* midnight,
 * so a midnight anchor plus a day of arithmetic can land on 23:00 of the day before.
 * `setFullYear` rather than the constructor's year argument, which maps 0-99 to 1900-1999.
 */
export function fromIso(iso: string | null | undefined): Date | null {
  if (!iso) {
    return null;
  }
  const match = ISO_PATTERN.exec(iso);
  if (!match) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(2000, 0, 1, 12, 0, 0, 0);
  date.setFullYear(year, month - 1, day);
  // Rejects `2026-02-31` and friends, which the constructor would happily roll over.
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) {
    return null;
  }
  return date;
}

/**
 * Today as `YYYY-MM-DD` in `timeZone` - the viewer's zone (§4.6), not the browser's.
 *
 * `en-CA` is the one widely available locale whose short date already is ISO order, which
 * is how a timezone-correct "today" is obtained without an instant ever being formatted.
 */
export function todayIso(timeZone?: string | null): IsoDate {
  try {
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: timeZone ?? undefined,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date());
  } catch {
    return toIso(new Date());
  }
}

/** `iso` moved by `days` (may be negative). Returns `iso` unchanged if unparsable. */
export function addDays(iso: IsoDate, days: number): IsoDate {
  const date = fromIso(iso);
  if (!date) {
    return iso;
  }
  date.setDate(date.getDate() + days);
  return toIso(date);
}

/** `iso` moved by `months`, clamping the day into the target month (Jan 31 + 1 = Feb 28). */
export function addMonths(iso: IsoDate, months: number): IsoDate {
  const date = fromIso(iso);
  if (!date) {
    return iso;
  }
  const day = date.getDate();
  date.setDate(1);
  date.setMonth(date.getMonth() + months);
  date.setDate(Math.min(day, daysInMonth(date.getFullYear(), date.getMonth())));
  return toIso(date);
}

/** The first day of `iso`'s month. */
export function startOfMonth(iso: IsoDate): IsoDate {
  const date = fromIso(iso);
  if (!date) {
    return iso;
  }
  date.setDate(1);
  return toIso(date);
}

/** The last day of `iso`'s month. */
export function endOfMonth(iso: IsoDate): IsoDate {
  const date = fromIso(iso);
  if (!date) {
    return iso;
  }
  date.setDate(daysInMonth(date.getFullYear(), date.getMonth()));
  return toIso(date);
}

/** Whole days from `from` to `to`, exclusive (`dayDiff(d, d) === 0`). */
export function dayDiff(from: IsoDate, to: IsoDate): number {
  const a = fromIso(from);
  const b = fromIso(to);
  if (!a || !b) {
    return 0;
  }
  // Both anchors are local noon, so the difference is whole days plus at most one hour
  // of DST slack; rounding is exact, never off by one.
  return Math.round((b.getTime() - a.getTime()) / 86_400_000);
}

/** Inclusive length of `[from, to]` in days - what the server caps at `MAX_PERIOD_DAYS`. */
export function spanDays(from: IsoDate, to: IsoDate): number {
  return dayDiff(from, to) + 1;
}

/** `iso` held inside `[min, max]`. */
export function clampIso(iso: IsoDate, min: IsoDate, max: IsoDate): IsoDate {
  if (iso < min) {
    return min;
  }
  return iso > max ? max : iso;
}

/** Weekday index with **Monday as 0** - the week this product starts on, in every locale. */
export function weekdayIndex(iso: IsoDate): number {
  const date = fromIso(iso);
  if (!date) {
    return 0;
  }
  return (date.getDay() + 6) % 7;
}

/** How many days a month has. `month` is 0-based, as on `Date`. */
export function daysInMonth(year: number, month: number): number {
  const probe = new Date(2000, 0, 1, 12, 0, 0, 0);
  probe.setFullYear(year, month + 1, 0);
  return probe.getDate();
}

/**
 * A month as six Monday-first weeks of seven slots.
 *
 * Slots outside the month are `null` rather than the neighbouring month's date: rendering
 * the real neighbours would put the same day on screen twice in a two-month view, and a
 * duplicated day breaks both the roving focus and the range highlight. Always six rows,
 * so the panel's height does not change as the user pages through months - a panel that
 * resizes under an open pointer is a panel that gets mis-clicked.
 */
export function monthGrid(iso: IsoDate): (IsoDate | null)[][] {
  const first = startOfMonth(iso);
  const date = fromIso(first);
  if (!date) {
    return [];
  }
  const lead = weekdayIndex(first);
  const total = daysInMonth(date.getFullYear(), date.getMonth());
  const weeks: (IsoDate | null)[][] = [];
  for (let week = 0; week < 6; week += 1) {
    const row: (IsoDate | null)[] = [];
    for (let slot = 0; slot < 7; slot += 1) {
      const dayNumber = week * 7 + slot - lead + 1;
      row.push(dayNumber >= 1 && dayNumber <= total ? addDays(first, dayNumber - 1) : null);
    }
    weeks.push(row);
  }
  return weeks;
}
