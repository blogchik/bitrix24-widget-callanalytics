/**
 * The UTM page's data model and its whole arithmetic (§4.13).
 *
 * ---------------------------------------------------------------------------------
 * **The page is one table, and choosing its rows costs nothing.**
 *
 * `/api/v1/utm` answers one row per tag combination. Period and employee change what is
 * read and therefore cost a round trip; the tag the table is grouped by does NOT, because
 * folding the combination rows onto one tag is arithmetic over data that is already here.
 * The response still carries `facets`, `days` and the amounts; the page no longer draws
 * them (simplified 2026-09-26 to the owner's one-table layout).
 * ---------------------------------------------------------------------------------
 */

import type { MirrorReportMeta } from '@/lib/api';

/** The five tags, in the fixed order `combinations[].k` is positional against. */
export const DIMENSIONS = [
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'utm_content',
  'utm_term',
] as const;

export type Dimension = (typeof DIMENSIONS)[number];

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
  /** A decimal string at scale two. */
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

export interface UtmResponse extends MirrorReportMeta {
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

// --- folding ------------------------------------------------------------------------------

/** One folded bucket: a value of the grouping dimension, and everything under it. */
export interface Bucket {
  value: string;
  leads: UtmMeasures;
  deals: UtmMeasures;
}

function emptyMeasures(): UtmMeasures {
  return { total: 0, in_progress: 0, won: 0, lost: 0 };
}

export function emptyBucket(value = ''): Bucket {
  return { value, leads: emptyMeasures(), deals: emptyMeasures() };
}

function addMeasures(into: UtmMeasures, from: UtmMeasures): void {
  into.total += from.total;
  into.in_progress += from.in_progress;
  into.won += from.won;
  into.lost += from.lost;
}

export function addBucket(into: Bucket, from: Pick<Bucket, 'leads' | 'deals'>): void {
  addMeasures(into.leads, from.leads);
  addMeasures(into.deals, from.deals);
}

/**
 * Fold the combination rows onto one dimension.
 *
 * Sorted by weight descending so the row a reader is looking for is near the top, with ties
 * broken by the value itself so the order does not flicker between two renders of the same
 * data.
 */
export function foldBy(
  rows: readonly UtmRow[],
  dimensions: readonly Dimension[],
  groupBy: Dimension,
): Bucket[] {
  const index = dimensions.indexOf(groupBy);
  if (index < 0) {
    return [];
  }
  const out = new Map<string, Bucket>();
  for (const row of rows) {
    const value = row.k[index] ?? '';
    let bucket = out.get(value);
    if (bucket === undefined) {
      bucket = emptyBucket(value);
      out.set(value, bucket);
    }
    addBucket(bucket, row);
  }
  return [...out.values()].sort(
    (a, b) =>
      b.leads.total + b.deals.total - (a.leads.total + a.deals.total) ||
      a.value.localeCompare(b.value),
  );
}

/** The grand total of the folded buckets. */
export function totalOf(buckets: readonly Bucket[]): Bucket {
  const out = emptyBucket('');
  for (const bucket of buckets) {
    addBucket(out, bucket);
  }
  return out;
}

// --- derived numbers ------------------------------------------------------------------------

/**
 * `part / leads`, or `null` when there is no denominator.
 *
 * `null` renders as `—`, never as `∞`, never as `0 %` and never as a hidden row. A tag with
 * deals and no leads is a real and common state - a deal somebody created by hand, or one
 * converted from a lead created before the period - and hiding it would remove it from the
 * page with nothing to say it had gone.
 *
 * Deals over leads ABOVE 1 is likewise normal and means exactly that; the page says so in
 * words. Converted leads over leads cannot pass 1: both count the same leads.
 */
export function conversion(part: number, leads: number): number | null {
  return leads > 0 ? part / leads : null;
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
