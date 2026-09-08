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
 *
 * ---------------------------------------------------------------------------------
 * **Why the grid is fluid and only then scrollable.**
 *
 * This shape used to be laid out at a fixed 420px inside an `overflow-x: auto` box. On a
 * 375px phone that meant the cells ran from x=49 to x=469: six hours of the day existed,
 * were paid for, and were invisible, with nothing on screen to say so. A chart that hides
 * a quarter of its data without admitting it is worse than one that does not fit.
 *
 * So the twenty-four columns are `minmax(<floor>, 1fr)`: they shrink with the panel until
 * a cell reaches the specification's 8px hit-target floor, which at 375 lands around 10px
 * and fits whole. Only below that - a narrower frame than any phone we ship to - does the
 * region scroll, and then it says so: a fade at the edge, `tabindex` and a named region so
 * a keyboard and a screen reader can both reach the hours off screen, and the weekday
 * labels pinned to the left so a scrolled grid still has a y axis.
 * ---------------------------------------------------------------------------------
 */

import { useTranslations } from 'next-intl';
import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react';

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

/** ISO weekday order, Monday first, for both locales this app ships. */
const WEEKDAYS: readonly number[] = [1, 2, 3, 4, 5, 6, 7];

/** Any Monday, used only to ask `Intl` for weekday names in the viewer's language. */
const REFERENCE_MONDAY_UTC = Date.UTC(2024, 0, 1);

const STYLE_ID = 'ca-heatmap';

/** The tooltip's fixed width, so it can be kept inside the panel it is drawn over. */
const TOOLTIP_W = 140;

/**
 * A couple of pixels of slack before the region calls itself scrollable.
 *
 * An hour label is centred in a track narrower than the label itself, so it bleeds a
 * pixel or two into the gap beside it; without the slack that ink alone would light up
 * the "there is more over here" fade on a grid that fits perfectly.
 */
const SCROLL_SLACK = 4;

/*
 * Geometry lives in CSS rather than in inline styles because the only thing that changes
 * between a phone and a slider is three numbers, and a media query changes them without
 * a React render or a measurement pass.
 *
 * `--ca-hm-cell` is the floor, not the size: the tracks are `minmax(floor, 1fr)`, so they
 * take whatever the panel offers and stop shrinking at the specification's >=8px target.
 */
const CSS = `
.ca-hm {
  position: relative;
}
.ca-hm-scroll {
  overflow-x: auto;
  overscroll-behavior-x: contain;
}
.ca-hm-grid {
  --ca-hm-label: 30px;
  --ca-hm-cell: 9px;
  --ca-hm-gap: 1px;
  --ca-hm-row: 16px;
  display: grid;
  grid-template-columns: var(--ca-hm-label) repeat(${HOURS}, minmax(var(--ca-hm-cell), 1fr));
  column-gap: var(--ca-hm-gap);
  row-gap: 2px;
  align-items: center;
}
@media (min-width: 480px) {
  .ca-hm-grid {
    --ca-hm-label: 34px;
    --ca-hm-cell: 11px;
    --ca-hm-gap: 2px;
  }
}
.ca-hm-hour {
  text-align: center;
  line-height: 1;
  padding-bottom: 4px;
}
/* Below 480px only every sixth hour is labelled: at ~10px a track, every third label
   would sit on top of its neighbour. Hidden rather than removed, so the columns the
   labels belong to keep their place in the grid. */
@media (max-width: 479px) {
  .ca-hm-hour:not(.ca-hm-hour-6) {
    visibility: hidden;
  }
}
.ca-hm-day {
  position: sticky;
  left: 0;
  z-index: 1;
  line-height: 1;
  padding-right: 4px;
  background: var(--ca-surface);
}
.ca-hm-cell {
  height: var(--ca-hm-row);
}
.ca-hm-fade {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  width: 28px;
  pointer-events: none;
  background: linear-gradient(to left, var(--ca-surface), transparent);
}
`;

interface HoverState {
  weekday: number;
  hour: number;
  count: number;
  /** Already clamped into the panel, so the tooltip can never leave the viewport. */
  left: number;
  top: number;
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
  const [scrollable, setScrollable] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

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

  /*
   * The affordance is only offered when it is true.
   *
   * A fade painted over a grid that fits would be a lie about the data, and a focus stop
   * on a region nobody can scroll is a keyboard tab that does nothing. Observing the
   * scroller costs one boolean and changes no layout, so it cannot feed back into
   * `AppFrame`'s own observer.
   */
  useEffect(() => {
    const node = scrollRef.current;
    if (!node) {
      return;
    }
    const measure = () => {
      setScrollable(node.scrollWidth - node.clientWidth > SCROLL_SLACK);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const onEnter = (
    event: ReactMouseEvent<HTMLDivElement>,
    weekday: number,
    hour: number,
    count: number,
  ): void => {
    const cell = event.currentTarget;
    const host = cell.closest('.ca-hm');
    const cellBox = cell.getBoundingClientRect();
    const hostBox = host?.getBoundingClientRect();
    const centre = cellBox.left - (hostBox?.left ?? 0) + cellBox.width / 2;
    const room = (hostBox?.width ?? 0) - TOOLTIP_W;
    setHover({
      weekday,
      hour,
      count,
      left: Math.max(0, Math.min(centre - TOOLTIP_W / 2, room)),
      top: Math.max(0, cellBox.top - (hostBox?.top ?? 0) - 46),
    });
  };

  return (
    <div>
      <style href={STYLE_ID} precedence="default" dangerouslySetInnerHTML={{ __html: CSS }} />

      <div className="ca-hm">
        <div
          ref={scrollRef}
          className="ca-hm-scroll"
          // A region only a mouse can pan is a region a keyboard user cannot read.
          {...(scrollable
            ? { tabIndex: 0, role: 'region', 'aria-label': t('app.dashboard.heatmap.title') }
            : {})}
        >
          <div className="ca-hm-grid">
            {/* Hour scale. The empty corner is sticky too, so the hours pan *under* it
                rather than over the weekday column when the grid is scrolled. */}
            <span className="ca-hm-day" />
            {Array.from({ length: HOURS }, (_, hour) => (
              <span
                key={hour}
                className={
                  'ca-viz-label ca-viz-num ca-hm-hour' + (hour % 6 === 0 ? ' ca-hm-hour-6' : '')
                }
              >
                {hour % 3 === 0 ? hour : ''}
              </span>
            ))}

            {WEEKDAYS.map((weekday, row) => (
              <Fragment key={weekday}>
                <span className="ca-viz-label ca-hm-day">{names[row]}</span>
                {Array.from({ length: HOURS }, (_, hour) => {
                  const count = matrix[row]?.[hour] ?? 0;
                  return (
                    <div
                      key={hour}
                      role="img"
                      className="ca-viz-cell ca-hm-cell"
                      style={{ background: rampVar(rampIndex(count, max)) }}
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
              </Fragment>
            ))}
          </div>
        </div>

        {scrollable ? <div className="ca-hm-fade" aria-hidden="true" /> : null}

        {hover ? (
          <div
            className="ca-viz-tooltip"
            style={{ left: hover.left, top: hover.top, width: TOOLTIP_W }}
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
    <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px]">
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
