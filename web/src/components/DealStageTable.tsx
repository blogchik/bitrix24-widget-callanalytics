'use client';

/**
 * Deals by operator, one table per funnel, one column per stage.
 *
 * ---------------------------------------------------------------------------------
 * **Why one table per funnel rather than the single grid the reference report shows.**
 *
 * The screenshot reads as one grid only because that portal's two funnels happen to share
 * stage names. Funnels do not share a stage directory — `crm.status.list` is called once
 * per funnel and a stage id is unique only inside its own — so a shared header would have
 * to be the union of every funnel's stages. Fifteen funnels of eight stages is a hundred
 * and twenty columns of which each row can fill eight, and a reader cannot tell an empty
 * cell ("nobody is at this stage") from a meaningless one ("this stage is not in this
 * funnel"). A block per funnel says that structurally and costs nothing to read.
 *
 * The cross-funnel summary is therefore a separate strip with NO stage columns at all:
 * adding up two funnels' "Проверка" counts would be an invented number.
 * ---------------------------------------------------------------------------------
 *
 * **Why the first column is pinned and the rest scrolls.** The same reason the by-hour
 * grid pins its two: a stage count means nothing without the name of the operator it
 * belongs to, and a wide grid scrolled sideways is otherwise a wall of numbers belonging
 * to nobody. The scroll is *announced* — `role="region"` with a name that says it scrolls —
 * rather than left to be discovered, and the row's own total is pinned to the opposite edge
 * so the cells visibly slide underneath it.
 *
 * **Why the stage columns are `Record`-keyed and not positional.** `cells` arrives sparse,
 * keyed by `"<category_id>:<status_id>"`, and a column the server discovered on a deal but
 * could not name is appended with `known: false`. A positional array would have to be
 * rebuilt whenever the two disagreed; a lookup cannot, and under
 * `noUncheckedIndexedAccess` both cost the same single `?? 0` at the point of use.
 *
 * **Why percentages are computed here and not sent.** `won / total` and `lost / total` are
 * functions of integers the server already sent. Deriving them in the browser keeps the
 * wire honest (every number on it is a count) and lets the "show %" toggle re-render
 * without a REST round trip, which on this page costs a live CRM scan.
 */

import { useMemo } from 'react';

import { formatCount, percent } from '@/lib/format';

/** One stage of one funnel — a column. Mirrors `api/app/bitrix/deals.py::Stage`. */
export interface StageColumn {
  key: string;
  category_id: number;
  status_id: string;
  name: string;
  semantic: 'P' | 'S' | 'F';
  sort: number;
  known: boolean;
}

/** The counters every row, subtotal and grand total carries. */
export interface DealMeasures {
  total: number;
  in_progress: number;
  won: number;
  lost: number;
  unknown_stage: number;
  cells?: Record<string, number>;
}

export interface DealRow extends DealMeasures {
  user_id: number | null;
  name?: string | null;
  phone_inner?: string | null;
  active?: boolean | null;
}

export interface DealGroup {
  category_id: number | null;
  name: string;
  is_default: boolean;
  stage_keys: string[];
  rows: DealRow[];
  subtotal: DealMeasures;
  total_rows: number;
  truncated: boolean;
}

type Translate = (key: string, values?: Record<string, string | number>) => string;

export interface DealStageTableProps {
  group: DealGroup;
  /** Every stage of every funnel; this table uses the ones `group.stage_keys` names. */
  stages: readonly StageColumn[];
  locale: string;
  t: Translate;
  /** The operator label, already carrying the extension (`withExtension`). */
  nameOf: (row: DealRow) => string;
  /** Render each stage cell's share of the row total under its count. */
  showPercent: boolean;
}

export interface DealSummaryStripProps {
  totals: DealMeasures;
  locale: string;
  t: Translate;
}

/** `won / total`, or null when there is no total to divide by. */
function rate(part: number, whole: number): number | null {
  return whole > 0 ? part / whole : null;
}

function stageLabel(stage: StageColumn, t: Translate): string {
  // A stage NAME is portal data, never a catalogue key: it is whatever the administrator
  // typed, in whatever language they typed it, and `tools/check-i18n.mjs` could not see it
  // even if we wanted it translated. Only the fallback for a stage we could not name is a
  // key — the same rule the telephony line names already follow.
  if (stage.name) {
    return stage.name;
  }
  if (!stage.status_id) {
    return t('app.deals.blankStage');
  }
  return t('app.deals.unknownStage', { id: stage.status_id });
}

/**
 * The cross-funnel totals: five numbers and two rates, and deliberately no stage columns.
 *
 * It is a definition list rather than a table because it has one row — a one-row table
 * makes a screen reader announce a grid the reader then has to navigate out of.
 */
export function DealSummaryStrip({ totals, locale, t }: DealSummaryStripProps) {
  const wonRate = rate(totals.won, totals.total);
  const lostRate = rate(totals.lost, totals.total);
  return (
    <section className="ca-card ca-deals-strip" aria-label={t('app.deals.grandTotal')}>
      <h3 className="ca-deals-strip-title">{t('app.deals.grandTotal')}</h3>
      <dl className="ca-deals-strip-grid">
        <div>
          <dt>{t('app.deals.totalColumn')}</dt>
          <dd>{formatCount(totals.total, locale)}</dd>
        </div>
        <div>
          <dt>{t('app.deals.inProgress')}</dt>
          <dd>{formatCount(totals.in_progress, locale)}</dd>
        </div>
        <div>
          <dt>{t('app.deals.won')}</dt>
          <dd>
            {formatCount(totals.won, locale)}
            <span className="ca-deals-share">{percent(wonRate, locale, 1)}</span>
          </dd>
        </div>
        <div>
          <dt>{t('app.deals.lost')}</dt>
          <dd>
            {formatCount(totals.lost, locale)}
            <span className="ca-deals-share">{percent(lostRate, locale, 1)}</span>
          </dd>
        </div>
      </dl>
      <p className="ca-deals-note">{t('app.deals.grandTotalNote')}</p>
    </section>
  );
}

function MeasureCells({
  measures,
  locale,
  showPercent,
}: {
  measures: DealMeasures;
  locale: string;
  showPercent: boolean;
}) {
  const wonRate = rate(measures.won, measures.total);
  const lostRate = rate(measures.lost, measures.total);
  return (
    <>
      <td className="ca-deals-roll">{formatCount(measures.in_progress, locale)}</td>
      <td className="ca-deals-roll">
        {formatCount(measures.won, locale)}
        {showPercent ? <span className="ca-deals-share">{percent(wonRate, locale, 1)}</span> : null}
      </td>
      <td className="ca-deals-roll">
        {formatCount(measures.lost, locale)}
        {showPercent ? <span className="ca-deals-share">{percent(lostRate, locale, 1)}</span> : null}
      </td>
    </>
  );
}

export default function DealStageTable({
  group,
  stages,
  locale,
  t,
  nameOf,
  showPercent,
}: DealStageTableProps) {
  // The columns this block draws, in the order the server put them: the funnel's own
  // stages by SORT, then whatever the deals turned up that the dictionary could not name.
  const columns = useMemo((): StageColumn[] => {
    const byKey = new Map(stages.map((stage) => [stage.key, stage]));
    return group.stage_keys
      .map((key) => byKey.get(key))
      .filter((stage): stage is StageColumn => stage !== undefined);
  }, [group.stage_keys, stages]);

  // "Прочее" exists only when it has something in it. A permanently empty column that
  // nobody can explain is worse than an absent one; a non-empty one that is hidden makes
  // the cells silently fail to add up to the row total.
  const hasUnknown = useMemo(
    () => group.subtotal.unknown_stage > 0,
    [group.subtotal.unknown_stage],
  );

  const funnelName = group.name || t('app.deals.unknownFunnel', { id: group.category_id ?? 0 });

  return (
    <section className="ca-card ca-deals" aria-labelledby={`ca-deals-h-${group.category_id ?? 'x'}`}>
      <h3 className="ca-deals-title" id={`ca-deals-h-${group.category_id ?? 'x'}`}>
        {funnelName}
      </h3>
      <div
        className="ca-deals-scroll"
        role="region"
        tabIndex={0}
        aria-label={t('app.deals.regionLabel', { funnel: funnelName })}
      >
        <table className="ca-deals-grid">
          <caption className="sr-only">{t('app.deals.caption', { funnel: funnelName })}</caption>
          <thead>
            <tr>
              <th scope="col" className="ca-deals-id ca-deals-user">
                {t('app.deals.operator')}
              </th>
              <th scope="col" className="ca-deals-roll">
                {t('app.deals.inProgress')}
              </th>
              <th scope="col" className="ca-deals-roll">
                {t('app.deals.won')}
              </th>
              <th scope="col" className="ca-deals-roll">
                {t('app.deals.lost')}
              </th>
              {columns.map((stage) => (
                <th scope="col" key={stage.key} className="ca-deals-cell-h" title={stageLabel(stage, t)}>
                  {stageLabel(stage, t)}
                </th>
              ))}
              {hasUnknown ? (
                <th scope="col" className="ca-deals-cell-h">
                  {t('app.deals.other')}
                </th>
              ) : null}
              <th scope="col" className="ca-deals-total">
                {t('app.deals.totalColumn')}
              </th>
            </tr>
          </thead>
          <tbody>
            {group.rows.map((row) => {
              const cells = row.cells ?? {};
              return (
                <tr key={row.user_id ?? 'unassigned'} className={row.active === false ? 'ca-deals-dim' : undefined}>
                  <th scope="row" className="ca-deals-id ca-deals-user" title={nameOf(row)}>
                    {nameOf(row)}
                  </th>
                  <MeasureCells measures={row} locale={locale} showPercent={showPercent} />
                  {columns.map((stage) => {
                    const value = cells[stage.key] ?? 0;
                    return (
                      <td
                        key={stage.key}
                        className="ca-deals-cell"
                        data-empty={value === 0 ? 'true' : 'false'}
                      >
                        {formatCount(value, locale)}
                        {showPercent && value > 0 ? (
                          <span className="ca-deals-share">
                            {percent(rate(value, row.total), locale, 1)}
                          </span>
                        ) : null}
                      </td>
                    );
                  })}
                  {hasUnknown ? (
                    <td
                      className="ca-deals-cell"
                      data-empty={row.unknown_stage === 0 ? 'true' : 'false'}
                    >
                      {formatCount(row.unknown_stage, locale)}
                    </td>
                  ) : null}
                  <td className="ca-deals-total">{formatCount(row.total, locale)}</td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            {/* Server-computed over EVERY operator this funnel matched, including any past
                the row cap. One re-derived from the rows on screen would be a confident
                number that a truncated answer makes wrong — the rule the by-hour grid
                already states for its own footer. */}
            <tr>
              <th scope="row" className="ca-deals-id ca-deals-user">
                {t('app.deals.subtotalRow')}
              </th>
              <MeasureCells measures={group.subtotal} locale={locale} showPercent={showPercent} />
              {columns.map((stage) => {
                const value = group.subtotal.cells?.[stage.key] ?? 0;
                return (
                  <td key={stage.key} className="ca-deals-cell" data-empty={value === 0 ? 'true' : 'false'}>
                    {formatCount(value, locale)}
                  </td>
                );
              })}
              {hasUnknown ? (
                <td className="ca-deals-cell">{formatCount(group.subtotal.unknown_stage, locale)}</td>
              ) : null}
              <td className="ca-deals-total">{formatCount(group.subtotal.total, locale)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
      {group.truncated ? (
        <p className="ca-deals-note" role="status">
          {t('app.deals.rowsTruncated', { shown: group.rows.length, total: group.total_rows })}
        </p>
      ) : null}
    </section>
  );
}

export const DEAL_CSS = `
.ca-deals-scroll {
  position: relative;
  overflow-x: auto;
  overscroll-behavior-x: contain;
}
/* Hidden by a clip path rather than by the overflow of a 1px box, so the UI audit stops
   counting the app's one intentional piece of hidden text as clipped content. The by-hour
   grid and the calls table carry the same rule for the same reason. */
.ca-deals-grid caption.sr-only {
  overflow: visible;
  clip-path: inset(50%);
}
.ca-deals-title {
  padding: 12px var(--ca-calls-px, 16px) 0;
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--ca-text);
}
.ca-deals-grid {
  border-collapse: separate;
  border-spacing: 0;
  font-size: 12px;
  width: max-content;
  min-width: 100%;
}
.ca-deals-grid th,
.ca-deals-grid td {
  border-bottom: 1px solid var(--ca-border-soft);
  padding: 0;
  white-space: nowrap;
}
.ca-deals-grid thead th {
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--ca-surface);
  border-bottom: 1px solid var(--ca-border);
  font-size: 11px;
  font-weight: 600;
  color: var(--ca-muted);
  text-align: center;
  padding: 6px 8px;
}
/* ONE pinned identity column, not the by-hour grid's two. That grid spends 242px of the
   343px a 375px screen leaves; this one needs room for three rollup columns before the
   stages begin, so the operator column is the only thing that stays put. */
.ca-deals-grid .ca-deals-id {
  position: sticky;
  left: 0;
  z-index: 1;
  background: var(--ca-surface);
  text-align: left;
  padding: 6px 10px;
  border-right: 1px solid var(--ca-border);
}
.ca-deals-grid thead .ca-deals-id {
  z-index: 3;
}
.ca-deals-grid .ca-deals-user {
  min-width: 140px;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ca-deals-grid .ca-deals-total {
  position: sticky;
  right: 0;
  z-index: 1;
  background: var(--ca-surface);
  border-left: 1px solid var(--ca-border);
  min-width: 64px;
  text-align: center;
  padding: 5px 8px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.ca-deals-grid thead .ca-deals-total {
  z-index: 3;
}
.ca-deals-grid td.ca-deals-cell,
.ca-deals-grid td.ca-deals-roll {
  min-width: 52px;
  text-align: center;
  padding: 5px 6px;
  color: var(--ca-viz-ink);
  font-variant-numeric: tabular-nums;
}
.ca-deals-grid td.ca-deals-roll {
  background: var(--ca-viz-band, transparent);
}
/* A stage nobody is at is left plain rather than given a faint tint: "no deals" is not a
   small number of deals, and the grid reads far faster when the empty stages look empty. */
.ca-deals-grid td.ca-deals-cell[data-empty='true'] {
  color: var(--ca-muted);
}
.ca-deals-grid .ca-deals-cell-h {
  max-width: 132px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ca-deals-share {
  display: block;
  font-size: 10px;
  color: var(--ca-muted);
  font-variant-numeric: tabular-nums;
}
.ca-deals-grid tfoot th,
.ca-deals-grid tfoot td {
  border-top: 2px solid var(--ca-border);
  border-bottom: none;
  font-weight: 600;
  background: var(--ca-surface);
}
.ca-deals-grid tfoot td.ca-deals-cell,
.ca-deals-grid tfoot td.ca-deals-roll {
  color: var(--ca-text);
}
.ca-deals-grid tfoot .ca-deals-id,
.ca-deals-grid tfoot .ca-deals-total {
  z-index: 2;
}
.ca-deals-dim {
  opacity: 0.72;
}
.ca-deals-note {
  padding: 10px var(--ca-calls-px, 16px);
  margin: 0;
  font-size: 12px;
  color: var(--ca-muted);
}
/* The one control that lives beside the period rather than in the filter grid: it changes
   how a cell is DRAWN, not which deals were fetched, so putting it with the filters would
   suggest it costs a request. It does not. */
.ca-deals-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: var(--ca-control-h);
  font-size: 13px;
  color: var(--ca-text);
  cursor: pointer;
}
.ca-deals-toggle input {
  width: 16px;
  height: 16px;
  accent-color: var(--ca-accent);
}
.ca-deals-strip-title {
  padding: 12px var(--ca-calls-px, 16px) 0;
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}
.ca-deals-strip-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px;
  padding: 10px var(--ca-calls-px, 16px);
  margin: 0;
}
.ca-deals-strip-grid dt {
  font-size: 11px;
  color: var(--ca-muted);
}
.ca-deals-strip-grid dd {
  margin: 2px 0 0;
  font-size: 20px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
`;
