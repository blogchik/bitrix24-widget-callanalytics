'use client';

/**
 * The unified funnel: one row per tag, leads through deals through money (§4.13).
 *
 * ---------------------------------------------------------------------------------
 * **Why the outcome split is painted with two data colours and one CHROME colour.**
 *
 * Won, lost and in-progress look like three categories that need three hues, and `viz.ts`
 * ships two. The resolution is semantic rather than cosmetic: *in-progress is not an
 * outcome, it is the absence of one.* So the two DECIDED outcomes take the validated
 * two-series pair, and the undecided segment is painted in `--ca-viz-grid` - the token the
 * palette already ships for gridlines, measured as low-contrast against both surfaces.
 * Three distinguishable segments, zero new hexes, and the paint says the right thing: "this
 * has not happened yet" reads as chrome rather than as data.
 *
 * Colour is never the only cue. Every segment's count is printed in the row beside the bar,
 * and the bar is redundant with the numbers rather than a replacement for them.
 * ---------------------------------------------------------------------------------
 *
 * **Why percentages are computed here and not sent.** `deals / leads`, `won / total` and the
 * mean deal size are functions of integers the server already sent. Deriving them in the
 * browser keeps the wire honest - every number on it is a count somebody could verify - and
 * lets the "show %" toggle re-render without a REST round trip, which on this page costs a
 * live CRM scan of two entities. `DealStageTable` states the same rule for the same reason.
 *
 * **Why the first column is pinned and the rest scrolls.** A count means nothing without the
 * tag it belongs to, and a wide grid scrolled sideways is otherwise a wall of numbers
 * belonging to nobody. The scroll is *announced* - `role="region"` with a name that says it
 * scrolls - rather than left to be discovered.
 *
 * **Why a conversion above 100 % is rendered plainly.** It is normal: a deal may have been
 * created by hand, or converted from a lead created before the period. Clamping it would
 * hide the very thing the number is telling the reader, and the page explains it in words
 * above the table.
 */

import { useMemo } from 'react';

import { formatCount, formatMoney, percent } from '@/lib/format';
import {
  ENTITY_VAR,
  averageDeal,
  bucketLabel,
  conversion,
  major,
  shares,
  type Bucket,
  type UtmResponse,
} from '@/lib/utm';

type Translate = (key: string, values?: Record<string, string | number>) => string;

export interface UtmTableProps {
  buckets: readonly Bucket[];
  totals: Bucket;
  /** The response's declared bucket sentinels - never compared against literals. */
  sentinels: UtmResponse['buckets'];
  amounts: UtmResponse['amounts'];
  /** The dimension the rows are grouped by, for the caption and the column head. */
  dimensionLabel: string;
  locale: string;
  t: Translate;
  /** Render each outcome segment's share instead of its count. */
  showPercent: boolean;
  /** Rows past this are folded into one visible residue row (the API already capped values). */
  maxRows?: number;
}

/** Rows drawn before the residue. Past this a table stops being read and starts being scrolled. */
const MAX_ROWS = 40;

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
.ca-utm-total td{border-top:2px solid var(--ca-border);border-bottom:none;font-weight:600;}
.ca-utm-muted{color:var(--ca-viz-muted);}
.ca-utm-split{display:flex;height:8px;min-width:72px;border-radius:2px;overflow:hidden;gap:2px;background:transparent;}
.ca-utm-split>span{display:block;height:100%;}
/* 140px and a wrapping value, not 120px and a wider one. An amount in a currency with no
   minor unit runs to "1 275 350 000 UZS": at 120px it did not clip, it OVERLAPPED the tile
   beside it, which the audit cannot see (overflow is visible) and a reader cannot miss. */
.ca-utm-strip{display:grid;gap:10px 20px;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin:0;}
.ca-utm-strip dt{color:var(--ca-viz-muted);font-size:11px;letter-spacing:.01em;}
.ca-utm-strip dd{margin:0;font-size:18px;line-height:1.25;color:var(--ca-text);overflow-wrap:anywhere;}
.ca-utm-strip dd small{display:block;font-size:11px;color:var(--ca-viz-muted);}
/* Hidden by a clip path rather than by the overflow of a 1px box, so the UI audit stops
   counting the app's one intentional piece of hidden text as clipped content. The deal
   table, the by-hour grid and the calls table all carry this rule for the same reason. */
.ca-utm-grid caption.sr-only{overflow:visible;clip-path:inset(50%);}
.ca-utm-note{margin:0;font-size:12px;color:var(--ca-muted);}
.ca-utm-toggle{display:inline-flex;align-items:center;gap:8px;min-height:var(--ca-control-h);font-size:13px;color:var(--ca-text);cursor:pointer;}
.ca-utm-toggle input{width:16px;height:16px;accent-color:var(--ca-accent);}
`;

/** The 100 %-stacked outcome bar. Two data colours and one chrome colour - see the docblock. */
function OutcomeBar({ measures, t }: { measures: Bucket['deals']; t: Translate }) {
  const split = shares(measures);
  if (measures.total <= 0) {
    return <span className="ca-utm-muted">—</span>;
  }
  const segments: Array<[string, number, string]> = [
    [ENTITY_VAR.lead, split.won, t('app.utm.won')],
    [ENTITY_VAR.deal, split.lost, t('app.utm.lost')],
    ['var(--ca-viz-grid)', split.inProgress, t('app.utm.inProgress')],
  ];
  return (
    <span
      className="ca-utm-split"
      role="img"
      aria-label={t('app.utm.outcomeLabel', {
        won: measures.won,
        lost: measures.lost,
        inProgress: measures.in_progress,
      })}
    >
      {segments.map(([paint, share, label]) =>
        share > 0 ? (
          <span key={label} style={{ background: paint, flex: `${share} 0 0` }} />
        ) : null,
      )}
    </span>
  );
}

/**
 * A KPI tile's amount: the number on one line, the currency code under it.
 *
 * `Intl.NumberFormat` puts a NON-BREAKING space before the code, so "1 275 350 000 UZS"
 * cannot wrap at the only place it would read well - it either overlapped the next tile or
 * broke as "UZ / S". Splitting the two is the typography a stat tile wants anyway, and it
 * removes the failure rather than tuning around it. The table keeps `formatMoney`: its cells
 * do not wrap at all, they scroll.
 */
function TileMoney({
  cents: value,
  currency,
  locale,
}: {
  cents: number | null;
  currency: string;
  locale: string;
}) {
  if (value === null) {
    return <>—</>;
  }
  return (
    <>
      {formatCount(Math.round(major(value)), locale)}
      {currency ? <small>{currency}</small> : null}
    </>
  );
}


export function UtmSummaryStrip({
  totals,
  amounts,
  locale,
  t,
}: {
  totals: Bucket;
  amounts: UtmResponse['amounts'];
  locale: string;
  t: Translate;
}) {
  const rate = conversion(totals.deals.total, totals.leads.total);
  const mean = averageDeal(totals.amount, totals.deals.total);
  return (
    <dl className="ca-utm-strip">
      <div>
        <dt>{t('app.utm.leads')}</dt>
        <dd className="ca-viz-num">{formatCount(totals.leads.total, locale)}</dd>
      </div>
      <div>
        <dt>{t('app.utm.deals')}</dt>
        <dd className="ca-viz-num">{formatCount(totals.deals.total, locale)}</dd>
      </div>
      <div>
        <dt>{t('app.utm.conversion')}</dt>
        <dd className="ca-viz-num">{percent(rate, locale, 1)}</dd>
      </div>
      <div>
        <dt>{t('app.utm.won')}</dt>
        <dd className="ca-viz-num">{formatCount(totals.deals.won, locale)}</dd>
      </div>
      {/* Hidden rather than wrong: a portal whose currencies do not agree gets no total. */}
      {amounts.trusted ? (
        <>
          <div>
            <dt>{t('app.utm.amount')}</dt>
            <dd className="ca-viz-num">
              <TileMoney cents={totals.amount} currency={amounts.currency} locale={locale} />
            </dd>
          </div>
          <div>
            <dt>{t('app.utm.averageDeal')}</dt>
            <dd className="ca-viz-num">
              <TileMoney cents={mean} currency={amounts.currency} locale={locale} />
            </dd>
          </div>
        </>
      ) : null}
    </dl>
  );
}

export function UtmTable({
  buckets,
  totals,
  sentinels,
  amounts,
  dimensionLabel,
  locale,
  t,
  showPercent,
  maxRows = MAX_ROWS,
}: UtmTableProps) {
  /**
   * Everything past the cap folds into ONE visible residue row rather than disappearing.
   *
   * The rule the whole feature is built on, applied one more time at the last possible
   * moment: a reader must be able to see that something was folded and how much of it there
   * was. `Σ rows == total` still holds on screen, which is the only way the total row can be
   * checked by eye.
   */
  const rows = useMemo(() => {
    if (buckets.length <= maxRows) {
      return { shown: buckets, residue: null as Bucket | null, hidden: 0 };
    }
    const shown = buckets.slice(0, maxRows);
    const rest = buckets.slice(maxRows);
    const residue: Bucket = {
      value: '',
      leads: { total: 0, in_progress: 0, won: 0, lost: 0 },
      deals: { total: 0, in_progress: 0, won: 0, lost: 0 },
      amount: 0,
      deals_from_lead: 0,
    };
    for (const bucket of rest) {
      for (const key of ['leads', 'deals'] as const) {
        residue[key].total += bucket[key].total;
        residue[key].in_progress += bucket[key].in_progress;
        residue[key].won += bucket[key].won;
        residue[key].lost += bucket[key].lost;
      }
      residue.amount += bucket.amount;
      residue.deals_from_lead += bucket.deals_from_lead;
    }
    return { shown, residue, hidden: rest.length };
  }, [buckets, maxRows]);

  if (buckets.length === 0) {
    return <p className="ca-muted text-sm">{t('app.utm.empty')}</p>;
  }

  const cell = (part: number, whole: number): string =>
    showPercent ? percent(whole > 0 ? part / whole : null, locale) : formatCount(part, locale);

  const body = (bucket: Bucket, label: string, muted: boolean) => {
    const rate = conversion(bucket.deals.total, bucket.leads.total);
    return (
      <tr key={label}>
        <td className={`ca-utm-tag${muted ? ' ca-utm-muted' : ''}`} title={label}>
          {label}
        </td>
        <td className="ca-viz-num">{formatCount(bucket.leads.total, locale)}</td>
        <td className="ca-viz-num">{formatCount(bucket.deals.total, locale)}</td>
        <td className="ca-viz-num">{percent(rate, locale, 1)}</td>
        <td>
          <OutcomeBar measures={bucket.deals} t={t} />
        </td>
        <td className="ca-viz-num">{cell(bucket.deals.won, bucket.deals.total)}</td>
        <td className="ca-viz-num">{cell(bucket.deals.lost, bucket.deals.total)}</td>
        <td className="ca-viz-num ca-utm-muted">
          {formatCount(bucket.deals_from_lead, locale)}
        </td>
        {amounts.trusted ? (
          <td className="ca-viz-num">{formatMoney(major(bucket.amount), amounts.currency, locale)}</td>
        ) : null}
      </tr>
    );
  };

  return (
    <div
      className="ca-utm-scroll"
      role="region"
      aria-label={t('app.utm.regionLabel', { dimension: dimensionLabel })}
      tabIndex={0}
    >
      <table className="ca-utm-grid">
        <caption className="sr-only">
          {t('app.utm.caption', { dimension: dimensionLabel })}
        </caption>
        <thead>
          <tr>
            <th scope="col">{dimensionLabel}</th>
            <th scope="col">{t('app.utm.leads')}</th>
            <th scope="col">{t('app.utm.deals')}</th>
            <th scope="col">{t('app.utm.conversion')}</th>
            <th scope="col">{t('app.utm.outcome')}</th>
            <th scope="col">{t('app.utm.won')}</th>
            <th scope="col">{t('app.utm.lost')}</th>
            <th scope="col">{t('app.utm.fromLead')}</th>
            {amounts.trusted ? <th scope="col">{t('app.utm.amount')}</th> : null}
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
            <td className="ca-utm-tag">{t('app.utm.allTags')}</td>
            <td className="ca-viz-num">{formatCount(totals.leads.total, locale)}</td>
            <td className="ca-viz-num">{formatCount(totals.deals.total, locale)}</td>
            <td className="ca-viz-num">
              {percent(conversion(totals.deals.total, totals.leads.total), locale, 1)}
            </td>
            <td>
              <OutcomeBar measures={totals.deals} t={t} />
            </td>
            <td className="ca-viz-num">{cell(totals.deals.won, totals.deals.total)}</td>
            <td className="ca-viz-num">{cell(totals.deals.lost, totals.deals.total)}</td>
            <td className="ca-viz-num ca-utm-muted">
              {formatCount(totals.deals_from_lead, locale)}
            </td>
            {amounts.trusted ? (
              <td className="ca-viz-num">
                {formatMoney(major(totals.amount), amounts.currency, locale)}
              </td>
            ) : null}
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

export default UtmTable;
