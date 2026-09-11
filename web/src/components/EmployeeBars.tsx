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
 *
 * Two layout rules, both of them about the panel this sits in rather than about bars:
 *
 *  * **The name column is a share of the panel, not 168 fixed pixels.** Beside a heatmap
 *    at 375 that fixed column left the bars 74px to say everything in - the marks were
 *    the smallest thing in a chart made of marks.
 *  * **The name wraps; it is never truncated with an ellipsis.** A clipped name is the
 *    one thing in this panel a reader cannot recover by looking harder, and the row it
 *    belongs to is exactly the row they were looking for. Rows carry a minimum height
 *    instead, so the rhythm holds whether a name takes one line or two.
 */

import { useTranslations } from 'next-intl';
import { useMemo, useState, type MouseEvent as ReactMouseEvent } from 'react';

import { formatCount, withExtension } from '@/lib/format';
import { SERIES_ORDER } from '@/lib/viz';

/** One row of the per-employee breakdown from `GET /api/v1/dashboard`. */
export interface EmployeeBucket {
  /** `calls.portal_user_id`; `null` for the aggregated "Other" bucket. */
  employee_id: number | null;
  name?: string | null;
  /** §7: the internal extension, rendered in parentheses after the name. */
  phone_inner?: string | null;
  /** Dismissed users keep their calls (§7) and are shown, marked, never hidden. */
  active?: boolean | null;
  /** True on the server-side "everyone past the top N" row. */
  other?: boolean | null;
  /**
   * True on the bucket `stats.py` emits for calls with no `portal_user_id` (§3 allows
   * NULL). It is not an employee and not the "Other" residue: it is every call that
   * belongs to nobody, kept so the axes add up.
   */
  unassigned?: boolean | null;
  total: number;
  answered: number;
  no_answer: number;
}

export interface EmployeeBarsProps {
  employees: EmployeeBucket[];
  locale: string;
}

/** The specification's cap: eight named employees, then one "Other". */
const MAX_NAMED = 8;

const BAR_HEIGHT = 14;

/** One row of the rhythm: 14px of mark inside a 24px slot, on the 4px scale. */
const ROW_HEIGHT = 24;

/** The tooltip's fixed width, so it can be kept inside the panel it is drawn over. */
const TOOLTIP_W = 156;

interface HoverState {
  index: number;
  /** Already clamped into the panel, so the tooltip can never leave the viewport. */
  left: number;
  top: number;
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
    // §7: the extension beside the name, through the same helper the filter and the call
    // table use - this chart sits directly under that filter, and a person who reads
    // "Азиз Каримов (101)" in one and "Азиз Каримов" in the other has to check whether
    // they are the same row.
    const name = withExtension(row.name, row.phone_inner);
    if (name) {
      return name;
    }
    // A row with no `portal_user_id` is nobody's call (§3 allows NULL, and `stats.py`
    // flags the bucket rather than dropping it). Naming it "User #0" invents a person:
    // 0 is a legal Bitrix24 id, so the label is unactionable AND collides with a real
    // employee. `lib/calls.ts` draws an em dash for exactly these rows in the table; the
    // chart needs a word, so it gets its own. Never coerce a missing id to a number.
    if (row.unassigned || row.employee_id === null || row.employee_id === undefined) {
      return t('app.dashboard.employees.unassigned');
    }
    return t('app.dashboard.filter.unknownEmployee', { id: String(row.employee_id) });
  };

  const onMove = (event: ReactMouseEvent<HTMLDivElement>, index: number): void => {
    const host = event.currentTarget.offsetParent as HTMLElement | null;
    const box = event.currentTarget.getBoundingClientRect();
    const hostBox = host?.getBoundingClientRect();
    const pointer = event.clientX - (hostBox?.left ?? 0);
    const room = (hostBox?.width ?? 0) - TOOLTIP_W;
    setHover({
      index,
      // Clamped both ways: a tooltip that hangs off the right of a 375px panel is a
      // second horizontal overflow, and this one appears under the reader's own cursor.
      left: Math.max(0, Math.min(pointer - TOOLTIP_W / 2, room)),
      top: Math.max(0, box.top - (hostBox?.top ?? 0) - 74),
    });
  };

  const hovered = hover === null ? null : (rows[hover.index] ?? null);

  return (
    <div className="ca-viz-plot">
      <div className="flex flex-col gap-1">
        {rows.map((row, index) => {
          const share = max > 0 ? row.total / max : 0;
          return (
            <div
              // The no-employee bucket gets a name of its own here too: keyed by position
              // it borrowed the identity of whichever row happened to sort into its place.
              key={
                row.other
                  ? 'other'
                  : row.employee_id === null || row.employee_id === undefined
                    ? 'unassigned'
                    : String(row.employee_id)
              }
              className="grid items-center gap-x-2"
              style={{
                // As much as the longest name needs and not one pixel more, capped at
                // 38% of the panel. A fixed 168px column wasted half of itself on a wide
                // slider and still wrapped "Дилноза Юсупова" at 375; `fit-content` is the
                // one track sizing that gets both of those right without measuring.
                // `overflow-wrap: anywhere` below keeps the min-content floor at one
                // character, so an unbreakable name cannot push the track past the cap.
                gridTemplateColumns: 'fit-content(38%) minmax(0, 1fr) auto',
                minHeight: ROW_HEIGHT,
              }}
              onMouseMove={(event) => onMove(event, index)}
              onMouseLeave={() => setHover(null)}
            >
              <span
                className="text-[13px] leading-tight"
                style={{
                  color: row.other ? 'var(--ca-viz-muted)' : 'var(--ca-viz-ink)',
                  overflowWrap: 'anywhere',
                }}
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
                className="ca-viz-num text-[13px] leading-tight"
                style={{ color: 'var(--ca-viz-ink)', minWidth: 36, textAlign: 'right' }}
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
          style={{ left: hover.left, top: hover.top, width: TOOLTIP_W }}
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
