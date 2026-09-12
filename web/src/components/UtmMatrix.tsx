'use client';

/**
 * Source x medium: where the volume actually sits (§4.13).
 *
 * A CSS grid on one sequential blue ramp, lightest at zero. Never a rainbow and never two
 * hues: the question this answers is "which combination brings the most", which is one
 * ordered quantity, and a diverging or categorical palette would invent a midpoint the data
 * does not have. This is `RAMP` used for exactly what `RAMP` is for, and it is the reason
 * this page needs no new colour to add a fifth visual.
 *
 * Two details that are requirements rather than taste, both inherited from
 * `HourWeekdayHeatmap`:
 *
 *  * **Every cell carries its exact number.** A colour ramp is a RANK cue, not a value cue;
 *    without the number the grid can only ever be read as "darker".
 *  * **The grid is fluid and only then scrollable.** Columns shrink with the panel until a
 *    cell reaches the specification's hit-target floor; below that - narrower than any phone
 *    we ship to - the region scrolls and SAYS it scrolls, with the row labels pinned so a
 *    scrolled grid still has a y axis.
 *
 * Both axes are ranked by volume and capped, so the grid has a stable shape: a heatmap whose
 * rows reorder as a filter changes is unreadable. What falls past the cap is not drawn here
 * at all - the table below is where every row is accounted for, and this panel says so.
 */

import { useState } from 'react';

import { formatCount } from '@/lib/format';
import { rampIndex, rampVar } from '@/lib/viz';
import { bucketLabel, type Matrix, type UtmResponse } from '@/lib/utm';

type Translate = (key: string, values?: Record<string, string | number>) => string;

export interface UtmMatrixProps {
  matrix: Matrix;
  sentinels: UtmResponse['buckets'];
  rowLabel: string;
  columnLabel: string;
  locale: string;
  t: Translate;
}

/** The floor a cell may shrink to before the region scrolls instead. */
const CELL_MIN = 34;

export const MATRIX_CSS: string = `
.ca-utm-matrix{display:grid;gap:2px;align-items:stretch;}
.ca-utm-matrix .ca-utm-head{font-size:11px;color:var(--ca-viz-muted);align-self:end;overflow-wrap:anywhere;line-height:1.2;}
.ca-utm-matrix .ca-utm-row{font-size:12px;color:var(--ca-viz-ink);position:sticky;left:0;background:var(--ca-surface);padding-right:6px;overflow-wrap:anywhere;line-height:1.2;align-self:center;}
.ca-utm-matrix .ca-utm-box{min-height:26px;display:flex;align-items:center;justify-content:center;font-size:11px;}
.ca-utm-wrap{overflow-x:auto;}
`;

export function UtmMatrix({
  matrix,
  sentinels,
  rowLabel,
  columnLabel,
  locale,
  t,
}: UtmMatrixProps) {
  const [hover, setHover] = useState<string | null>(null);

  if (matrix.rows.length === 0 || matrix.columns.length === 0) {
    return <p className="ca-muted text-sm">{t('app.utm.empty')}</p>;
  }

  return (
    <div
      className="ca-utm-wrap"
      role="region"
      aria-label={t('app.utm.matrixRegion', { rows: rowLabel, columns: columnLabel })}
      tabIndex={0}
    >
      <div
        className="ca-utm-matrix"
        style={{
          // `fit-content()` is NOT a legal `minmax()` argument - a browser drops the whole
          // declaration and the grid silently collapses to ONE column, which is a defect a
          // screenshot shows and a type checker never will. `EmployeeBars` uses the same
          // sizing as its own track, which is where it is valid.
          gridTemplateColumns: `fit-content(26%) repeat(${matrix.columns.length}, minmax(${CELL_MIN}px, 1fr))`,
        }}
      >
        <span className="ca-utm-head" aria-hidden />
        {matrix.columns.map((column) => (
          <span key={`h:${column}`} className="ca-utm-head" title={bucketLabel(column, sentinels, t)}>
            {bucketLabel(column, sentinels, t)}
          </span>
        ))}

        {matrix.rows.map((row) => (
          <Row
            key={`r:${row}`}
            row={row}
            matrix={matrix}
            sentinels={sentinels}
            locale={locale}
            t={t}
            hover={hover}
            setHover={setHover}
          />
        ))}
      </div>
    </div>
  );
}

function Row({
  row,
  matrix,
  sentinels,
  locale,
  t,
  hover,
  setHover,
}: {
  row: string;
  matrix: Matrix;
  sentinels: UtmResponse['buckets'];
  locale: string;
  t: Translate;
  hover: string | null;
  setHover: (key: string | null) => void;
}) {
  const label = bucketLabel(row, sentinels, t);
  return (
    <>
      <span className="ca-utm-row" title={label}>
        {label}
      </span>
      {matrix.columns.map((column) => {
        const value = matrix.cells[row]?.[column] ?? 0;
        const key = `${row}::${column}`;
        // Index 0 is "near zero"; an empty cell gets the surface rather than the lightest
        // step, so "nothing here" and "almost nothing here" do not look the same.
        const paint = value > 0 ? rampVar(rampIndex(value, matrix.max)) : 'var(--ca-surface-soft)';
        return (
          <span
            key={key}
            className="ca-viz-cell ca-utm-box"
            style={{
              background: paint,
              color: value > 0 && rampIndex(value, matrix.max) > 6 ? '#fff' : 'var(--ca-viz-ink)',
              outline: hover === key ? '1px solid var(--ca-viz-ink-2)' : undefined,
            }}
            onMouseEnter={() => setHover(key)}
            onMouseLeave={() => setHover(null)}
            title={t('app.utm.matrixCell', {
              row: label,
              column: bucketLabel(column, sentinels, t),
              count: value,
            })}
          >
            <span className="ca-viz-num">{value > 0 ? formatCount(value, locale) : ''}</span>
          </span>
        );
      })}
    </>
  );
}

export default UtmMatrix;
