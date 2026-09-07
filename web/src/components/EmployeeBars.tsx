'use client';

/**
 * The per-employee comparison (§4.11 "Dashboard").
 *
 * Horizontal bars, one hue, sorted descending, at most eight rows plus the API's
 * "Other" bucket. Three consequences of the chart specification are visible here:
 *
 *  * **No legend.** One series does not need one, and a legend for a single colour only
 *    teaches the reader that the colour means nothing.
 *  * **A ninth employee folds into "Other", never a generated hue.** Cycling a
 *    categorical palette past its validated length is how two people end up wearing the
 *    same colour on one screen.
 *  * **The name sits outside the bar and the value at its end.** A label inside a bar
 *    is unreadable as soon as the bar is short, which is exactly the row a reader is
 *    looking for.
 *
 * Bars are laid out with CSS grid rather than SVG: they are axis-aligned rectangles with
 * text beside them, so the grid gives the same marks with real text wrapping and no
 * measurement pass.
 */

import { useTranslations } from 'next-intl';
import { useMemo, useState, type MouseEvent as ReactMouseEvent } from 'react';

import { formatCount } from '@/lib/format';
import { SERIES_ORDER } from '@/lib/viz';

/** One row of the per-employee breakdown from `GET /api/v1/dashboard`. */
export interface EmployeeBucket {
  /** `calls.portal_user_id`; `null` for the aggregated "Other" bucket. */
  employee_id: number | null;
  name?: string | null;
  /** Dismissed users keep their calls (§7) and are shown, marked, never hidden. */
  active?: boolean | null;
  /** True on the server-side "everyone past the top N" row. */
  other?: boolean | null;
  total: number;
  answered: number;
  missed: number;
  not_connected: number;
}

export interface EmployeeBarsProps {
  employees: EmployeeBucket[];
  locale: string;
}

/** The specification's cap: eight named employees, then one "Other". */
const MAX_NAMED = 8;

const BAR_HEIGHT = 14;

interface HoverState {
  index: number;
  x: number;
  y: number;
}

export function EmployeeBars({ employees, locale }: EmployeeBarsProps) {
  const t = useTranslations();
  const [hover, setHover] = useState<HoverState | null>(null);

  /**
   * Sorted descending, with "Other" pinned last however large it is: it is a residue,
   * not a competitor, and letting it outrank a person would be a lie about a person.
   */
  const rows = useMemo(() => {
    const named = employees.filter((row) => !row.other).slice(0, MAX_NAMED);
    const others = employees.filter((row) => row.other);
    named.sort((a, b) => b.total - a.total);
    return [...named, ...others];
  }, [employees]);

  const max = rows.reduce((peak, row) => Math.max(peak, row.total), 0);

  const label = (row: EmployeeBucket): string => {
    if (row.other) {
      return t('app.dashboard.employees.other');
    }
    const name = row.name?.trim();
    if (name) {
      return name;
    }
    return t('app.dashboard.filter.unknownEmployee', { id: String(row.employee_id ?? 0) });
  };

  const onMove = (event: ReactMouseEvent<HTMLDivElement>, index: number): void => {
    const host = event.currentTarget.offsetParent as HTMLElement | null;
    const box = event.currentTarget.getBoundingClientRect();
    const hostBox = host?.getBoundingClientRect();
    setHover({
      index,
      x: event.clientX - (hostBox?.left ?? 0),
      y: box.top - (hostBox?.top ?? 0),
    });
  };

  const hovered = hover === null ? null : (rows[hover.index] ?? null);

  return (
    <div className="ca-viz-plot">
      <div className="flex flex-col gap-2">
        {rows.map((row, index) => {
          const share = max > 0 ? row.total / max : 0;
          return (
            <div
              key={row.other ? 'other' : String(row.employee_id ?? `row-${index}`)}
              className="grid items-center gap-3"
              style={{ gridTemplateColumns: 'minmax(88px, 168px) 1fr auto' }}
              onMouseMove={(event) => onMove(event, index)}
              onMouseLeave={() => setHover(null)}
            >
              <span
                className="truncate text-[13px]"
                style={{ color: row.other ? 'var(--ca-viz-muted)' : 'var(--ca-viz-ink)' }}
                title={label(row)}
              >
                {label(row)}
                {row.active === false ? (
                  <span className="ca-muted"> · {t('app.dashboard.filter.dismissed')}</span>
                ) : null}
              </span>

              {/* The track is the surface itself: an empty rail behind every bar would
                  read as a target the employee failed to reach. */}
              <span
                className="block"
                style={{ height: BAR_HEIGHT, minHeight: BAR_HEIGHT, position: 'relative' }}
              >
                <span
                  className="block"
                  style={{
                    width: `${Math.max(share * 100, row.total > 0 ? 1.5 : 0)}%`,
                    height: BAR_HEIGHT,
                    background: 'var(--ca-viz-bar)',
                    // Anchored square to the axis, rounded only at the data end.
                    borderRadius: '0 4px 4px 0',
                    opacity: row.other ? 0.55 : 1,
                  }}
                />
              </span>

              <span
                className="ca-viz-num text-[13px]"
                style={{ color: 'var(--ca-viz-ink)', minWidth: 40, textAlign: 'right' }}
              >
                {formatCount(row.total, locale)}
              </span>
            </div>
          );
        })}
      </div>

      {hovered && hover ? (
        <div
          className="ca-viz-tooltip"
          style={{ left: Math.max(0, hover.x - 78), top: Math.max(0, hover.y - 74), width: 156 }}
        >
          <div className="mb-1 font-medium">{label(hovered)}</div>
          {SERIES_ORDER.map((group) => (
            <div key={group} className="flex items-center gap-2">
              <span className="ca-viz-ink-2 flex-1">{t(`app.dashboard.series.${group}`)}</span>
              <span className="ca-viz-num">{formatCount(hovered[group], locale)}</span>
            </div>
          ))}
          <div
            className="mt-1 flex items-center gap-2 pt-1"
            style={{ borderTop: '1px solid var(--ca-border)' }}
          >
            <span className="ca-viz-ink-2 flex-1">{t('app.dashboard.perDay.total')}</span>
            <span className="ca-viz-num font-medium">{formatCount(hovered.total, locale)}</span>
          </div>
        </div>
      ) : null}

      {rows.some((row) => row.other) ? (
        <p className="ca-muted mt-3 text-[12px]">
          {t('app.dashboard.employees.otherHint', { count: MAX_NAMED })}
        </p>
      ) : null}
    </div>
  );
}

export default EmployeeBars;
