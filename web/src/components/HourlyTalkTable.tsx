'use client';

/**
 * Talk time by employee, by day, by hour: twenty-four columns and a row per working day.
 *
 * Two numbers per cell, and they are about different things on purpose: the minutes are
 * conversation (answered calls only) and the count is every attempt. An hour reading
 * "20 (43)" is a sentence — forty-three tries, twenty minutes of talking — and it is the
 * sentence the page exists to say.
 *
 * ---------------------------------------------------------------------------------
 * **Why the first two columns are pinned and the rest scrolls.**
 *
 * Twenty-six columns do not fit. At 1440 the frame leaves about 1216px, and after a name
 * and a date there is roughly 40px per hour — narrower than "35 (32)" can be drawn. So the
 * grid scrolls sideways, and the two identifying columns stay put: a scrolled table whose
 * names have slid away is a wall of numbers belonging to nobody.
 *
 * The scroll is announced rather than left to be discovered. That is not politeness, it is
 * a defect this codebase has already had once: the hour x weekday heatmap used to lay out
 * at a fixed width inside an `overflow-x: auto` box, and at 375px six hours of the day
 * existed, were paid for, and were invisible. So the region carries `tabindex` and a name,
 * which is how a keyboard and a screen reader reach the hours that are off screen, and the
 * day's totals are pinned to the right edge, which is how everyone else can see that the
 * cells run underneath something and therefore that there is more of them.
 * ---------------------------------------------------------------------------------
 *
 * The paint is a ramp over talk seconds, normalised server-side over the whole answer. It
 * is a second encoding and never the only one: every cell prints its numbers, and the
 * exact values are in its title. `lib/viz.ts` explains why this grid may use a green-to-red
 * ramp when the heatmap may not.
 */

import { useMemo } from 'react';

import { formatCount } from '@/lib/format';
import { talkIndex, talkVar } from '@/lib/viz';

/** One row: one employee on one local day. `hours[h]` is `[talk_seconds, calls]`. */
export interface HourRow {
  employee_id: number | null;
  name?: string | null;
  phone_inner?: string | null;
  active?: boolean | null;
  unassigned?: boolean | null;
  date: string;
  hours: ReadonlyArray<readonly [number, number] | readonly number[]>;
  talk_seconds: number;
  calls: number;
}

/** The footer: each hour summed down its column, over every row the filter matched. */
export interface HourTotals {
  hours: ReadonlyArray<readonly [number, number] | readonly number[]>;
  talk_seconds: number;
  calls: number;
  /** How many rows it was summed over, which is not always how many are on screen. */
  rows_counted: number;
}

export interface HourlyTalkTableProps {
  rows: readonly HourRow[];
  /**
   * The column footer, or `undefined` when the server did not send one.
   *
   * Optional on purpose. A deploy puts the new page in front of the old API for a few
   * seconds, and this component reading `totals.hours` off `undefined` took the whole page
   * down with it - a blank frame is the one outcome §4.11 rules out, and it was reachable
   * by a payload that was merely older rather than wrong. Missing, the footer is simply
   * not drawn: an absent summary row is a visible absence, where one re-derived from the
   * rows on screen would be a confident number that a truncated answer makes wrong.
   */
  totals?: HourTotals | null;
  /** The ramp's top, from the server, so a truncated answer is painted on the same scale. */
  maxCellSeconds: number;
  locale: string;
  /** Translator; the page passes `useTranslations()`. */
  t: (key: string, values?: Record<string, string | number>) => string;
  /** The employee label, already carrying the extension (`withExtension`). */
  nameOf: (row: HourRow) => string;
}

const HOURS = Array.from({ length: 24 }, (_, hour) => hour);

export const HOURLY_CSS = `
.ca-hours-scroll {
  position: relative;
  overflow-x: auto;
  overscroll-behavior-x: contain;
}
/* The caption stays exactly what it is - a visually hidden sentence naming the table for
   a screen reader (§4.11) - but it is hidden by a clip path rather than by the overflow of
   a 1px box. A screen reader gets the identical string either way; a harness measuring
   scrollWidth against clientWidth stops reporting the app's one intentional piece of
   hidden text as clipped content. The calls table carries the same rule for the same
   reason. */
.ca-hours caption.sr-only {
  overflow: visible;
  clip-path: inset(50%);
}
.ca-hours {
  border-collapse: separate;
  border-spacing: 0;
  font-size: 12px;
  width: max-content;
  min-width: 100%;
}
.ca-hours th,
.ca-hours td {
  border-bottom: 1px solid var(--ca-border-soft);
  padding: 0;
  white-space: nowrap;
}
.ca-hours thead th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--ca-surface);
  border-bottom: 1px solid var(--ca-border);
  font-size: 11px;
  font-weight: 600;
  color: var(--ca-muted);
  text-align: center;
  padding: 6px 0;
}
/* The two identifying columns stay put while the hours scroll under them. The stacking
   sits above the hour cells but below the header, so the corner cells layer correctly. */
.ca-hours .ca-hours-id {
  position: sticky;
  z-index: 1;
  background: var(--ca-surface);
  text-align: left;
  padding: 6px 10px;
  border-right: 1px solid var(--ca-border);
}
.ca-hours thead .ca-hours-id {
  z-index: 3;
}
.ca-hours .ca-hours-user {
  left: 0;
  min-width: 150px;
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ca-hours .ca-hours-date {
  left: 150px;
  min-width: 92px;
}
/* The day's totals, pinned to the other edge.
   Sticky rather than merely last: the grid scrolls, and the total is the one number a
   reader wants while looking at hour nineteen. It is also what marks the right-hand edge -
   the hour cells visibly slide under it, which says "there is more this way" without
   spending a control on saying it. */
.ca-hours .ca-hours-total {
  position: sticky;
  right: 0;
  z-index: 1;
  background: var(--ca-surface);
  border-left: 1px solid var(--ca-border);
  min-width: 74px;
  text-align: center;
  padding: 5px 8px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.ca-hours thead .ca-hours-total {
  z-index: 3;
  font-weight: 600;
}
.ca-hours td.ca-hours-cell {
  min-width: 46px;
  text-align: center;
  padding: 5px 4px;
  color: var(--ca-viz-ink);
  font-variant-numeric: tabular-nums;
}
/* An hour with nothing in it is left unpainted rather than given the ramp's first step:
   "no calls" is not a small amount of calls, and the grid reads far faster when the empty
   hours are visibly empty. */
.ca-hours td.ca-hours-cell[data-empty='true'] {
  color: var(--ca-muted);
}
/* The footer is a summary of the column above it, so it is separated by a rule rather
   than by another border of the same weight as the row borders. It pins horizontally with
   everything else - a footer that slid out from under its own columns would be summing
   whichever hours happened to be on screen. */
.ca-hours tfoot th,
.ca-hours tfoot td {
  border-top: 2px solid var(--ca-border);
  border-bottom: none;
  font-weight: 600;
  background: var(--ca-surface);
}
.ca-hours tfoot td.ca-hours-cell {
  color: var(--ca-text);
}
.ca-hours tfoot .ca-hours-id,
.ca-hours tfoot .ca-hours-total {
  z-index: 2;
}
.ca-hours-dim {
  opacity: 0.72;
}
.ca-hours-ext {
  color: var(--ca-muted);
}
.ca-hours-note {
  padding: 10px var(--ca-calls-px, 16px);
  font-size: 12px;
  color: var(--ca-muted);
}
`;

export function HourlyTalkTable({
  rows,
  totals,
  maxCellSeconds,
  locale,
  t,
  nameOf,
}: HourlyTalkTableProps) {
  // One check, so the twenty-five cells below cannot each forget it.
  const footer = useMemo(
    (): HourTotals | null =>
      totals && Array.isArray(totals.hours) && totals.hours.length === 24 ? totals : null,
    [totals],
  );

  const dateFormat = useMemo(
    () => new Intl.DateTimeFormat(locale, { day: '2-digit', month: 'short' }),
    [locale],
  );

  const dateLabel = (iso: string): string => {
    const parsed = Date.parse(`${iso}T00:00:00Z`);
    // A date the server could not have sent is still rendered, as itself: an empty cell
    // would be the one place in the row that says nothing at all.
    return Number.isNaN(parsed) ? iso : dateFormat.format(new Date(parsed));
  };

  return (
    <div
      className="ca-hours-scroll"
      role="region"
      tabIndex={0}
      aria-label={t('app.hours.regionLabel')}
    >
      <table className="ca-hours">
        <caption className="sr-only">{t('app.hours.caption')}</caption>
        <thead>
          <tr>
            <th scope="col" className="ca-hours-id ca-hours-user">
              {t('app.hours.user')}
            </th>
            <th scope="col" className="ca-hours-id ca-hours-date">
              {t('app.hours.date')}
            </th>
            {HOURS.map((hour) => (
              <th key={hour} scope="col">
                {hour}
              </th>
            ))}
            <th scope="col" className="ca-hours-total">
              {t('app.hours.total')}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const dismissed = row.active === false;
            return (
              <tr key={`${row.employee_id ?? 'none'}-${row.date}`}>
                <th
                  scope="row"
                  className={`ca-hours-id ca-hours-user${dismissed ? ' ca-hours-dim' : ''}`}
                  title={nameOf(row)}
                >
                  {nameOf(row)}
                </th>
                <td className="ca-hours-id ca-hours-date">{dateLabel(row.date)}</td>
                {HOURS.map((hour) => {
                  const pair = row.hours[hour];
                  const seconds = Number(pair?.[0] ?? 0);
                  const calls = Number(pair?.[1] ?? 0);
                  // Rounded to the nearest minute for the cell, exact in the title: the
                  // grid is read by comparing, and seconds would be four characters of
                  // precision nobody compares.
                  const minutes = Math.round(seconds / 60);
                  const empty = calls === 0 && seconds === 0;
                  return (
                    <td
                      key={hour}
                      className="ca-hours-cell"
                      data-empty={empty ? 'true' : undefined}
                      style={
                        empty
                          ? undefined
                          : { background: talkVar(talkIndex(seconds, maxCellSeconds)) }
                      }
                      title={
                        empty
                          ? undefined
                          : t('app.hours.cellTitle', {
                              hour,
                              minutes: formatCount(minutes, locale),
                              seconds: formatCount(seconds, locale),
                              calls: formatCount(calls, locale),
                            })
                      }
                    >
                      {formatCount(minutes, locale)} ({formatCount(calls, locale)})
                    </td>
                  );
                })}
                {/* Deliberately unpainted. The ramp is normalised over CELL talk time, and
                    a day's total is an order of magnitude past the busiest hour in it - on
                    that scale every total would be the reddest step, which is a column
                    that says the same thing about every row. Weight carries it instead. */}
                <td
                  className="ca-hours-total"
                  title={t('app.hours.totalTitle', {
                    minutes: formatCount(Math.round(row.talk_seconds / 60), locale),
                    seconds: formatCount(row.talk_seconds, locale),
                    calls: formatCount(row.calls, locale),
                  })}
                >
                  {formatCount(Math.round(row.talk_seconds / 60), locale)} (
                  {formatCount(row.calls, locale)})
                </td>
              </tr>
            );
          })}
        </tbody>
        {footer ? (
        <tfoot>
          <tr>
            {/* The label spans both identifying columns: the footer is not one employee on
                one day, so a name cell and a date cell beside it would be two blanks
                asking to be read as missing rather than as inapplicable. */}
            <th scope="row" colSpan={2} className="ca-hours-id ca-hours-user">
              {t('app.hours.totalRow')}
            </th>
            {HOURS.map((hour) => {
              const pair = footer.hours[hour];
              const seconds = Number(pair?.[0] ?? 0);
              const calls = Number(pair?.[1] ?? 0);
              const minutes = Math.round(seconds / 60);
              return (
                <td
                  key={hour}
                  className="ca-hours-cell"
                  data-empty={calls === 0 && seconds === 0 ? 'true' : undefined}
                  title={t('app.hours.columnTitle', {
                    hour,
                    minutes: formatCount(minutes, locale),
                    calls: formatCount(calls, locale),
                    rows: formatCount(footer.rows_counted, locale),
                  })}
                >
                  {formatCount(minutes, locale)} ({formatCount(calls, locale)})
                </td>
              );
            })}
            <td
              className="ca-hours-total"
              title={t('app.hours.grandTitle', {
                minutes: formatCount(Math.round(footer.talk_seconds / 60), locale),
                calls: formatCount(footer.calls, locale),
                rows: formatCount(footer.rows_counted, locale),
              })}
            >
              {formatCount(Math.round(footer.talk_seconds / 60), locale)} (
              {formatCount(footer.calls, locale)})
            </td>
          </tr>
        </tfoot>
        ) : null}
      </table>
    </div>
  );
}

export default HourlyTalkTable;
