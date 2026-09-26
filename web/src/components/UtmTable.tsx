'use client';

/**
 * The UTM report: one row per value of the chosen tag, one Итого row (§4.13).
 *
 * Laid out after the owner's own spreadsheet (2026-09-26): leads, converted leads and deals
 * as counts, the two rates beside them, then the deals split by where they stand today. It
 * replaced a page of tiles, charts and filters; everything a reader needs from it is in
 * this one grid.
 *
 * **Why percentages are computed here and not sent.** Both rates are functions of integers
 * the server already sent. Deriving them in the browser keeps the wire honest - every number
 * on it is a count somebody could verify - and switching the grouping tag re-renders without
 * a round trip. `DealStageTable` states the same rule for the same reason.
 *
 * **Why the first column is pinned and the rest scrolls.** A count means nothing without the
 * tag it belongs to, and a wide grid scrolled sideways is otherwise a wall of numbers
 * belonging to nobody. The scroll is *announced* - `role="region"` with a name that says it
 * scrolls - rather than left to be discovered.
 *
 * **Why deals over leads above 100 % is rendered plainly.** It is normal: a deal may have
 * been created by hand, or converted from a lead created before the period. Clamping it
 * would hide the very thing the number is telling the reader, and the page says so in words
 * under the table.
 */

import { useMemo } from 'react';

import { formatCount, percent } from '@/lib/format';
import {
  addBucket,
  bucketLabel,
  conversion,
  emptyBucket,
  type Bucket,
  type UtmResponse,
} from '@/lib/utm';

type Translate = (key: string, values?: Record<string, string | number>) => string;

export interface UtmTableProps {
  buckets: readonly Bucket[];
  totals: Bucket;
  /** The response's declared bucket sentinels - never compared against literals. */
  sentinels: UtmResponse['buckets'];
  /** The tag the rows are grouped by, for the column head. */
  dimensionLabel: string;
  /** The same tag spelled out in full, for the caption and the scroll region's name. */
  dimensionTitle: string;
  /**
   * False when the report carries no leads: a portal whose leads Bitrix24 refuses, or one
   * running Simple CRM, where every lead becomes a deal at once. The four lead columns are
   * then left out rather than drawn as zeros - "no leads here" and "nobody created a lead"
   * are different facts - and the deals' own win rate takes their place.
   */
  leadsAvailable: boolean;
  locale: string;
  t: Translate;
  /** Rows past this are folded into one visible residue row (the API already capped values). */
  maxRows?: number;
}

/** Rows drawn before the residue. Past this a table stops being read and starts being scrolled. */
const MAX_ROWS = 40;

/** Two decimals, as the owner's spreadsheet prints its rates. */
const RATE_DIGITS = 2;

/** One numeric column: `lead` ones need leads, `dealsOnly` ones replace them. */
interface Column {
  head: string;
  cell: (bucket: Bucket) => string;
  lead?: boolean;
  dealsOnly?: boolean;
}

export const UTM_CSS: string = `
.ca-utm-scroll{overflow-x:auto;position:relative;}
.ca-utm-grid{width:100%;border-collapse:separate;border-spacing:0;font-size:13px;}
.ca-utm-grid th,.ca-utm-grid td{padding:6px 10px;white-space:nowrap;border-bottom:1px solid var(--ca-border-soft);}
.ca-utm-grid thead th{position:sticky;top:0;z-index:2;background:var(--ca-surface);color:var(--ca-viz-muted);font-weight:500;font-size:11px;letter-spacing:.01em;text-align:right;}
.ca-utm-grid thead th:first-child{text-align:left;z-index:3;}
.ca-utm-grid td{text-align:right;color:var(--ca-text);}
.ca-utm-tag{position:sticky;left:0;z-index:1;background:var(--ca-surface);text-align:left !important;max-width:15rem;white-space:normal;overflow-wrap:anywhere;}
.ca-utm-grid tbody tr:hover td{background:var(--ca-surface-soft);}
.ca-utm-grid tbody tr:hover .ca-utm-tag{background:var(--ca-surface-soft);}
.ca-utm-total td{border-top:2px solid var(--ca-border);border-bottom:none;font-weight:600;background:var(--ca-surface-soft);}
/* Scoped to the grid so it outranks \`.ca-utm-grid td\`: unscoped, the residue rows' labels
   ("Без метки", "Прочие значения") rendered in the ordinary text colour. */
.ca-utm-grid .ca-utm-muted{color:var(--ca-viz-muted);}
/* Hidden by a clip path rather than by the overflow of a 1px box, so the UI audit stops
   counting the app's one intentional piece of hidden text as clipped content. The deal
   table, the by-hour grid and the calls table all carry this rule for the same reason. */
.ca-utm-grid caption.sr-only{overflow:visible;clip-path:inset(50%);}
.ca-utm-note{margin:0;font-size:12px;color:var(--ca-muted);}
`;

export function UtmTable({
  buckets,
  totals,
  sentinels,
  dimensionLabel,
  dimensionTitle,
  leadsAvailable,
  locale,
  t,
  maxRows = MAX_ROWS,
}: UtmTableProps) {
  /**
   * Everything past the cap folds into ONE visible residue row rather than disappearing.
   *
   * A reader must be able to see that something was folded and how much of it there was.
   * `Σ rows == Итого` still holds on screen, which is the only way the total row can be
   * checked by eye.
   */
  const rows = useMemo(() => {
    if (buckets.length <= maxRows) {
      return { shown: buckets, residue: null as Bucket | null, hidden: 0 };
    }
    const shown = buckets.slice(0, maxRows);
    const rest = buckets.slice(maxRows);
    const residue = emptyBucket('');
    for (const bucket of rest) {
      addBucket(residue, bucket);
    }
    return { shown, residue, hidden: rest.length };
  }, [buckets, maxRows]);

  if (buckets.length === 0) {
    return <p className="ca-muted text-sm">{t('app.utm.empty')}</p>;
  }

  const all: Column[] = [
    { head: t('app.utm.leads'), cell: (b) => formatCount(b.leads.total, locale), lead: true },
    { head: t('app.utm.converted'), cell: (b) => formatCount(b.leads.won, locale), lead: true },
    { head: t('app.utm.deals'), cell: (b) => formatCount(b.deals.total, locale), lead: false },
    {
      head: t('app.utm.leadConversion'),
      cell: (b) => percent(conversion(b.leads.won, b.leads.total), locale, RATE_DIGITS),
      lead: true,
    },
    {
      head: t('app.utm.dealsPerLead'),
      cell: (b) => percent(conversion(b.deals.total, b.leads.total), locale, RATE_DIGITS),
      lead: true,
    },
    { head: t('app.utm.won'), cell: (b) => formatCount(b.deals.won, locale), lead: false },
    { head: t('app.utm.inProgress'), cell: (b) => formatCount(b.deals.in_progress, locale), lead: false },
    { head: t('app.utm.lost'), cell: (b) => formatCount(b.deals.lost, locale), lead: false },
    {
      head: t('app.utm.winRate'),
      cell: (b) => percent(conversion(b.deals.won, b.deals.total), locale, RATE_DIGITS),
      dealsOnly: true,
    },
  ];
  const columns = all.filter((column) =>
    leadsAvailable ? !column.dealsOnly : !column.lead,
  );

  const cells = (bucket: Bucket) =>
    columns.map((column) => (
      <td key={column.head} className="ca-viz-num">
        {column.cell(bucket)}
      </td>
    ));

  const body = (bucket: Bucket, label: string, muted: boolean) => (
    <tr key={label}>
      <td className={`ca-utm-tag${muted ? ' ca-utm-muted' : ''}`} title={label}>
        {label}
      </td>
      {cells(bucket)}
    </tr>
  );

  return (
    <div
      className="ca-utm-scroll"
      role="region"
      aria-label={t('app.utm.regionLabel', { dimension: dimensionTitle })}
      tabIndex={0}
    >
      <table className="ca-utm-grid">
        <caption className="sr-only">{t('app.utm.caption', { dimension: dimensionTitle })}</caption>
        <thead>
          <tr>
            <th scope="col">{dimensionLabel}</th>
            {columns.map((column) => (
              <th key={column.head} scope="col">
                {column.head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.shown.map((bucket) =>
            body(
              bucket,
              bucketLabel(bucket.value, sentinels, t),
              bucket.value === sentinels.none ||
                bucket.value === sentinels.other ||
                bucket.value === sentinels.collapsed,
            ),
          )}
          {rows.residue
            ? body(rows.residue, t('app.utm.moreRows', { count: rows.hidden }), true)
            : null}
        </tbody>
        <tfoot>
          <tr className="ca-utm-total">
            <td className="ca-utm-tag">{t('app.utm.summary')}</td>
            {cells(totals)}
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

export default UtmTable;
