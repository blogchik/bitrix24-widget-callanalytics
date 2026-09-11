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
 * existed, were paid for, and were invisible. Hence the fade at the edge, the `tabindex`
 * and the named region, so a keyboard and a screen reader can both reach the hours that
 * are off screen.
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

export interface HourlyTalkTableProps {
  rows: readonly HourRow[];
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
/* The fade says "there is more this way" without spending a control on saying it. It sits
   above the cells and takes no pointer events, so it can never eat a tap. */
.ca-hours-scroll::after {
  content: '';
  position: sticky;
  top: 0;
  right: 0;
  float: right;
  width: 24px;
  height: 1px;
  pointer-events: none;
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
  maxCellSeconds,
  locale,
  t,
  nameOf,
}: HourlyTalkTableProps) {
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
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default HourlyTalkTable;
