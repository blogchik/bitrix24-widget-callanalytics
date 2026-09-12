'use client';

/**
 * Deals by operator: what happened to the work, per funnel, over a period.
 *
 * The other two left-menu pages answer questions about the phone. The dashboard knows how
 * many calls were made and by whom; the by-hour grid knows when. Neither knows whether any
 * of it turned into anything. This page reads the portal's own CRM pipeline and puts one
 * operator on a row and one of that funnel's stages in each column.
 *
 * ---------------------------------------------------------------------------------
 * **Three things here are unlike every other page in this app, and all three follow from
 * the same fact: this page reads Bitrix24 live, and nothing it shows is stored.**
 *
 * 1. **It POSTs.** The body carries the viewer's own Bitrix24 access token, obtained from
 *    `BX24.getAuth()`. `principal.access` is a TELEPHONY verdict and cannot authorise a CRM
 *    read, so the server borrows Bitrix24's own answer by asking as the viewer — the same
 *    bargain the CRM detail tab strikes. The token must never enter a URL, hence a body,
 *    hence POST. `viewer_token_required` is the code the player already uses for this and
 *    the hook below answers it exactly as `InlinePlayer` does.
 * 2. **It can be refused.** A period the portal is too busy to scan inside the browser's
 *    thirty-second timeout is a 400 carrying the deal count, the cap and the period length,
 *    and the page states all three so the reader knows how far to narrow. A partial report
 *    was rejected deliberately: its Итого row would be wrong and nothing on screen would
 *    say so.
 * 3. **Its period control stops at 92 days** while the neighbouring pages offer 366. A live
 *    REST scan cannot honestly promise a year, and a control that offered one would spend
 *    twenty seconds to refuse.
 * ---------------------------------------------------------------------------------
 *
 * Everything else is the by-hour page's skeleton, deliberately unchanged: the `useMe` gate,
 * the three terminal states, stale-while-refetching with `ca-viz-dim` + `aria-busy`, and
 * the debounced `fitWindow` (§4.10, §4.11).
 */

import { useLocale, useTranslations } from 'next-intl';
import { usePathname } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ErrorState, LoadingBlock, PageShell, StaleNotice } from '@/components/AppFrame';
import DealStageTable, {
  DEAL_CSS,
  DealSummaryStrip,
  type DealGroup,
  type DealMeasures,
  type DealRow,
  type StageColumn,
} from '@/components/DealStageTable';
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
import { DateRange, MultiSelect, SegmentedControl, type SelectOption } from '@/components/ui';
import { ApiError, apiFetch, deniedBodyKey, useMe } from '@/lib/api';
import { fitWindow } from '@/lib/bx24';
import { viewerAccessToken } from '@/lib/calls';
import { withExtension } from '@/lib/format';
import { VIZ_CSS } from '@/lib/viz';

/** The response `POST /api/v1/deals` answers with. Mirrors `services/deal_stats.py`. */
interface DealsResponse {
  range: { from: string; to: string; days: number; timezone: string; preset: string };
  filters: { employees: number[] };
  stages: StageColumn[];
  groups: DealGroup[];
  totals: DealMeasures;
  scan: {
    deals: number;
    deals_total: number;
    deal_cap: number;
    stage_dictionary_failed: number[];
    stage_dictionary_truncated: number[];
    funnels_hidden: number;
    list_dialect: string;
    dictionary_dialect: string;
    rest_requests: number;
    from_cache: boolean;
  };
}

/**
 * Thirty days, not the dashboard's default and not this page's own maximum.
 *
 * A live scan costs REST on every open, so the default period is the one most likely to
 * answer in one round trip rather than the widest one the control allows.
 */
const DEFAULT_PRESET = 'd30' as const;

/** Mirrors `DEAL_REPORT_MAX_PERIOD_DAYS`; the server refuses anything longer. */
const MAX_DEAL_PERIOD_DAYS = 92;

/** How long to wait after a change before asking Bitrix24 to resize the slider. */
const FIT_DEBOUNCE_MS = 140;

export default function DealsPage() {
  const t = useTranslations();
  const locale = useLocale();
  const me = useMe();
  const pathname = usePathname();

  const [filters, setFilters] = useState<DashboardFilters | null>(null);
  const [employees, setEmployees] = useState<readonly string[]>([]);
  const [options, setOptions] = useState<FilterOptions>(EMPTY_FILTER_OPTIONS);
  const [showPercent, setShowPercent] = useState(false);

  const timezone = me.data?.timezone ?? null;
  const access = me.data?.access ?? null;
  const canRead = access === 'all' || access === 'own';

  // The default period is thirty days *in the viewer's zone*, so it cannot be computed
  // before `GET /me` has answered with that zone.
  useEffect(() => {
    if (!canRead) {
      return;
    }
    setFilters((current) => {
      if (current) {
        return current;
      }
      const base = defaultFilters(timezone);
      const range = rangeForPreset(DEFAULT_PRESET, timezone);
      return { ...base, preset: DEFAULT_PRESET, from: range.from, to: range.to };
    });
  }, [canRead, timezone]);

  // The employee vocabulary is a separate, cheap read whose failure must not take the page
  // down: without it the control simply offers "All" (§4.11 — never a blank).
  useEffect(() => {
    if (!canRead) {
      return;
    }
    const controller = new AbortController();
    apiFetch<FilterOptions>('/filters', { signal: controller.signal })
      .then((value) =>
        setOptions({
          employees: Array.isArray(value?.employees) ? value.employees : [],
          lines: Array.isArray(value?.lines) ? value.lines : [],
        }),
      )
      .catch(() => setOptions(EMPTY_FILTER_OPTIONS));
    return () => controller.abort();
  }, [canRead]);

  const report = useDeals(canRead ? filters : null, employees);

  // §4.10: the page height changes when the tables do, so the slider is re-measured.
  const fitTimer = useRef<number | undefined>(undefined);
  useEffect(() => {
    window.clearTimeout(fitTimer.current);
    fitTimer.current = window.setTimeout(() => void fitWindow(), FIT_DEBOUNCE_MS);
    return () => window.clearTimeout(fitTimer.current);
  }, [report.data, report.pending, me.data, showPercent]);

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

  const nameOf = useCallback(
    (row: DealRow): string => {
      if (row.user_id === null) {
        return t('app.deals.unassigned');
      }
      return (
        withExtension(row.name, row.phone_inner) ??
        t('app.dashboard.filter.unknownEmployee', { id: String(row.user_id) })
      );
    },
    [t],
  );

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

  const data = report.data;
  const scan = data?.scan;

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: `${VIZ_CSS}${NAV_CSS}${DEAL_CSS}` }} />
      <PageShell
        // One block per funnel, each of them a grid: the reading-width cap would make them
        // scroll sideways on a screen with room to spare.
        wide
        title={t('app.deals.title')}
        subtitle={t('app.deals.subtitle')}
        nav={
          <PageNav
            current={pathname}
            label={t('app.nav.label')}
            items={[
              { href: '/dashboard', label: t('app.nav.dashboard') },
              { href: '/hours', label: t('app.nav.hours') },
              { href: '/deals', label: t('app.nav.deals') },
            ]}
          />
        }
        // Not `app.ownScopeBanner`: "you see only your own calls" is wrong on its face on a
        // page about deals. Every viewer is told the same true thing instead — what they
        // see is what Bitrix24 lets them see.
        banner={<span>{t('app.deals.scopeNote')}</span>}
      >
        {filters ? (
          <div className={report.pending && data ? 'ca-viz-dim' : undefined}>
            <div className="flex flex-col gap-3">
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
                      // Tighter than the other pages on purpose; see the docblock.
                      maxSpanDays={MAX_DEAL_PERIOD_DAYS}
                    />
                  </div>
                ) : null}
                <label className="ca-deals-toggle">
                  <input
                    type="checkbox"
                    checked={showPercent}
                    onChange={(event) => setShowPercent(event.target.checked)}
                  />
                  <span>{t('app.deals.showPercent')}</span>
                </label>
              </div>

              {me.data.access === 'all' ? (
                <div className="grid items-end gap-3 [grid-template-columns:repeat(auto-fit,minmax(220px,1fr))]">
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
          </div>
        ) : null}

        {report.error && data ? <StaleNotice error={report.error} onRetry={report.reload} /> : null}

        {!data ? (
          report.error ? (
            <ErrorState error={report.error} onRetry={report.reload} />
          ) : (
            <LoadingBlock label={t('app.loading')} />
          )
        ) : data.groups.length === 0 ? (
          <p className="ca-card ca-empty">{t('app.deals.empty')}</p>
        ) : (
          <div className="flex flex-col gap-4 sm:gap-5" aria-busy={report.pending}>
            <div className={report.pending ? 'ca-viz-dim' : undefined}>
              <div className="flex flex-col gap-4 sm:gap-5">
                {data.groups.map((group) => (
                  <DealStageTable
                    key={group.category_id ?? 'unknown'}
                    group={group}
                    stages={data.stages}
                    locale={locale}
                    t={t}
                    nameOf={nameOf}
                    showPercent={showPercent}
                  />
                ))}
                <DealSummaryStrip totals={data.totals} locale={locale} t={t} />
              </div>
            </div>

            {/* The most likely misreading of the whole report, answered where it happens.
                Every count is where deals sit TODAY among those that touched the period,
                not where they sat during it. Without this sentence a lead reads a month's
                "in progress" as a month's backlog and does not find out for months. */}
            <p className="ca-deals-note" role="note">
              {t('app.deals.currentStageNote')}
            </p>

            {scan && scan.funnels_hidden > 0 ? (
              <p className="ca-deals-note" role="status">
                {t('app.deals.funnelsHidden', { count: scan.funnels_hidden })}
              </p>
            ) : null}
            {scan && scan.stage_dictionary_failed.length > 0 ? (
              <p className="ca-deals-note" role="status">
                {t('app.deals.stageDictionaryFailed', { count: scan.stage_dictionary_failed.length })}
              </p>
            ) : null}
            {scan && scan.stage_dictionary_truncated.length > 0 ? (
              <p className="ca-deals-note" role="status">
                {t('app.deals.stageDictionaryTruncated', {
                  count: scan.stage_dictionary_truncated.length,
                })}
              </p>
            ) : null}
          </div>
        )}
      </PageShell>
    </>
  );
}

// --- data --------------------------------------------------------------------------------

interface DealsResource {
  data: DealsResponse | null;
  error: unknown;
  /** A request is in flight. The previous tables stay on screen while it is. */
  pending: boolean;
  reload: () => void;
}

/**
 * `POST /deals` for a filter set, stale-while-refetching.
 *
 * Two differences from `useHours`, both forced by this endpoint reading Bitrix24 live.
 *
 * It POSTs the viewer's token, and it fetches that token BEFORE the first attempt rather
 * than after a 409: the server needs one on every request, so waiting to be asked would
 * make every single report cost two round trips. `viewer_token_required` coming back
 * anyway means the token we held had expired, and the one retry re-reads it — that is
 * `refreshAuth()`'s job inside `viewerAccessToken`, so the retry is a plain second attempt.
 *
 * The previous answer is deliberately not cleared while refetching: §4.11 asks for a page
 * that never goes blank, and inside a slider a disappearing panel also costs a resize
 * round trip.
 */
function useDeals(filters: DashboardFilters | null, employees: readonly string[]): DealsResource {
  const [data, setData] = useState<DealsResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const query = useMemo(() => {
    if (!filters) {
      return null;
    }
    // `period: 'custom'` always, with explicit dates: without it the server silently
    // answers its own default period and the page would describe a range nobody picked.
    const params = new URLSearchParams({
      period: 'custom',
      from: filters.from,
      to: filters.to,
    });
    // Repeated, not comma-joined: `parse_filters` reads repeated parameters, and a list
    // encoded into one value would arrive as a single unparsable id.
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

    const run = async (): Promise<DealsResponse> => {
      const token = await viewerAccessToken();
      if (!token) {
        // Outside a Bitrix24 frame, or the SDK refused. There is no report to build and
        // no retry that would change that, so the page says so rather than spinning.
        throw new ApiError('viewer_token_required', 409);
      }
      try {
        return await apiFetch<DealsResponse>(`/deals?${query}`, {
          method: 'POST',
          body: { access_token: token },
          signal: controller.signal,
        });
      } catch (cause) {
        const expired = cause instanceof ApiError && cause.code === 'viewer_token_required';
        if (!expired || controller.signal.aborted) {
          throw cause;
        }
        // One retry, and only this one: `viewerAccessToken` runs `refreshAuth()` when the
        // cached pair is stale, so the second attempt carries a different credential. A
        // second failure is a real answer and is shown.
        const fresh = await viewerAccessToken();
        if (!fresh) {
          throw cause;
        }
        return await apiFetch<DealsResponse>(`/deals?${query}`, {
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
