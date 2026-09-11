'use client';

/**
 * "By hour": when each person is actually on the phone.
 *
 * The dashboard answers two neighbouring questions and neither of these. Its heatmap knows
 * the clock but not who; its employee chart knows who but not when. This page is the
 * intersection, and the period is read across days rather than summed over them, because
 * "she is busy at ten on Mondays" is not a fact a monthly total can state.
 *
 * Two filters, and both are deliberately narrow. Employees, several at once, because a
 * team lead compares a team; and the period. Direction, result and line are accepted by
 * `GET /hours` for free — it shares `parse_filters` with everything else — but they are not
 * offered here: this page is about time spent, and a grid with six controls over it is a
 * grid nobody reads.
 *
 * The empty employee selection means everyone, which is the same convention `''` has in
 * `DashboardFilters` and the same thing a missing `employee` parameter has always meant to
 * the server. Nothing special is sent for "all".
 */

import { useLocale, useTranslations } from 'next-intl';
import { usePathname } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  ErrorState,
  LoadingBlock,
  PageShell,
  StaleNotice,
} from '@/components/AppFrame';
import {
  defaultFilters,
  rangeForPreset,
  type DashboardFilters,
  type EmployeeOption,
  type FilterOptions,
  EMPTY_FILTER_OPTIONS,
} from '@/components/Filters';
import HourlyTalkTable, {
  HOURLY_CSS,
  type HourRow,
  type HourTotals,
} from '@/components/HourlyTalkTable';
import PageNav, { NAV_CSS } from '@/components/PageNav';
import StateCard from '@/components/StateCard';
import { DateRange, MultiSelect, SegmentedControl, type SelectOption } from '@/components/ui';
import { apiFetch, deniedBodyKey, useMe } from '@/lib/api';
import { fitWindow } from '@/lib/bx24';
import { withExtension } from '@/lib/format';
import { VIZ_CSS } from '@/lib/viz';

/** The response `GET /api/v1/hours` answers with. */
interface HoursResponse {
  rows: HourRow[];
  totals: HourTotals;
  max_cell_seconds: number;
  total_rows: number;
  row_cap: number;
  truncated: boolean;
}

/**
 * Seven days, not the dashboard's thirty.
 *
 * A row here is an employee-day, so a month of a ten-person team is three hundred rows of
 * twenty-four cells. The period control still reaches further; the default is simply the
 * span this page is legible at.
 */
const DEFAULT_PRESET = 'd7' as const;

/** §10 step 5: a custom period is capped at 366 days, client-side as well as server-side. */
const MAX_PERIOD_DAYS = 366;

/** How long to wait after a change before asking Bitrix24 to resize the slider. */
const FIT_DEBOUNCE_MS = 140;

export default function HoursPage() {
  const t = useTranslations();
  const locale = useLocale();
  const me = useMe();
  const pathname = usePathname();

  const [filters, setFilters] = useState<DashboardFilters | null>(null);
  const [employees, setEmployees] = useState<readonly string[]>([]);
  const [options, setOptions] = useState<FilterOptions>(EMPTY_FILTER_OPTIONS);

  const timezone = me.data?.timezone ?? null;
  const access = me.data?.access ?? null;
  const canRead = access === 'all' || access === 'own';

  // The default period is seven days *in the viewer's zone*, so it cannot be computed
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

  const grid = useHours(canRead ? filters : null, employees);

  // §4.10: the page height changes when the grid does, so the slider is re-measured.
  const fitTimer = useRef<number | undefined>(undefined);
  useEffect(() => {
    window.clearTimeout(fitTimer.current);
    fitTimer.current = window.setTimeout(() => void fitWindow(), FIT_DEBOUNCE_MS);
    return () => window.clearTimeout(fitTimer.current);
  }, [grid.data, grid.pending, me.data]);

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
    (row: HourRow): string => {
      if (row.unassigned) {
        return t('app.dashboard.employees.unassigned');
      }
      return (
        withExtension(row.name, row.phone_inner) ??
        t('app.dashboard.filter.unknownEmployee', { id: String(row.employee_id ?? 0) })
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

  const data = grid.data;

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: `${VIZ_CSS}${NAV_CSS}${HOURLY_CSS}` }} />
      <PageShell
        // Twenty-six columns: the reading-width cap would make this scroll sideways on a
        // screen with room to spare.
        wide
        title={t('app.hours.title')}
        subtitle={t('app.hours.subtitle')}
        nav={
          <PageNav
            current={pathname}
            label={t('app.nav.label')}
            items={[
              { href: '/dashboard', label: t('app.nav.dashboard') },
              { href: '/hours', label: t('app.nav.hours') },
            ]}
          />
        }
        banner={me.data.access === 'own' ? <span>{t('app.ownScopeBanner')}</span> : undefined}
      >
        {filters ? (
          <div className={grid.pending && data ? 'ca-viz-dim' : undefined}>
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
                      maxSpanDays={MAX_PERIOD_DAYS}
                    />
                  </div>
                ) : null}
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

        {grid.error && data ? <StaleNotice error={grid.error} onRetry={grid.reload} /> : null}

        {!data ? (
          grid.error ? (
            <ErrorState error={grid.error} onRetry={grid.reload} />
          ) : (
            <LoadingBlock label={t('app.loading')} />
          )
        ) : data.rows.length === 0 ? (
          <p className="ca-card ca-empty">{t('app.hours.empty')}</p>
        ) : (
          <section className="ca-card ca-calls" aria-busy={grid.pending}>
            <div className={grid.pending ? 'ca-viz-dim' : undefined}>
              <HourlyTalkTable
                rows={data.rows}
                totals={data.totals}
                maxCellSeconds={data.max_cell_seconds}
                locale={locale}
                t={t}
                nameOf={nameOf}
              />
            </div>
            {data.truncated ? (
              <p className="ca-hours-note" role="status">
                {t('app.hours.truncated', { shown: data.rows.length, total: data.total_rows })}
              </p>
            ) : null}
          </section>
        )}
      </PageShell>
    </>
  );
}

// --- data --------------------------------------------------------------------------------

interface HoursResource {
  data: HoursResponse | null;
  error: unknown;
  /** A request is in flight. The previous grid stays on screen while it is. */
  pending: boolean;
  reload: () => void;
}

/**
 * `GET /hours` for a filter set, stale-while-refetching.
 *
 * The previous answer is deliberately not cleared when the filters change: §4.11 asks for a
 * page that never goes blank, and inside a slider a disappearing panel also costs a resize
 * round trip. Only a first load or a hard failure has nothing to show.
 */
function useHours(
  filters: DashboardFilters | null,
  employees: readonly string[],
): HoursResource {
  const [data, setData] = useState<HoursResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const query = useMemo(() => {
    if (!filters) {
      return null;
    }
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
    apiFetch<HoursResponse>(`/hours?${query}`, { signal: controller.signal })
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
