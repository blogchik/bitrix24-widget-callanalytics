'use client';

/**
 * Tags ranked: leads beside deals, or conversion alone (§4.13).
 *
 * CSS grid rather than SVG, for `EmployeeBars`' reason: these are axis-aligned rectangles
 * with text beside them, so the grid gives the same marks with real text wrapping and no
 * measurement pass.
 *
 * Three rules inherited verbatim from that component, each of them a specification
 * consequence rather than taste:
 *
 *  * **A row past the cap folds into "Other", never into a generated hue.** Cycling a
 *    categorical palette past its validated length is how two tags end up wearing the same
 *    colour on one screen. Here the colour carries the ENTITY (lead or deal), never the tag,
 *    so the palette is two values wide and stays that way however many tags there are.
 *  * **The name sits outside the bar and the value at its end.** A label inside a bar is
 *    unreadable as soon as the bar is short, which is exactly the row a reader is hunting
 *    for.
 *  * **The name column is a share of the panel and the name WRAPS.** A clipped tag is the
 *    one thing in this panel a reader cannot recover by looking harder, and `utm_campaign`
 *    values are routinely long.
 *
 * In `conversion` mode the bar is a single hue (`--ca-viz-bar`) because one series needs no
 * legend, and the scale is the largest conversion in view rather than 100 %: a page where
 * every bar is a sliver because one tag converts at 4 % has stopped comparing anything.
 */

import { useMemo, useState } from 'react';

import { formatCount, percent } from '@/lib/format';
import { END_RADIUS_PX } from '@/lib/viz';
import { ENTITY_VAR, bucketLabel, conversion, type Bucket, type UtmResponse } from '@/lib/utm';

type Translate = (key: string, values?: Record<string, string | number>) => string;

export type BarMode = 'volume' | 'conversion';

export interface UtmSourceBarsProps {
  buckets: readonly Bucket[];
  sentinels: UtmResponse['buckets'];
  mode: BarMode;
  locale: string;
  t: Translate;
}

/** Named rows before the residue. Eight is `EmployeeBars`' number, for the same panel. */
const MAX_NAMED = 8;
const ROW_HEIGHT = 30;

interface Row {
  key: string;
  label: string;
  muted: boolean;
  leads: number;
  deals: number;
  rate: number | null;
}

export function UtmSourceBars({ buckets, sentinels, mode, locale, t }: UtmSourceBarsProps) {
  const rows = useMemo((): Row[] => {
    const toRow = (bucket: Bucket, key: string, label: string, muted: boolean): Row => ({
      key,
      label,
      muted,
      leads: bucket.leads.total,
      deals: bucket.deals.total,
      rate: conversion(bucket.deals.total, bucket.leads.total),
    });
    const named = buckets.slice(0, MAX_NAMED).map((bucket, index) =>
      toRow(
        bucket,
        `${index}:${bucket.value}`,
        bucketLabel(bucket.value, sentinels, t),
        bucket.value === sentinels.none ||
          bucket.value === sentinels.other ||
          bucket.value === sentinels.collapsed,
      ),
    );
    const rest = buckets.slice(MAX_NAMED);
    if (rest.length === 0) {
      return named;
    }
    // The residue is a real row with real numbers, and it sorts LAST however large it is:
    // it is a leftover, not a competitor, and letting it outrank a named tag would be a lie
    // about that tag.
    const leads = rest.reduce((sum, bucket) => sum + bucket.leads.total, 0);
    const deals = rest.reduce((sum, bucket) => sum + bucket.deals.total, 0);
    return [
      ...named,
      {
        key: 'residue',
        label: t('app.utm.moreRows', { count: rest.length }),
        muted: true,
        leads,
        deals,
        rate: conversion(deals, leads),
      },
    ];
  }, [buckets, sentinels, t]);

  const [hover, setHover] = useState<number | null>(null);

  if (rows.length === 0) {
    return <p className="ca-muted text-sm">{t('app.utm.empty')}</p>;
  }

  const peak =
    mode === 'volume'
      ? rows.reduce((max, row) => Math.max(max, row.leads, row.deals), 0)
      : rows.reduce((max, row) => Math.max(max, row.rate ?? 0), 0);

  const share = (value: number): number => (peak > 0 ? value / peak : 0);

  return (
    <div className="ca-viz-plot">
      {/* One legend, and only in the mode that has two series. A legend for a single colour
          teaches the reader that the colour means something, which here it does not. */}
      {mode === 'volume' ? (
        <div className="mb-2 flex flex-wrap items-center gap-3 ca-viz-label">
          {(['lead', 'deal'] as const).map((entity) => (
            <span key={entity} className="inline-flex items-center gap-1.5">
              <span className="ca-viz-swatch" style={{ background: ENTITY_VAR[entity] }} />
              {t(entity === 'lead' ? 'app.utm.leads' : 'app.utm.deals')}
            </span>
          ))}
        </div>
      ) : null}

      <div className="flex flex-col gap-1">
        {rows.map((row, index) => (
          <div
            key={row.key}
            className="grid items-center gap-x-2"
            style={{
              gridTemplateColumns: 'fit-content(38%) minmax(0, 1fr) auto',
              minHeight: ROW_HEIGHT,
            }}
            onMouseEnter={() => setHover(index)}
            onMouseLeave={() => setHover(null)}
          >
            <span
              className="text-[13px] leading-tight"
              style={{
                color: row.muted ? 'var(--ca-viz-muted)' : 'var(--ca-viz-ink)',
                overflowWrap: 'anywhere',
              }}
              title={row.label}
            >
              {row.label}
            </span>

            {mode === 'volume' ? (
              <span className="flex flex-col gap-[2px]" aria-hidden>
                {(['lead', 'deal'] as const).map((entity) => (
                  <span
                    key={entity}
                    style={{
                      display: 'block',
                      height: 8,
                      width: `${Math.max(share(entity === 'lead' ? row.leads : row.deals) * 100, row[entity === 'lead' ? 'leads' : 'deals'] > 0 ? 1.5 : 0)}%`,
                      background: ENTITY_VAR[entity],
                      borderRadius: `0 ${END_RADIUS_PX}px ${END_RADIUS_PX}px 0`,
                      opacity: hover === null || hover === index ? 1 : 0.65,
                    }}
                  />
                ))}
              </span>
            ) : (
              <span
                aria-hidden
                style={{
                  display: 'block',
                  height: 10,
                  width: `${Math.max(share(row.rate ?? 0) * 100, (row.rate ?? 0) > 0 ? 1.5 : 0)}%`,
                  background: 'var(--ca-viz-bar)',
                  borderRadius: `0 ${END_RADIUS_PX}px ${END_RADIUS_PX}px 0`,
                  opacity: hover === null || hover === index ? 1 : 0.65,
                }}
              />
            )}

            <span className="ca-viz-num text-[12px] tabular-nums" style={{ color: 'var(--ca-viz-ink-2)' }}>
              {mode === 'volume'
                ? `${formatCount(row.leads, locale)} / ${formatCount(row.deals, locale)}`
                : percent(row.rate, locale, 1)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default UtmSourceBars;
