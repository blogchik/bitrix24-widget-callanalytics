/**
 * The UTM page's data model and its whole arithmetic (§4.13).
 *
 * ---------------------------------------------------------------------------------
 * **Everything in this file exists so that a filter costs nothing.**
 *
 * `POST /api/v1/utm` is a live CRM scan of two entities. Period and employee change what
 * is scanned and therefore cost a round trip; the five UTM filters and the grouping
 * control do NOT, because the response already carries every combination row. The fold
 * below is what turns those rows into whichever view the reader asked for, in the browser,
 * with no network at all - the same bargain `DealStageTable` strikes for its percentage
 * toggle, applied to the controls a marketer actually touches.
 *
 * Two consequences worth stating, because both are easy to undo by accident:
 *
 *  * **Filter options come from `facets`, never from the folded rows.** `facets` is
 *    computed server-side BEFORE any bucketing and before any filter, so the option lists
 *    are exact and they never shrink when a filter is applied. The alternative - deriving
 *    options from what survived the current filter - is the classic cross-filter trap:
 *    pick `utm_source=google`, watch `utm_medium` collapse to one entry, and now there is
 *    no way back to the others.
 *  * **Money is integer cents here, never a float.** The server sends decimal strings at
 *    scale two precisely so the browser can add them exactly; parsing them with
 *    `parseFloat` and summing would reintroduce the drift the string form removed, and the
 *    column would stop agreeing with its own total in the last digit.
 * ---------------------------------------------------------------------------------
 */

import { seriesVar } from '@/lib/viz';

/** The five tags, in the fixed order `combinations[].k` is positional against. */
export const DIMENSIONS = [
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'utm_content',
  'utm_term',
] as const;

export type Dimension = (typeof DIMENSIONS)[number];

/**
 * Leads and deals are two fixed entities, so they take the validated two-series pair.
 *
 * This is NOT a new chart colour - `viz.ts` is untouched and its hexes are the same two the
 * palette validator was run against. It is a second NAME for one of them, which is what the
 * contribution rule is about: the thing that must not drift is the specification value, not
 * the identifier pointing at it. Assignment is by ENTITY and never by size, so applying a
 * filter never repaints the bars that survive it.
 */
export const ENTITY_VAR = {
  lead: seriesVar('answered'),
  deal: seriesVar('no_answer'),
} as const;

// --- the wire -----------------------------------------------------------------------------

/** One entity's counters. `total` is the sum of the other three, always (asserted server-side). */
export interface UtmMeasures {
  total: number;
  in_progress: number;
  won: number;
  lost: number;
}

/** Everything one row carries, whatever it is a row OF. */
export interface UtmAcc {
  leads: UtmMeasures;
  deals: UtmMeasures;
  /** A decimal string at scale two. Parse with {@link cents}; never with `parseFloat`. */
  amount: string;
  deals_from_lead: number;
}

/** One combination row. `k` is POSITIONAL against `UtmResponse.dimensions`. */
export interface UtmRow extends UtmAcc {
  k: string[];
}

export interface UtmFacetValue extends UtmAcc {
  value: string;
}

export interface UtmFacet {
  dimension: Dimension;
  distinct_total: number;
  distinct_kept: number;
  /** Whether this dimension is part of the current composite key. */
  selected: boolean;
  /** Lifted out of the key because the row count was still past the cap after bucketing. */
  collapsed: boolean;
  values: UtmFacetValue[];
}

export interface UtmDay {
  date: string;
  leads: number;
  deals: number;
}

export interface UtmEntityScan {
  available: boolean;
  reason: string;
  folded: number;
  total: number;
  dialect?: string;
  tagged_rows?: number;
  utm_fields?: string[];
}

export interface UtmResponse {
  range: { from: string; to: string; days: number; timezone: string; preset: string };
  filters: { employees: number[] };
  dimensions: Dimension[];
  /** Declared, never hardcoded: a real tag could be spelled like a bucket. */
  buckets: { none: string; other: string; collapsed: string };
  facets: UtmFacet[];
  combinations: UtmRow[];
  totals: UtmAcc;
  days: UtmDay[];
  amounts: {
    trusted: boolean;
    source: 'account' | 'native';
    currency: string;
    currencies: string[];
    rows_without_amount: number;
  };
  scan: {
    leads: UtmEntityScan;
    deals: UtmEntityScan;
    scanned_total: number;
    scan_cap: number;
    value_cap: number;
    combination_cap: number;
    value_max_chars: number;
    combinations: number;
    collapsed: Dimension[];
    rest_requests: number;
    from_cache: boolean;
  };
}

// --- money ---------------------------------------------------------------------------------

/**
 * A scale-two decimal string as an integer number of cents.
 *
 * Written out rather than `Math.round(parseFloat(s) * 100)` because that expression is
 * wrong for values this page will actually see: `parseFloat('8.115') * 100` is
 * `811.4999999999999`, and a portal with a few thousand deals accumulates enough of those
 * to make a column disagree with its own total by a unit. The server quantises per record
 * so that this parse is exact; throwing that away in the first line of the client would be
 * a strange way to spend it.
 */
export function cents(amount: string | null | undefined): number {
  const match = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec((amount ?? '').trim());
  if (!match) {
    return 0;
  }
  const whole = Number(match[2]);
  const fraction = Number((match[3] ?? '').padEnd(2, '0'));
  return (match[1] === '-' ? -1 : 1) * (whole * 100 + fraction);
}

/** Cents back to the major units `formatMoney` takes. */
export function major(value: number): number {
  return value / 100;
}

// --- folding ------------------------------------------------------------------------------

/** What the reader has selected, per dimension. An empty or absent list means "all". */
export type Selections = Partial<Record<Dimension, readonly string[]>>;

/** One folded bucket: a value of the grouping dimension, and everything under it. */
export interface Bucket {
  value: string;
  leads: UtmMeasures;
  deals: UtmMeasures;
  /** Integer cents, summed exactly. */
  amount: number;
  deals_from_lead: number;
}

function emptyMeasures(): UtmMeasures {
  return { total: 0, in_progress: 0, won: 0, lost: 0 };
}

export function emptyBucket(value = ''): Bucket {
  return { value, leads: emptyMeasures(), deals: emptyMeasures(), amount: 0, deals_from_lead: 0 };
}

function addMeasures(into: UtmMeasures, from: UtmMeasures): void {
  into.total += from.total;
  into.in_progress += from.in_progress;
  into.won += from.won;
  into.lost += from.lost;
}

export function addRow(into: Bucket, row: UtmAcc): void {
  addMeasures(into.leads, row.leads);
  addMeasures(into.deals, row.deals);
  into.amount += cents(row.amount);
  into.deals_from_lead += row.deals_from_lead;
}

/**
 * Does this row survive the reader's filters?
 *
 * OR within one dimension, AND across dimensions - the convention `MultiSelect` already
 * documents for the other pages' facets, and the one a reader expects from every filter bar
 * they have ever used. An empty selection is "all" rather than "none", so a freshly opened
 * page shows everything.
 *
 * A dimension the response did not group by cannot be filtered on, because the row does not
 * carry it. The caller is responsible for not offering that control; this returns `true`
 * rather than `false` for it, so a stale selection can never blank the page.
 */
export function matches(
  row: UtmRow,
  dimensions: readonly Dimension[],
  selections: Selections,
): boolean {
  for (let index = 0; index < dimensions.length; index += 1) {
    const dimension = dimensions[index];
    const chosen = dimension === undefined ? undefined : selections[dimension];
    if (!chosen || chosen.length === 0) {
      continue;
    }
    const value = row.k[index] ?? '';
    if (!chosen.includes(value)) {
      return false;
    }
  }
  return true;
}

/**
 * Fold the combination rows onto one dimension, under the current filters.
 *
 * Sorted by weight descending so the row a reader is looking for is near the top, with ties
 * broken by the value itself so the order does not flicker between two renders of the same
 * data.
 */
export function foldBy(
  rows: readonly UtmRow[],
  dimensions: readonly Dimension[],
  selections: Selections,
  groupBy: Dimension,
): Bucket[] {
  const index = dimensions.indexOf(groupBy);
  if (index < 0) {
    return [];
  }
  const out = new Map<string, Bucket>();
  for (const row of rows) {
    if (!matches(row, dimensions, selections)) {
      continue;
    }
    const value = row.k[index] ?? '';
    let bucket = out.get(value);
    if (bucket === undefined) {
      bucket = emptyBucket(value);
      out.set(value, bucket);
    }
    addRow(bucket, row);
  }
  return [...out.values()].sort(
    (a, b) =>
      b.leads.total + b.deals.total - (a.leads.total + a.deals.total) ||
      a.value.localeCompare(b.value),
  );
}

/** The grand total of whatever survived the filters. */
export function totalOf(buckets: readonly Bucket[]): Bucket {
  const out = emptyBucket('');
  for (const bucket of buckets) {
    addMeasures(out.leads, bucket.leads);
    addMeasures(out.deals, bucket.deals);
    out.amount += bucket.amount;
    out.deals_from_lead += bucket.deals_from_lead;
  }
  return out;
}

/**
 * A two-dimensional cross-tab of whatever survived the filters.
 *
 * Returns dense rows in weight order so the grid has a stable axis: a heatmap whose rows
 * reorder as a filter changes is unreadable, and one with holes in it cannot be scanned
 * along either axis.
 */
export interface Matrix {
  rows: string[];
  columns: string[];
  /** `cells[rowValue][columnValue]`, sparse: an absent key is zero, not missing. */
  cells: Record<string, Record<string, number>>;
  max: number;
}

export function crossTab(
  rows: readonly UtmRow[],
  dimensions: readonly Dimension[],
  selections: Selections,
  rowDimension: Dimension,
  columnDimension: Dimension,
  measure: (row: UtmAcc) => number,
  limit = 12,
): Matrix {
  const rowIndex = dimensions.indexOf(rowDimension);
  const columnIndex = dimensions.indexOf(columnDimension);
  if (rowIndex < 0 || columnIndex < 0) {
    return { rows: [], columns: [], cells: {}, max: 0 };
  }
  const cells: Record<string, Record<string, number>> = {};
  const rowWeight = new Map<string, number>();
  const columnWeight = new Map<string, number>();

  for (const row of rows) {
    if (!matches(row, dimensions, selections)) {
      continue;
    }
    const value = measure(row);
    if (value <= 0) {
      continue;
    }
    const r = row.k[rowIndex] ?? '';
    const c = row.k[columnIndex] ?? '';
    (cells[r] ??= {})[c] = (cells[r]?.[c] ?? 0) + value;
    rowWeight.set(r, (rowWeight.get(r) ?? 0) + value);
    columnWeight.set(c, (columnWeight.get(c) ?? 0) + value);
  }

  const rank = (weights: Map<string, number>): string[] =>
    [...weights.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, limit)
      .map(([value]) => value);

  const keptRows = rank(rowWeight);
  const keptColumns = rank(columnWeight);
  let max = 0;
  for (const r of keptRows) {
    for (const c of keptColumns) {
      max = Math.max(max, cells[r]?.[c] ?? 0);
    }
  }
  return { rows: keptRows, columns: keptColumns, cells, max };
}

// --- derived numbers ------------------------------------------------------------------------

/**
 * `deals / leads`, or `null` when there is no denominator.
 *
 * `null` renders as `—`, never as `∞`, never as `0 %` and never as a hidden row. A tag with
 * deals and no leads is a real and common state - a deal somebody created by hand, or one
 * converted from a lead created before the period - and hiding it would remove revenue from
 * the page with nothing to say it had gone.
 *
 * A value ABOVE 1 is likewise normal and means exactly that. The page says so in words,
 * because the first cell over 100 % otherwise destroys the reader's trust in everything
 * else on the screen.
 */
export function conversion(deals: number, leads: number): number | null {
  return leads > 0 ? deals / leads : null;
}

/** Mean deal size in cents, or `null` when nothing was counted. */
export function averageDeal(amountCents: number, deals: number): number | null {
  return deals > 0 ? amountCents / deals : null;
}

/** One entity's outcome shares, for the 100 %-stacked bar. Empty totals give an empty bar. */
export function shares(measures: UtmMeasures): { won: number; lost: number; inProgress: number } {
  if (measures.total <= 0) {
    return { won: 0, lost: 0, inProgress: 0 };
  }
  return {
    won: measures.won / measures.total,
    lost: measures.lost / measures.total,
    inProgress: measures.in_progress / measures.total,
  };
}

// --- labels ---------------------------------------------------------------------------------

/**
 * A tag value as the page should print it.
 *
 * The three reserved buckets are compared against the values the RESPONSE declared, never
 * against literals: a portal may genuinely tag a campaign `other`, and a page that
 * hardcoded the string would relabel real data as a residue row.
 */
export function bucketLabel(
  value: string,
  buckets: UtmResponse['buckets'],
  t: (key: string) => string,
): string {
  if (value === buckets.none) {
    return t('app.utm.noTag');
  }
  if (value === buckets.other) {
    return t('app.utm.otherValues');
  }
  if (value === buckets.collapsed) {
    return t('app.utm.collapsedValue');
  }
  return value;
}

/** The i18n key for one dimension's label. */
export function dimensionKey(dimension: Dimension): string {
  return `app.utm.dimension.${dimension}`;
}
