'use client';

/**
 * UTM analytics: which advertising tags brought the leads and the deals.
 *
 * The fourth left-menu page, and the only one that answers a marketing question. The
 * dashboard knows how many calls were made; the by-hour grid knows when; the deals page
 * knows what happened to the work. None of them knows where the work came FROM.
 *
 * It is the deals page's twin - a live CRM read that stores nothing, POSTs the viewer's own
 * Bitrix24 token, and can be refused - so everything §4.12 says about those three properties
 * applies here unchanged.
 *
 * ---------------------------------------------------------------------------------
 * **One table, after the owner's own spreadsheet (simplified 2026-09-26).** Period and
 * employee sit on top and cost a read; the tag the rows are grouped by sits on the table
 * and costs nothing, because the response already carries every combination row and the
 * fold is arithmetic in the browser. The tiles, the charts and the five tag filters this
 * page used to carry are gone; the response still carries what they drew.
 * ---------------------------------------------------------------------------------
 *
 * Everything else is the deals page's skeleton, deliberately unchanged: the `useMe` gate,
 * the three terminal states, stale-while-refetching with `ca-viz-dim` + `aria-busy`, and the
 * debounced `fitWindow` (§4.10, §4.11).
 *
 * On a portal promoted to the CRM mirror (§4.14) the viewers `/me.crm.read` names get the
 * same report by GET from Postgres: no token, no scan, 366 days.
 */

import { useLocale, useTranslations } from 'next-intl';
import { usePathname } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ErrorState, LoadingBlock, PageShell, StaleNotice } from '@/components/AppFrame';
import CrmCoverageNotice from '@/components/CrmCoverageNotice';
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
import UtmTable, { UTM_CSS } from '@/components/UtmTable';
import { DateRange, MultiSelect, SegmentedControl, type SelectOption } from '@/components/ui';
import { ApiError, apiFetch, deniedBodyKey, useMe } from '@/lib/api';
import { fitWindow } from '@/lib/bx24';
import { viewerAccessToken } from '@/lib/calls';
import { useCrmCensus } from '@/lib/crmScope';
import { withExtension } from '@/lib/format';
import {
  DIMENSIONS,
  dimensionKey,
  foldBy,
  totalOf,
  type Dimension,
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

/** The rows the table opens with: the owner's spreadsheet is one row per campaign. */
const DEFAULT_GROUP: Dimension = 'utm_campaign';

/** Mirrors `UTM_REPORT_MAX_PERIOD_DAYS`; the server refuses anything longer. */
const MAX_UTM_PERIOD_DAYS = 92;

/** Mirrors `MAX_PERIOD_DAYS`, which a report from the CRM mirror shares with the call pages. */
const MIRROR_MAX_PERIOD_DAYS = 366;

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
  // Free: not in the fetch effect's dependency list, so switching it only re-folds.
  const [groupBy, setGroupBy] = useState<Dimension>(DEFAULT_GROUP);

  const timezone = me.data?.timezone ?? 'UTC';
  // D-7: with CRM analytics turned off the report is closed, so nothing is fetched for it.
  const crmOff = me.data?.crm?.analytics_enabled === false;
  // §4.14: the server decides which path this viewer takes; the page only follows it.
  const mirror = me.data?.crm?.read === 'mirror';
  const census = useCrmCensus(me);

  useEffect(() => {
    if (!me.data || filters || crmOff) {
      return;
    }
    const base = defaultFilters(timezone);
    const range = rangeForPreset(DEFAULT_PRESET, timezone);
    setFilters({ ...base, preset: DEFAULT_PRESET, from: range.from, to: range.to });
  }, [crmOff, filters, me.data, timezone]);

  useEffect(() => {
    if (!me.data || me.data.access === 'denied' || crmOff) {
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
  }, [crmOff, me.data]);

  const report = useUtm(census.running ? null : filters, employees, mirror);
  const data = report.data;

  // The portal's report path changed under an open page: `/me` is read again once, exactly as
  // the deals page does.
  const reread = useRef(false);
  const reloadMe = me.reload;
  useEffect(() => {
    const changed = report.error instanceof ApiError && report.error.code === 'crm_mirror_unavailable';
    if (changed && !reread.current) {
      reread.current = true;
      reloadMe();
    }
  }, [reloadMe, report.error]);

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
  }, [data, groupBy, report.pending]);

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

  /** The table, re-folded from the rows already in memory. */
  const view = useMemo(() => {
    if (!data) {
      return null;
    }
    const buckets = foldBy(data.combinations, data.dimensions, groupBy);
    return { buckets, totals: totalOf(buckets) };
  }, [data, groupBy]);

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
  if (crmOff) {
    return (
      <StateCard kind="crm_off" title={t('state.crm_off.title')} body={t('state.crm_off.body')} />
    );
  }

  const scan = data?.scan;

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: `${VIZ_CSS}${NAV_CSS}${UTM_CSS}` }} />
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
            settingsLabel={me.data.is_admin ? t('app.nav.settings') : undefined}
          />
        }
        banner={
          <span>
            {mirror && me.data.access !== 'all'
              ? t('app.utm.scopeGranted')
              : t('app.utm.scopeNote')}
          </span>
        }
      >
        {filters ? (
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
                  maxSpanDays={mirror ? MIRROR_MAX_PERIOD_DAYS : MAX_UTM_PERIOD_DAYS}
                />
              </div>
            ) : null}
            {/* Shown to anyone who can see somebody else's records: every administrator,
                and anyone an administrator granted a scope to. */}
            {me.data.access === 'all' || mirror ? (
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
        ) : null}

        {data ? (
          <CrmCoverageNotice
            report={data}
            locale={locale}
            timeZone={timezone}
            t={t}
            className="ca-utm-note"
          />
        ) : null}
        {report.error && data ? <StaleNotice error={report.error} onRetry={report.reload} /> : null}

        {!data || !view ? (
          report.error ? (
            <ErrorState error={report.error} onRetry={report.reload} />
          ) : (
            <LoadingBlock label={t('app.loading')} />
          )
        ) : (
          <div className="flex flex-col gap-3" aria-busy={report.pending}>
            <section
              className={`ca-card min-w-0 px-4 py-4 sm:px-6 sm:py-5${report.pending ? ' ca-viz-dim' : ''}`}
            >
              <div className="mb-3">
                <SegmentedControl<Dimension>
                  label={t('app.utm.groupBy')}
                  value={groupBy}
                  onChange={setGroupBy}
                  options={DIMENSIONS.filter((dimension) => data.dimensions.includes(dimension)).map(
                    (dimension) => ({ value: dimension, label: t(`app.utm.short.${dimension}`) }),
                  )}
                  className="min-w-0 max-w-full"
                />
              </div>
              <UtmTable
                buckets={view.buckets}
                totals={view.totals}
                sentinels={data.buckets}
                dimensionLabel={t(`app.utm.short.${groupBy}`)}
                dimensionTitle={t(dimensionKey(groupBy))}
                leadsAvailable={data.scan.leads.available}
                locale={locale}
                t={t}
              />
            </section>

            {/* The sentences without which this table is misread. The second explains a
                column that is only drawn when the portal has leads. */}
            <p className="ca-utm-note" role="note">
              {t('app.utm.tableNote')}
              {data.scan.leads.available ? ` ${t('app.utm.ratioNote')}` : null}
            </p>

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
 * on nothing else, so switching the grouping tag re-renders a `useMemo` instead of spending
 * a live CRM scan.
 */
function useUtm(
  filters: DashboardFilters | null,
  employees: readonly string[],
  mirror: boolean,
): UtmResource {
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
      if (mirror) {
        // §4.14: answered from Postgres. There is no token to fetch and none to send.
        return await apiFetch<UtmResponse>(`/utm?${query}`, { signal: controller.signal });
      }
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
  }, [attempt, mirror, query]);

  return {
    data,
    error,
    pending,
    reload: useCallback(() => setAttempt((value) => value + 1), []),
  };
}
