'use client';

/**
 * Calls per day, stacked by result (§4.11 "Dashboard").
 *
 * Hand-rolled inline SVG, no chart library: this page runs inside a Bitrix24 slider, and
 * a charting dependency would buy little while costing bundle size and control over the
 * mark specification the palette validator was run against.
 *
 * What the specification pins down, and what is therefore not adjustable here:
 *
 *  * **One y-axis**, from zero, on a 1/2/5 x 10^k scale ({@link niceScale}).
 *  * **A 2px surface gap between stacked segments** and a **4px rounded top on the
 *    topmost segment only** - the rounded end says "the data stops here"; the squared
 *    bottom says "this is anchored to the baseline".
 *  * **Fixed series order and fixed hues** (`answered`, `no_answer`),
 *    assigned by outcome and not by size, so applying a filter never repaints the bars
 *    that survive it.
 *  * **A crosshair and a tooltip listing every series** for the hovered day - a stacked
 *    bar is unreadable above the bottom segment without one.
 *
 * The series are dense: a day with no calls is drawn as an empty slot, never skipped,
 * because a bar chart that silently drops days lies about the shape of a week.
 */

import { useTranslations } from 'next-intl';
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type RefObject,
} from 'react';

import { addDays, dayToUtc } from '@/components/Filters';
import { formatCount } from '@/lib/format';
import { SERIES_ORDER, niceScale, seriesVar, topRoundedPath, type ResultGroup } from '@/lib/viz';

/** One calendar day in the viewer's timezone, as `GET /api/v1/dashboard` returns it. */
export interface DayBucket {
  /** `YYYY-MM-DD`. */
  date: string;
  answered: number;
  no_answer: number;
}

export interface CallsPerDayChartProps {
  days: DayBucket[];
  /** Inclusive period ends, used to fill days the API had no rows for. */
  from: string;
  to: string;
  locale: string;
}

const HEIGHT = 224;
const PAD_TOP = 12;
const PAD_BOTTOM = 26;
const PAD_LEFT = 44;
const PAD_RIGHT = 10;

/** 2px, from the specification. */
const SEGMENT_GAP = 2;

/** Never let a day slot get so thin that the bar stops being a target. */
const MIN_BAR = 4;
const MAX_BAR = 28;

/** Guard against a pathological range; the API caps the period at 366 days anyway. */
const MAX_SLOTS = 400;

function total(bucket: DayBucket): number {
  return bucket.answered + bucket.no_answer;
}

/** Every day in `[from, to]`, with zeros where the API returned nothing. */
function densify(days: DayBucket[], from: string, to: string): DayBucket[] {
  const byDate = new Map<string, DayBucket>();
  for (const day of days) {
    byDate.set(day.date, day);
  }
  const start = dayToUtc(from);
  const end = dayToUtc(to);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) {
    return days;
  }
  const out: DayBucket[] = [];
  let cursor = from;
  for (let index = 0; index < MAX_SLOTS; index += 1) {
    out.push(byDate.get(cursor) ?? { date: cursor, answered: 0, no_answer: 0 });
    if (cursor === to) {
      break;
    }
    cursor = addDays(cursor, 1);
  }
  return out;
}

/** Container width in CSS pixels; the SVG uses a 1:1 viewBox so units are pixels. */
function useMeasuredWidth(): [RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const node = ref.current;
    if (!node) {
      return;
    }
    const apply = () => setWidth(Math.max(0, Math.round(node.getBoundingClientRect().width)));
    apply();
    const observer = new ResizeObserver(apply);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return [ref, width];
}

export function CallsPerDayChart({ days, from, to, locale }: CallsPerDayChartProps) {
  const t = useTranslations();
  const [ref, width] = useMeasuredWidth();
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  const series = useMemo(() => densify(days, from, to), [days, from, to]);

  const scale = useMemo(
    () => niceScale(series.reduce((max, day) => Math.max(max, total(day)), 0)),
    [series],
  );

  const dayLabel = useMemo(
    () => makeFormatter(locale, { day: 'numeric', month: 'short' }),
    [locale],
  );
  const fullLabel = useMemo(
    () => makeFormatter(locale, { weekday: 'short', day: 'numeric', month: 'short' }),
    [locale],
  );

  const innerWidth = Math.max(0, width - PAD_LEFT - PAD_RIGHT);
  const innerHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const band = series.length > 0 ? innerWidth / series.length : 0;
  const barWidth = Math.max(MIN_BAR, Math.min(MAX_BAR, band - 4));
  const baseline = PAD_TOP + innerHeight;

  const yFor = (value: number): number =>
    baseline - (Math.min(value, scale.max) / scale.max) * innerHeight;

  const labelEvery = Math.max(1, Math.ceil(series.length / 8));

  const onMove = (event: ReactMouseEvent<SVGRectElement>): void => {
    const node = svgRef.current;
    if (!node || band <= 0) {
      return;
    }
    const box = node.getBoundingClientRect();
    const index = Math.floor((event.clientX - box.left - PAD_LEFT) / band);
    setHover(index >= 0 && index < series.length ? index : null);
  };

  const hovered = hover === null ? null : (series[hover] ?? null);

  return (
    <div>
      <div className="ca-viz-plot" ref={ref}>
        {width > 0 ? (
          <svg
            ref={svgRef}
            width={width}
            height={HEIGHT}
            viewBox={`0 0 ${width} ${HEIGHT}`}
            role="img"
            aria-label={t('app.dashboard.perDay.title')}
            style={{ display: 'block' }}
          >
            {/* One y-axis: gridlines and their labels, nothing on the right. */}
            {scale.ticks.map((tick) => (
              <g key={tick}>
                <line
                  x1={PAD_LEFT}
                  x2={width - PAD_RIGHT}
                  y1={yFor(tick)}
                  y2={yFor(tick)}
                  stroke={tick === 0 ? 'var(--ca-viz-baseline)' : 'var(--ca-viz-grid)'}
                  strokeWidth={1}
                  shapeRendering="crispEdges"
                />
                <text
                  x={PAD_LEFT - 8}
                  y={yFor(tick) + 3.5}
                  textAnchor="end"
                  fontSize={11}
                  fill="var(--ca-viz-muted)"
                  className="ca-viz-num"
                >
                  {formatCount(tick, locale)}
                </text>
              </g>
            ))}

            {/* The hovered day, marked before the bars so the bars stay on top. */}
            {hover !== null ? (
              <>
                <rect
                  x={PAD_LEFT + hover * band}
                  y={PAD_TOP}
                  width={Math.max(band, 1)}
                  height={innerHeight}
                  fill="var(--ca-viz-grid)"
                  opacity={0.45}
                />
                <line
                  x1={PAD_LEFT + hover * band + band / 2}
                  x2={PAD_LEFT + hover * band + band / 2}
                  y1={PAD_TOP}
                  y2={baseline}
                  stroke="var(--ca-viz-baseline)"
                  strokeWidth={1}
                  strokeDasharray="3 3"
                  shapeRendering="crispEdges"
                />
              </>
            ) : null}

            {series.map((day, index) => {
              const x = PAD_LEFT + index * band + (band - barWidth) / 2;
              const stack = SERIES_ORDER.map((group) => ({ group, value: day[group] })).filter(
                (entry) => entry.value > 0,
              );
              let accumulated = 0;
              return (
                <g key={day.date}>
                  {stack.map((entry, position) => {
                    const bottom = yFor(accumulated);
                    accumulated += entry.value;
                    const top = yFor(accumulated);
                    // The gap is taken off the *bottom* of every segment above the
                    // first, so the stack keeps its total height and the topmost end
                    // still lands on the value it represents.
                    const height = Math.max(1, bottom - top - (position > 0 ? SEGMENT_GAP : 0));
                    const isTop = position === stack.length - 1;
                    return isTop ? (
                      <path
                        key={entry.group}
                        d={topRoundedPath(x, top, barWidth, height)}
                        fill={seriesVar(entry.group)}
                      />
                    ) : (
                      <rect
                        key={entry.group}
                        x={x}
                        y={top}
                        width={barWidth}
                        height={height}
                        fill={seriesVar(entry.group)}
                      />
                    );
                  })}
                  {index % labelEvery === 0 ? (
                    <text
                      x={PAD_LEFT + index * band + band / 2}
                      y={baseline + 15}
                      textAnchor="middle"
                      fontSize={11}
                      fill="var(--ca-viz-muted)"
                    >
                      {dayLabel(day.date)}
                    </text>
                  ) : null}
                </g>
              );
            })}

            {/* The hover layer, last so nothing above it swallows the pointer. */}
            <rect
              x={PAD_LEFT}
              y={PAD_TOP}
              width={Math.max(innerWidth, 1)}
              height={innerHeight}
              fill="transparent"
              onMouseMove={onMove}
              onMouseLeave={() => setHover(null)}
            />
          </svg>
        ) : (
          <div style={{ height: HEIGHT }} />
        )}

        {hovered && hover !== null ? (
          <div
            className="ca-viz-tooltip"
            style={{
              left: Math.min(
                Math.max(PAD_LEFT + hover * band + band / 2 - 78, 0),
                Math.max(width - 160, 0),
              ),
              top: 4,
              width: 156,
            }}
          >
            <div className="mb-1 font-medium">{fullLabel(hovered.date)}</div>
            {SERIES_ORDER.map((group) => (
              <div key={group} className="flex items-center gap-2">
                <span className="ca-viz-swatch" style={{ background: seriesVar(group) }} />
                <span className="ca-viz-ink-2 flex-1">{t(`app.dashboard.series.${group}`)}</span>
                <span className="ca-viz-num">{formatCount(hovered[group], locale)}</span>
              </div>
            ))}
            <div
              className="mt-1 flex items-center gap-2 pt-1"
              style={{ borderTop: '1px solid var(--ca-border)' }}
            >
              <span className="ca-viz-ink-2 flex-1">{t('app.dashboard.perDay.total')}</span>
              <span className="ca-viz-num font-medium">{formatCount(total(hovered), locale)}</span>
            </div>
          </div>
        ) : null}
      </div>

      <Legend />
    </div>
  );
}

/**
 * Two or more series means a legend, and the legend is never the only identity cue:
 * the tooltip repeats every label, and the call table below the page repeats the values.
 */
function Legend() {
  const t = useTranslations();
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2">
      {SERIES_ORDER.map((group: ResultGroup) => (
        <span key={group} className="flex items-center gap-2 text-[12px]">
          <span className="ca-viz-swatch" style={{ background: seriesVar(group) }} />
          <span style={{ color: 'var(--ca-viz-ink-2)' }}>{t(`app.dashboard.series.${group}`)}</span>
        </span>
      ))}
    </div>
  );
}

/**
 * A calendar-date formatter.
 *
 * The dates are already calendar days in the viewer's timezone, so they are read back at
 * **UTC midnight**: converting them a second time would slide every label by a day for
 * any viewer west of Greenwich.
 */
function makeFormatter(
  locale: string,
  options: Intl.DateTimeFormatOptions,
): (iso: string) => string {
  let format: Intl.DateTimeFormat;
  try {
    format = new Intl.DateTimeFormat(locale, { ...options, timeZone: 'UTC' });
  } catch {
    format = new Intl.DateTimeFormat(undefined, { ...options, timeZone: 'UTC' });
  }
  return (iso: string): string => {
    const value = dayToUtc(iso);
    return Number.isNaN(value) ? iso : format.format(new Date(value));
  };
}

export default CallsPerDayChart;
