'use client';

/**
 * UTM analytics: which advertising tags brought the leads, the deals and the money.
 *
 * The fourth left-menu page, and the only one that answers a marketing question. The
 * dashboard knows how many calls were made; the by-hour grid knows when; the deals page
 * knows what happened to the work. None of them knows where the work came FROM.
 *
 * It is the deals page's twin - a live CRM read that stores nothing, POSTs the viewer's own
 * Bitrix24 token, and can be refused - so everything §4.12 says about those three properties
 * applies here unchanged. What is new is the filter model, and it is the whole design:
 *
 * ---------------------------------------------------------------------------------
 * **Period and employee cost a live scan of two entities. Every tag control costs nothing.**
 *
 * The response carries one row per tag combination, so filtering by source, grouping by
 * campaign, switching the chart's measure and toggling percentages are all folds over data
 * that is already here. The UTM selections are deliberately NOT in the fetch effect's
 * dependency list: changing one leaves the query string byte-identical, the effect does not
 * re-fire, and only the `useMemo` below re-runs. The layout says so too - the two controls
 * that cost a round trip sit in their own row above the ones that do not.
 *
 * The filter OPTIONS come from `facets`, which the server computes before any bucketing and
 * before any filter. So they are exact, and they never shrink when a filter is applied -
 * the classic cross-filter trap, where picking one source collapses the medium list to a
 * single entry and there is no way back.
 * ---------------------------------------------------------------------------------
 *
 * **Why there is no "group by these five" control.** The API takes a `dimensions` parameter
 * and it narrows the FOLD rather than the SCAN - the same pages are fetched either way - so
 * exposing it would put a control that costs a round trip in the row of controls this page
 * promises are free. The grouping the reader actually wants is "show me one tag at a time",
 * which is the `groupBy` segmented control, and that is pure client-side arithmetic.
 *
 * Everything else is the deals page's skeleton, deliberately unchanged: the `useMe` gate,
 * the three terminal states, stale-while-refetching with `ca-viz-dim` + `aria-busy`, and the
 * debounced `fitWindow` (§4.10, §4.11).
 */

import { useLocale, useTranslations } from 'next-intl';
import { usePathname } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ErrorState, LoadingBlock, PageShell, Section, StaleNotice } from '@/components/AppFrame';
import {
  defaultFilters,
  rangeForPreset,
  type DashboardFilters,
  type EmployeeOption,
  type FilterOptions,
  EMPTY_FILTER_OPTIONS,
} from '@/components/Filters';
import PageNav, { NAV_CSS } from '@/components/PageNav';
import StateCard from '@/components/StateCard';
import UtmMatrix, { MATRIX_CSS } from '@/components/UtmMatrix';
import UtmSourceBars, { type BarMode } from '@/components/UtmSourceBars';
import UtmTable, { UTM_CSS, UtmSummaryStrip } from '@/components/UtmTable';
import UtmTrend from '@/components/UtmTrend';
import { DateRange, MultiSelect, SegmentedControl, type SelectOption } from '@/components/ui';
import { ApiError, apiFetch, deniedBodyKey, useMe } from '@/lib/api';
import { fitWindow } from '@/lib/bx24';
import { viewerAccessToken } from '@/lib/calls';
import { withExtension } from '@/lib/format';
import {
  DIMENSIONS,
  bucketLabel,
  crossTab,
  dimensionKey,
  foldBy,
  totalOf,
  type Dimension,
  type Selections,
  type UtmResponse,
} from '@/lib/utm';
import { VIZ_CSS } from '@/lib/viz';

/**
 * Thirty days, not the widest the control allows.
 *
 * A live scan of two entities costs REST on every open, so the default period is the one
 * most likely to answer in one round trip rather than the largest one that would be legal.
 */
const DEFAULT_PRESET = 'd30' as const;

/** Mirrors `UTM_REPORT_MAX_PERIOD_DAYS`; the server refuses anything longer. */
const MAX_UTM_PERIOD_DAYS = 92;

/** How long to wait after a change before asking Bitrix24 to resize the slider. */
const FIT_DEBOUNCE_MS = 140;

export default function UtmPage() {
  const t = useTranslations();
  const locale = useLocale();
  const me = useMe();
  const pathname = usePathname();

  const [filters, setFilters] = useState<DashboardFilters | null>(null);
  const [employees, setEmployees] = useState<readonly string[]>([]);
  const [options, setOptions] = useState<FilterOptions>(EMPTY_FILTER_OPTIONS);

  // Free controls. None of these is in the fetch effect's dependency list.
  const [selections, setSelections] = useState<Selections>({});
  const [groupBy, setGroupBy] = useState<Dimension>('utm_source');
  const [mode, setMode] = useState<BarMode>('volume');
  const [showPercent, setShowPercent] = useState(false);

  const timezone = me.data?.timezone ?? 'UTC';

  useEffect(() => {
    if (!me.data || filters) {
      return;
    }
    const base = defaultFilters(timezone);
    const range = rangeForPreset(DEFAULT_PRESET, timezone);
    setFilters({ ...base, preset: DEFAULT_PRESET, from: range.from, to: range.to });
  }, [filters, me.data, timezone]);

  useEffect(() => {
    if (!me.data || me.data.access === 'denied') {
      return;
    }
    let cancelled = false;
    apiFetch<FilterOptions>('/filters')
      .then((value) => {
        if (!cancelled) {
          setOptions(value);
        }
      })
      // The employee control is a convenience; the page is useful without it and a failure
      // here must not take the report down with it.
      .catch(() => setOptions(EMPTY_FILTER_OPTIONS));
    return () => {
      cancelled = true;
    };
  }, [me.data]);

  const report = useUtm(filters, employees);
  const data = report.data;

  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (timer.current) {
      clearTimeout(timer.current);
    }
    timer.current = setTimeout(fitWindow, FIT_DEBOUNCE_MS);
    return () => {
      if (timer.current) {
        clearTimeout(timer.current);
      }
    };
  }, [data, groupBy, mode, report.pending, selections, showPercent]);

  const employeeOptions = useMemo(
    (): readonly SelectOption[] =>
      options.employees.map((employee: EmployeeOption) => ({
        value: String(employee.id),
        label:
          withExtension(employee.name, employee.phone_inner) ??
          t('app.dashboard.filter.unknownEmployee', { id: String(employee.id) }),
        hint: employee.active === false ? t('app.dashboard.filter.dismissed') : undefined,
      })),
    [options.employees, t],
  );

  /** Every view on the page, recomputed from the rows already in memory. */
  const view = useMemo(() => {
    if (!data) {
      return null;
    }
    const buckets = foldBy(data.combinations, data.dimensions, selections, groupBy);
    const matrix = crossTab(
      data.combinations,
      data.dimensions,
      selections,
      'utm_source',
      'utm_medium',
      (row) => row.leads.total + row.deals.total,
    );
    return { buckets, totals: totalOf(buckets), matrix };
  }, [data, groupBy, selections]);

  if (me.loading) {
    return <LoadingBlock label={t('app.loading')} />;
  }
  if (me.error || !me.data) {
    return <ErrorState error={me.error} onRetry={me.reload} />;
  }
  // §4.7: `denied` is a state with mandated copy, not an error.
  if (me.data.access === 'denied') {
    return (
      <StateCard kind="denied" title={t('state.denied.title')} body={t(deniedBodyKey(me.data))} />
    );
  }

  const scan = data?.scan;
  const groupLabel = t(dimensionKey(groupBy));

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: `${VIZ_CSS}${NAV_CSS}${UTM_CSS}${MATRIX_CSS}` }} />
      <PageShell
        wide
        title={t('app.utm.title')}
        subtitle={t('app.utm.subtitle')}
        nav={
          <PageNav
            current={pathname}
            label={t('app.nav.label')}
            items={[
              { href: '/dashboard', label: t('app.nav.dashboard') },
              { href: '/hours', label: t('app.nav.hours') },
              { href: '/deals', label: t('app.nav.deals') },
              { href: '/utm', label: t('app.nav.utm') },
            ]}
          />
        }
        banner={<span>{t('app.utm.scopeNote')}</span>}
      >
        {filters ? (
          <div className="flex flex-col gap-3">
            {/* Row one: the two controls that cost a live CRM scan. */}
            <div className="flex flex-wrap items-end gap-3">
              <SegmentedControl<'today' | 'd7' | 'd30' | 'custom'>
                label={t('app.dashboard.period.label')}
                value={filters.preset}
                onChange={(preset) => {
                  if (preset === 'custom') {
                    setFilters({ ...filters, preset });
                    return;
                  }
                  const range = rangeForPreset(preset, timezone);
                  setFilters({ ...filters, preset, from: range.from, to: range.to });
                }}
                options={(['today', 'd7', 'd30', 'custom'] as const).map((preset) => ({
                  value: preset,
                  label: t(`app.dashboard.period.${preset}`),
                }))}
                className="min-w-0 max-w-full"
              />
              {filters.preset === 'custom' ? (
                <div className="min-w-0 flex-1 basis-[240px] sm:max-w-[360px]">
                  <DateRange
                    value={{ from: filters.from, to: filters.to }}
                    onChange={(next) =>
                      setFilters({ ...filters, preset: 'custom', from: next.from, to: next.to })
                    }
                    timeZone={timezone}
                    maxSpanDays={MAX_UTM_PERIOD_DAYS}
                  />
                </div>
              ) : null}
              {me.data.access === 'all' ? (
                <div className="min-w-0 flex-1 basis-[220px] sm:max-w-[320px]">
                  <MultiSelect
                    label={t('app.hours.employees')}
                    values={employees}
                    onChange={setEmployees}
                    options={employeeOptions}
                    allLabel={t('app.dashboard.filter.all')}
                    summaryLabel={(count) => t('app.hours.selected', { count })}
                    emptyText={t('app.hours.noEmployees')}
                  />
                </div>
              ) : null}
            </div>

            {/* Row two: the tag filters. Free, and the page says so. */}
            {data ? (
              <>
                <div className="grid items-end gap-3 [grid-template-columns:repeat(auto-fit,minmax(200px,1fr))]">
                  {data.facets.map((facet) => (
                    <MultiSelect
                      key={facet.dimension}
                      label={t(dimensionKey(facet.dimension))}
                      values={selections[facet.dimension] ?? []}
                      onChange={(values) =>
                        setSelections((current) => ({ ...current, [facet.dimension]: values }))
                      }
                      options={facet.values.map((value) => ({
                        value: value.value,
                        label: bucketLabel(value.value, data.buckets, t),
                        hint: t('app.utm.facetHint', {
                          leads: value.leads.total,
                          deals: value.deals.total,
                        }),
                      }))}
                      allLabel={t('app.dashboard.filter.all')}
                      summaryLabel={(count) => t('app.hours.selected', { count })}
                      emptyText={t('app.utm.noValues')}
                      disabled={!facet.selected}
                    />
                  ))}
                </div>
                <p className="ca-utm-note" role="note">
                  {t('app.utm.freeFiltersNote')}
                </p>
              </>
            ) : null}
          </div>
        ) : null}

        {report.error && data ? <StaleNotice error={report.error} onRetry={report.reload} /> : null}

        {!data || !view ? (
          report.error ? (
            <ErrorState error={report.error} onRetry={report.reload} />
          ) : (
            <LoadingBlock label={t('app.loading')} />
          )
        ) : (
          <div className="flex flex-col gap-4 sm:gap-5" aria-busy={report.pending}>
            <div className={report.pending ? 'ca-viz-dim' : undefined}>
              <div className="flex flex-col gap-4 sm:gap-5">
                <Section title={t('app.utm.summary')}>
                  <UtmSummaryStrip
                    totals={view.totals}
                    amounts={data.amounts}
                    locale={locale}
                    t={t}
                  />
                </Section>

                <Section title={t('app.utm.trend')}>
                  <UtmTrend days={data.days} locale={locale} t={t} />
                  <p className="ca-utm-note" role="note">
                    {t('app.utm.trendNote')}
                  </p>
                </Section>

                <div className="grid gap-4 sm:gap-5 [grid-template-columns:repeat(auto-fit,minmax(340px,1fr))]">
                  <Section title={t('app.utm.ranking', { dimension: groupLabel })}>
                    <div className="mb-3 flex flex-wrap items-end gap-3">
                      <SegmentedControl<Dimension>
                        label={t('app.utm.groupBy')}
                        value={groupBy}
                        onChange={setGroupBy}
                        options={DIMENSIONS.filter((dimension) =>
                          data.dimensions.includes(dimension),
                        )
                          .slice(0, 5)
                          .map((dimension) => ({
                            value: dimension,
                            label: t(`app.utm.short.${dimension}`),
                          }))}
                      />
                      <SegmentedControl<BarMode>
                        label={t('app.utm.measure')}
                        value={mode}
                        onChange={setMode}
                        options={[
                          { value: 'volume', label: t('app.utm.measureVolume') },
                          { value: 'conversion', label: t('app.utm.measureConversion') },
                        ]}
                      />
                    </div>
                    <UtmSourceBars
                      buckets={view.buckets}
                      sentinels={data.buckets}
                      mode={mode}
                      locale={locale}
                      t={t}
                    />
                  </Section>

                  <Section title={t('app.utm.matrix')}>
                    <UtmMatrix
                      matrix={view.matrix}
                      sentinels={data.buckets}
                      rowLabel={t('app.utm.short.utm_source')}
                      columnLabel={t('app.utm.short.utm_medium')}
                      locale={locale}
                      t={t}
                    />
                  </Section>
                </div>

                <Section title={t('app.utm.funnel', { dimension: groupLabel })}>
                  <label className="ca-utm-toggle mb-3">
                    <input
                      type="checkbox"
                      checked={showPercent}
                      onChange={(event) => setShowPercent(event.target.checked)}
                    />
                    <span>{t('app.utm.showPercent')}</span>
                  </label>
                  <UtmTable
                    buckets={view.buckets}
                    totals={view.totals}
                    sentinels={data.buckets}
                    amounts={data.amounts}
                    dimensionLabel={groupLabel}
                    locale={locale}
                    t={t}
                    showPercent={showPercent}
                  />
                </Section>
              </div>
            </div>

            {/* The two sentences without which this page is misread, in the order a reader
                meets the numbers they explain. */}
            <p className="ca-utm-note" role="note">
              {t('app.utm.attributionNote')}
            </p>
            <p className="ca-utm-note" role="note">
              {t('app.utm.snapshotNote')}
            </p>
            {data.amounts.trusted ? (
              <p className="ca-utm-note" role="note">
                {t('app.utm.amountNote', { currency: data.amounts.currency })}
              </p>
            ) : (
              <p className="ca-utm-note" role="status">
                {t('app.utm.currencyMixed', { currencies: data.amounts.currencies.join(', ') })}
              </p>
            )}

            {scan && !scan.leads.available ? (
              <p className="ca-utm-note" role="status">
                {t('app.utm.leadsUnavailable')}
              </p>
            ) : null}
            {scan && scan.leads.available && (scan.leads.tagged_rows ?? 0) === 0 && scan.leads.folded > 0 ? (
              <p className="ca-utm-note" role="status">
                {t('app.utm.noTaggedRows', { count: scan.leads.folded })}
              </p>
            ) : null}
            {scan && scan.collapsed.length > 0 ? (
              <p className="ca-utm-note" role="status">
                {t('app.utm.collapsedNote', {
                  dimensions: scan.collapsed.map((name) => t(`app.utm.short.${name}`)).join(', '),
                })}
              </p>
            ) : null}
            {data.facets.some((facet) => facet.distinct_total > facet.distinct_kept) ? (
              <p className="ca-utm-note" role="status">
                {t('app.utm.valuesCapped', { kept: data.scan.value_cap })}
              </p>
            ) : null}
          </div>
        )}
      </PageShell>
    </>
  );
}

// --- data --------------------------------------------------------------------------------

interface UtmResource {
  data: UtmResponse | null;
  error: unknown;
  /** A request is in flight. The previous report stays on screen while it is. */
  pending: boolean;
  reload: () => void;
}

/**
 * `POST /utm` for a period and an employee set, stale-while-refetching.
 *
 * Byte-identical in shape to `useDeals`, including the part that matters most: the viewer's
 * Bitrix24 token is fetched BEFORE the first attempt rather than after a 409, because the
 * server needs one on every request and waiting to be asked would make every report cost two
 * round trips. One retry, and only one: `viewerAccessToken` runs `refreshAuth()` when the
 * cached pair is stale, so the second attempt carries a different credential and a second
 * failure is a real answer.
 *
 * **The dependency list is the feature.** `query` depends on the period and the employees and
 * on nothing else, so every tag control on the page re-renders a `useMemo` instead of
 * spending a live CRM scan.
 */
function useUtm(filters: DashboardFilters | null, employees: readonly string[]): UtmResource {
  const [data, setData] = useState<UtmResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const query = useMemo(() => {
    if (!filters) {
      return null;
    }
    // `period: 'custom'` always, with explicit dates: without it the server silently answers
    // its own default period and the page would describe a range nobody picked.
    const params = new URLSearchParams({
      period: 'custom',
      from: filters.from,
      to: filters.to,
    });
    // Repeated, not comma-joined: `parse_filters` reads repeated parameters.
    for (const id of employees) {
      params.append('employee', id);
    }
    return params.toString();
  }, [employees, filters]);

  useEffect(() => {
    if (query === null) {
      return;
    }
    const controller = new AbortController();
    setPending(true);

    const run = async (): Promise<UtmResponse> => {
      const token = await viewerAccessToken();
      if (!token) {
        // Outside a Bitrix24 frame, or the SDK refused. There is no report to build and no
        // retry that would change that, so the page says so rather than spinning.
        throw new ApiError('viewer_token_required', 409);
      }
      try {
        return await apiFetch<UtmResponse>(`/utm?${query}`, {
          method: 'POST',
          body: { access_token: token },
          signal: controller.signal,
        });
      } catch (cause) {
        const expired = cause instanceof ApiError && cause.code === 'viewer_token_required';
        if (!expired || controller.signal.aborted) {
          throw cause;
        }
        const fresh = await viewerAccessToken();
        if (!fresh) {
          throw cause;
        }
        return await apiFetch<UtmResponse>(`/utm?${query}`, {
          method: 'POST',
          body: { access_token: fresh },
          signal: controller.signal,
        });
      }
    };

    run()
      .then((value) => {
        setData(value);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setError(cause);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setPending(false);
        }
      });
    return () => controller.abort();
  }, [attempt, query]);

  return {
    data,
    error,
    pending,
    reload: useCallback(() => setAttempt((value) => value + 1), []),
  };
}
