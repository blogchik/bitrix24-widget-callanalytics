'use client';

/**
 * Call load by hour x weekday (§4.11 "Dashboard").
 *
 * A 7 x 24 CSS grid, one sequential blue ramp, lightest at zero. Never a rainbow and
 * never two hues: the question this answers is "when is the phone busy", which is one
 * ordered quantity, and a diverging or categorical palette would invent a midpoint the
 * data does not have.
 *
 * Two details that are requirements rather than taste:
 *
 *  * **The week starts on Monday** for both `ru` and `en` here. `Intl` would start the
 *    `en` week on Sunday, which puts the two quietest days at opposite ends of the grid
 *    and makes the working week impossible to read as a block.
 *  * **Every cell has a tooltip with the exact count.** A colour ramp is a rank cue, not
 *    a value cue; without the number the grid can only ever be read as "darker".
 *
 * The buckets come from `GET /api/v1/dashboard` already aggregated in the viewer's
 * timezone (§4.6 `tz`, open question 9), so nothing here converts a clock.
 */

import { useTranslations } from 'next-intl';
import { useMemo, useState, type MouseEvent as ReactMouseEvent } from 'react';

import { formatCount } from '@/lib/format';
import { RAMP, rampIndex, rampVar } from '@/lib/viz';

/** One (weekday, hour) bucket. `weekday` is ISO: 1 = Monday ... 7 = Sunday. */
export interface HourCell {
  weekday: number;
  hour: number;
  count: number;
}

export interface HourWeekdayHeatmapProps {
  cells: HourCell[];
  locale: string;
}

const HOURS = 24;
const DAYS = 7;

/** >=8px hit targets, from the specification; 16px also keeps the grid readable. */
const CELL_HEIGHT = 16;

/** ISO weekday order, Monday first, for both locales this app ships. */
const WEEKDAYS: readonly number[] = [1, 2, 3, 4, 5, 6, 7];

/** Any Monday, used only to ask `Intl` for weekday names in the viewer's language. */
const REFERENCE_MONDAY_UTC = Date.UTC(2024, 0, 1);

interface HoverState {
  weekday: number;
  hour: number;
  count: number;
  x: number;
  y: number;
}

function weekdayNames(locale: string): string[] {
  let format: Intl.DateTimeFormat;
  try {
    format = new Intl.DateTimeFormat(locale, { weekday: 'short', timeZone: 'UTC' });
  } catch {
    format = new Intl.DateTimeFormat(undefined, { weekday: 'short', timeZone: 'UTC' });
  }
  return WEEKDAYS.map((_, index) =>
    format.format(new Date(REFERENCE_MONDAY_UTC + index * 86_400_000)),
  );
}

export function HourWeekdayHeatmap({ cells, locale }: HourWeekdayHeatmapProps) {
  const t = useTranslations();
  const [hover, setHover] = useState<HoverState | null>(null);

  const { matrix, max } = useMemo(() => {
    const grid: number[][] = Array.from({ length: DAYS }, () => new Array<number>(HOURS).fill(0));
    let peak = 0;
    for (const cell of cells) {
      const row = cell.weekday - 1;
      if (row < 0 || row >= DAYS || cell.hour < 0 || cell.hour >= HOURS) {
        continue; // an out-of-range bucket is dropped, never wrapped into another day
      }
      const line = grid[row];
      if (!line) {
        continue;
      }
      line[cell.hour] = (line[cell.hour] ?? 0) + cell.count;
      peak = Math.max(peak, line[cell.hour] ?? 0);
    }
    return { matrix: grid, max: peak };
  }, [cells]);

  const names = useMemo(() => weekdayNames(locale), [locale]);

  const onEnter = (
    event: ReactMouseEvent<HTMLDivElement>,
    weekday: number,
    hour: number,
    count: number,
  ): void => {
    const cell = event.currentTarget;
    const host = cell.offsetParent as HTMLElement | null;
    const cellBox = cell.getBoundingClientRect();
    const hostBox = host?.getBoundingClientRect();
    setHover({
      weekday,
      hour,
      count,
      x: cellBox.left - (hostBox?.left ?? 0) + cellBox.width / 2,
      y: cellBox.top - (hostBox?.top ?? 0),
    });
  };

  return (
    <div>
      <div className="ca-viz-plot" style={{ overflowX: 'auto' }}>
        <div style={{ minWidth: 420 }}>
          {/* Hour scale: every third hour, so the labels never collide. */}
          <div
            className="grid"
            style={{
              gridTemplateColumns: `2.75rem repeat(${HOURS}, minmax(11px, 1fr))`,
              columnGap: 2,
              marginBottom: 4,
            }}
          >
            <span />
            {Array.from({ length: HOURS }, (_, hour) => (
              <span
                key={hour}
                className="ca-viz-label ca-viz-num"
                style={{ textAlign: 'center', lineHeight: 1 }}
              >
                {hour % 3 === 0 ? hour : ''}
              </span>
            ))}
          </div>

          {WEEKDAYS.map((weekday, row) => (
            <div
              key={weekday}
              className="grid"
              style={{
                gridTemplateColumns: `2.75rem repeat(${HOURS}, minmax(11px, 1fr))`,
                columnGap: 2,
                marginBottom: 2,
                alignItems: 'center',
              }}
            >
              <span className="ca-viz-label" style={{ lineHeight: 1 }}>
                {names[row]}
              </span>
              {Array.from({ length: HOURS }, (_, hour) => {
                const count = matrix[row]?.[hour] ?? 0;
                return (
                  <div
                    key={hour}
                    role="img"
                    className="ca-viz-cell"
                    style={{
                      height: CELL_HEIGHT,
                      background: rampVar(rampIndex(count, max)),
                    }}
                    onMouseEnter={(event) => onEnter(event, weekday, hour, count)}
                    onMouseLeave={() => setHover(null)}
                    aria-label={t('app.dashboard.heatmap.cell', {
                      weekday: names[row] ?? String(weekday),
                      hour: `${String(hour).padStart(2, '0')}:00`,
                      calls: t('app.dashboard.callsCount', { count }),
                    })}
                  />
                );
              })}
            </div>
          ))}
        </div>

        {hover ? (
          <div
            className="ca-viz-tooltip"
            style={{
              left: Math.max(0, hover.x - 70),
              top: Math.max(0, hover.y - 46),
              width: 140,
            }}
          >
            <div className="font-medium">
              {names[hover.weekday - 1] ?? ''} {String(hover.hour).padStart(2, '0')}:00
            </div>
            <div className="ca-viz-num ca-viz-ink-2">
              {t('app.dashboard.callsCount', { count: hover.count })}
            </div>
          </div>
        ) : null}
      </div>

      <ScaleLegend max={max} locale={locale} />
    </div>
  );
}

/**
 * The ends of the scale, spelled out.
 *
 * A sequential ramp with no legend is a picture of nothing: the reader has no way to
 * know whether the darkest cell is five calls or five hundred.
 */
function ScaleLegend({ max, locale }: { max: number; locale: string }) {
  const t = useTranslations();
  const steps = [0, 2, 4, 6, 8, 10, RAMP.length - 1];
  return (
    <div className="mt-3 flex items-center gap-2 text-[12px]">
      <span className="ca-muted">{t('app.dashboard.heatmap.legendLow')}</span>
      <span className="flex items-center gap-[2px]">
        {steps.map((step) => (
          <span
            key={step}
            className="ca-viz-swatch"
            style={{ background: rampVar(step), width: 14 }}
          />
        ))}
      </span>
      <span className="ca-viz-num ca-muted">
        {t('app.dashboard.heatmap.legendHigh', { count: formatCount(max, locale) })}
      </span>
    </div>
  );
}

export default HourWeekdayHeatmap;
