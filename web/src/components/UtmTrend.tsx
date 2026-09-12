'use client';

/**
 * Leads and deals per day across the scanned period (§4.13).
 *
 * Hand-rolled inline SVG, no chart library: this page runs inside a Bitrix24 slider, and a
 * charting dependency would buy little while costing bundle size and control over the mark
 * specification the palette validator was run against. The rules `CallsPerDayChart` pins
 * down apply here unchanged - one y-axis from zero on a 1/2/5 x 10^k scale, a 4px rounded
 * top, fixed hues assigned by SERIES and never by size, and a dense series in which a day
 * with nothing on it is an empty slot rather than a skipped one.
 *
 * ---------------------------------------------------------------------------------
 * **One honest limitation, stated on the page as well as here.**
 *
 * This panel draws the whole SELECTION - the period and the employee filter - and not the
 * tag filters. Those are applied in the browser against combination rows that carry no date,
 * and inventing a per-tag-per-day series would multiply the response by the length of the
 * period; on a portal with per-click `utm_term` that is tens of thousands of rows sent to a
 * phone. So the trend answers "what arrived, and when", the rest of the page answers "from
 * where", and the caption says which is which rather than leaving a reader to discover that
 * one panel ignores their filter.
 * ---------------------------------------------------------------------------------
 *
 * Grouped bars rather than stacked: a deal is not a subset of a lead on the same day (it may
 * have been converted from a lead created months earlier), so stacking them would draw a
 * total that means nothing.
 */

import { useMemo, useState, type MouseEvent as ReactMouseEvent } from 'react';

import { formatCount } from '@/lib/format';
import { ENTITY_VAR, type UtmDay } from '@/lib/utm';
import { niceScale, topRoundedPath } from '@/lib/viz';

type Translate = (key: string, values?: Record<string, string | number>) => string;

export interface UtmTrendProps {
  days: readonly UtmDay[];
  locale: string;
  t: Translate;
}

const HEIGHT = 168;
const PAD_TOP = 10;
const PAD_BOTTOM = 22;
const PAD_LEFT = 40;
const PAD_RIGHT = 8;
/** Two bars per day plus a hair of air between the pairs. */
const PAIR_GAP = 1;

export function UtmTrend({ days, locale, t }: UtmTrendProps) {
  const [hover, setHover] = useState<number | null>(null);

  const scale = useMemo(
    () => niceScale(days.reduce((max, day) => Math.max(max, day.leads, day.deals), 0)),
    [days],
  );

  if (days.length === 0) {
    return <p className="ca-muted text-sm">{t('app.utm.empty')}</p>;
  }

  const width = 720;
  const plotW = width - PAD_LEFT - PAD_RIGHT;
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const slot = plotW / days.length;
  const bar = Math.max(1.5, (slot - PAIR_GAP * 2) / 2);
  const y = (value: number): number =>
    PAD_TOP + plotH - (scale.max > 0 ? (value / scale.max) * plotH : 0);

  const onMove = (event: ReactMouseEvent<SVGSVGElement>): void => {
    const box = event.currentTarget.getBoundingClientRect();
    const x = ((event.clientX - box.left) / box.width) * width - PAD_LEFT;
    const index = Math.floor(x / slot);
    setHover(index >= 0 && index < days.length ? index : null);
  };

  const hovered = hover === null ? null : (days[hover] ?? null);

  return (
    <div className="ca-viz-plot">
      <div className="mb-2 flex flex-wrap items-center gap-3 ca-viz-label">
        {(['lead', 'deal'] as const).map((entity) => (
          <span key={entity} className="inline-flex items-center gap-1.5">
            <span className="ca-viz-swatch" style={{ background: ENTITY_VAR[entity] }} />
            {t(entity === 'lead' ? 'app.utm.leads' : 'app.utm.deals')}
          </span>
        ))}
      </div>

      <svg
        viewBox={`0 0 ${width} ${HEIGHT}`}
        width="100%"
        height={HEIGHT}
        role="img"
        aria-label={t('app.utm.trendLabel')}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        {scale.ticks.map((tick) => (
          <g key={tick}>
            <line
              x1={PAD_LEFT}
              x2={width - PAD_RIGHT}
              y1={y(tick)}
              y2={y(tick)}
              stroke={tick === 0 ? 'var(--ca-viz-baseline)' : 'var(--ca-viz-grid)'}
              strokeWidth={1}
            />
            <text
              x={PAD_LEFT - 6}
              y={y(tick) + 3}
              textAnchor="end"
              fontSize={10}
              fill="var(--ca-viz-muted)"
              className="ca-viz-num"
            >
              {formatCount(tick, locale)}
            </text>
          </g>
        ))}

        {days.map((day, index) => {
          const left = PAD_LEFT + index * slot + PAIR_GAP / 2;
          const dim = hover !== null && hover !== index;
          return (
            <g key={day.date} opacity={dim ? 0.55 : 1}>
              {day.leads > 0 ? (
                <path
                  d={topRoundedPath(left, y(day.leads), bar, PAD_TOP + plotH - y(day.leads))}
                  fill={ENTITY_VAR.lead}
                />
              ) : null}
              {day.deals > 0 ? (
                <path
                  d={topRoundedPath(
                    left + bar + PAIR_GAP,
                    y(day.deals),
                    bar,
                    PAD_TOP + plotH - y(day.deals),
                  )}
                  fill={ENTITY_VAR.deal}
                />
              ) : null}
            </g>
          );
        })}

        {/* First and last day only: a month of tick labels at this width is a smear. */}
        {[0, days.length - 1].map((index) => {
          const day = days[index];
          return day === undefined ? null : (
            <text
              key={`x:${day.date}`}
              x={index === 0 ? PAD_LEFT : width - PAD_RIGHT}
              y={HEIGHT - 6}
              textAnchor={index === 0 ? 'start' : 'end'}
              fontSize={10}
              fill="var(--ca-viz-muted)"
            >
              {day.date.slice(5)}
            </text>
          );
        })}
      </svg>

      {hovered ? (
        <div
          className="ca-viz-tooltip"
          style={{
            left: `${Math.min(Math.max(((hover ?? 0) / days.length) * 100, 0), 78)}%`,
            top: 8,
          }}
        >
          <div className="font-medium">{hovered.date}</div>
          <div className="ca-viz-num">
            {t('app.utm.leads')}: {formatCount(hovered.leads, locale)}
          </div>
          <div className="ca-viz-num">
            {t('app.utm.deals')}: {formatCount(hovered.deals, locale)}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default UtmTrend;
