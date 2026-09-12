'use client';

/**
 * The left-menu view: the dashboard (`PLACEMENT` `DEFAULT` / `LEFT_MENU`, §4.4, §4.11).
 *
 * One `GET /api/v1/dashboard` call serves the whole page - summary tiles, calls per day,
 * hour x weekday, per employee - because all four are one `GROUPING SETS` aggregation
 * over one filtered set (§2 `services/stats.py`), and four round trips would let four
 * panels disagree with each other while a filter is being changed.
 *
 * The behaviours that make it read as a Bitrix24 section rather than a third-party page:
 *
 *  * **A filter change refetches with the previous data left visible and dimmed.** A
 *    full-page spinner inside a slider collapses the frame, `fitWindow()` shrinks it,
 *    and the next paint jumps it back open. Keeping the marks on screen keeps the
 *    height stable and the change legible.
 *  * **`BX24.fitWindow()` after the data lands, debounced** (§4.10). `AppFrame` already
 *    watches the document; this adds the one beat after the numbers arrive, when the
 *    page height actually changes.
 *  * **Every terminal condition is a rendered, translated state** (§4.11): `denied` gets
 *    the mandated sentence, an empty period says "No calls in this period" in words, and
 *    a failed fetch with stale data on screen explains itself without discarding it.
 *  * **`acc='own'` hides the employee filter and shows the mandated banner** (§4.7); the
 *    scope itself is pinned server-side by `scope_filter`, never here.
 */

import { useLocale, useTranslations } from 'next-intl';
import { usePathname } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  ErrorState,
  LoadingBlock,
  PageShell,
  Section,
  StaleNotice,
} from '@/components/AppFrame';
import CallsPerDayChart, { type DayBucket } from '@/components/CallsPerDayChart';
import CallsTable from '@/components/CallsTable';
import EmployeeBars, { type EmployeeBucket } from '@/components/EmployeeBars';
import PageNav, { NAV_CSS } from '@/components/PageNav';
import Filters, {
  defaultFilters,
  toQuery,
  type DashboardFilters,
  type FilterOptions,
  EMPTY_FILTER_OPTIONS,
} from '@/components/Filters';
import HourWeekdayHeatmap, { type HourCell } from '@/components/HourWeekdayHeatmap';
import StateCard from '@/components/StateCard';
import SummaryCards, { type DashboardSummary } from '@/components/SummaryCards';
import SyncBanner from '@/components/SyncBanner';
import { ApiError, CODE_SERVER, apiFetch, deniedBodyKey, presentError, useMe } from '@/lib/api';
import { fitWindow } from '@/lib/bx24';
import { VIZ_CSS } from '@/lib/viz';

/** The period the server actually aggregated, after its own capping (§10 step 5). */
export interface DashboardRange {
  from: string;
  to: string;
  /** Inclusive length in days; the previous-period comparison uses the same length. */
  days: number;
}

/**
 * `GET /api/v1/dashboard` (`api/app/api/dashboard.py`).
 *
 * One response, four shapes, all aggregated in the viewer's timezone and all already
 * scoped by `scope_filter` (§4.7) - the browser never narrows call data itself.
 */
export interface DashboardResponse {
  range: DashboardRange;
  summary: DashboardSummary;
  /** One entry per day that had calls; missing days are drawn as zero, not skipped. */
  per_day: DayBucket[];
  /** `weekday` is ISO (1 = Monday), `hour` is 0-23, both in the viewer's timezone. */
  hour_weekday: HourCell[];
  /** Top employees plus, when there are more, one aggregated `other` row. */
  per_employee: EmployeeBucket[];
}

/** How long to wait after a change before asking Bitrix24 to resize the slider. */
const FIT_DEBOUNCE_MS = 140;

export default function DashboardPage() {
  const t = useTranslations();
  const locale = useLocale();
  const me = useMe();
  const pathname = usePathname();

  const [filters, setFilters] = useState<DashboardFilters | null>(null);
  const [options, setOptions] = useState<FilterOptions>(EMPTY_FILTER_OPTIONS);

  const timezone = me.data?.timezone ?? null;
  const access = me.data?.access ?? null;
  const canRead = access === 'all' || access === 'own';

  // The default period is "the last 30 days" *in the viewer's zone*, so it cannot be
  // computed before `GET /me` has answered with that zone.
  useEffect(() => {
    if (canRead) {
      setFilters((current) => current ?? defaultFilters(timezone));
    }
  }, [canRead, timezone]);

  // The filter vocabulary is a separate, cacheable read and its failure must not take
  // the page down: without it the selects simply offer "All" (§4.11 - never a blank).
  useEffect(() => {
    if (!canRead) {
      return;
    }
    const controller = new AbortController();
    apiFetch<FilterOptions>('/filters', { signal: controller.signal })
      .then((value) => {
        setOptions({
          employees: Array.isArray(value?.employees) ? value.employees : [],
          lines: Array.isArray(value?.lines) ? value.lines : [],
        });
      })
      .catch(() => setOptions(EMPTY_FILTER_OPTIONS));
    return () => controller.abort();
  }, [canRead]);

  const dashboard = useDashboard(canRead ? filters : null);

  // §4.10: after the numbers land the page height changes, so the slider is re-measured.
  const fitTimer = useRef<number | undefined>(undefined);
  useEffect(() => {
    window.clearTimeout(fitTimer.current);
    fitTimer.current = window.setTimeout(() => void fitWindow(), FIT_DEBOUNCE_MS);
    return () => window.clearTimeout(fitTimer.current);
  }, [dashboard.data, dashboard.pending, filters?.preset, me.data]);

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

  const data = dashboard.data;

  return (
    <>
      <style dangerouslySetInnerHTML={{ __html: `${VIZ_CSS}${NAV_CSS}` }} />
      <PageShell
        // The same column as the by-hour page: both are grids, and a reading-width cap
        // left the charts and the call table narrower than the screen they had.
        wide
        title={t('app.dashboard.title')}
        subtitle={data ? rangeLabel(data.range, locale) : undefined}
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
        banner={me.data.access === 'own' ? <span>{t('app.ownScopeBanner')}</span> : undefined}
      >
        <SyncBanner sync={me.data.sync} isAdmin={me.data.is_admin} locale={locale} />

        {filters ? (
          <Filters
            value={filters}
            onChange={setFilters}
            options={options}
            showEmployee={me.data.access === 'all'}
            timeZone={timezone}
            busy={dashboard.pending && data !== null}
          />
        ) : null}

        {dashboard.error && data ? (
          <StaleNotice error={dashboard.error} onRetry={dashboard.reload} />
        ) : null}

        {!data ? (
          dashboard.error ? (
            <ErrorState error={dashboard.error} onRetry={dashboard.reload} />
          ) : (
            <LoadingBlock label={t('app.loading')} />
          )
        ) : (
          <div
            className={dashboard.pending ? 'ca-viz-dim' : undefined}
            aria-busy={dashboard.pending}
          >
            {data.summary.total === 0 ? (
              <EmptyPeriod importing={Boolean(me.data.sync?.importing)} />
            ) : (
              <div className="flex flex-col gap-5">
                <SummaryCards summary={data.summary} locale={locale} />

                <Section title={t('app.dashboard.perDay.title')}>
                  <CallsPerDayChart
                    days={data.per_day}
                    from={data.range.from}
                    to={data.range.to}
                    locale={locale}
                  />
                </Section>

                <div
                  className="grid gap-5"
                  // `min(340px, 100%)` and not a bare 340px: a bare track floor is a floor
                  // the grid will honour even when the column is narrower than it, so at
                  // 320px this pushed the page 36px wider than the viewport and the whole
                  // dashboard scrolled sideways. With `min()` the floor gives way once it
                  // no longer fits, which is the only width at which it was ever wrong.
                  style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(min(340px, 100%), 1fr))' }}
                >
                  <Section title={t('app.dashboard.heatmap.title')}>
                    <HourWeekdayHeatmap cells={data.hour_weekday} locale={locale} />
                  </Section>

                  <Section title={t('app.dashboard.employees.title')}>
                    <EmployeeBars employees={data.per_employee} locale={locale} />
                  </Section>
                </div>

                {/*
                  The call table is part of the v1 scope, and it is also the table view the
                  chart palette depends on: one of the series colours sits below 3:1 against
                  a white surface, and the relief for that is a readable table of the same
                  numbers. It is therefore never behind a toggle that defaults to off.
                */}
                <CallsTable
                  query={{ ...filters, period: 'custom' }}
                  timezone={me.data.timezone}
                  showEmployee={me.data.access === 'all'}
                  importing={Boolean(me.data.sync?.importing)}
                />
              </div>
            )}
          </div>
        )}
      </PageShell>
    </>
  );
}

// --- data ------------------------------------------------------------------------------

interface DashboardResource {
  data: DashboardResponse | null;
  error: unknown;
  /** A request is in flight. The previous `data` stays on screen while it is. */
  pending: boolean;
  reload: () => void;
}

/**
 * `GET /dashboard` for a filter set, stale-while-refetching.
 *
 * The previous response is deliberately *not* cleared when the filters change: §4.11
 * asks for a page that never goes blank, and inside a slider a disappearing panel also
 * costs a resize round trip. `pending` drives the dimming; only a first load or a hard
 * failure has nothing to show.
 */
function useDashboard(filters: DashboardFilters | null): DashboardResource {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [attempt, setAttempt] = useState(0);

  const query = filters ? toQuery(filters) : null;

  useEffect(() => {
    if (query === null) {
      return;
    }
    const controller = new AbortController();
    setPending(true);
    setError(null);
    apiFetch<DashboardResponse>(`/dashboard?${query}`, { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) {
          return;
        }
        // A body that does not carry the four shapes is a contract failure, not data:
        // rendering half of it would put a blank panel on screen, which §4.11 forbids.
        if (!isDashboardResponse(value)) {
          setError(new ApiError(CODE_SERVER, 200));
          return;
        }
        setData(value);
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
  }, [query, attempt]);

  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  return { data, error, pending, reload };
}

/** Does this body carry all four shapes the page renders? */
function isDashboardResponse(value: unknown): value is DashboardResponse {
  if (!value || typeof value !== 'object') {
    return false;
  }
  const body = value as Record<string, unknown>;
  const range = body.range as Record<string, unknown> | undefined;
  const summary = body.summary as Record<string, unknown> | undefined;
  return (
    typeof range?.from === 'string' &&
    typeof range?.to === 'string' &&
    typeof summary?.total === 'number' &&
    Array.isArray(body.per_day) &&
    Array.isArray(body.hour_weekday) &&
    Array.isArray(body.per_employee)
  );
}

// --- small states ----------------------------------------------------------------------

/**
 * §4.11: an empty period says so, in words.
 *
 * While the backfill is still running the sentence changes, because "no calls" and "not
 * imported yet" are different facts and a moderator opening a fresh install must not be
 * told the first one.
 */
function EmptyPeriod({ importing }: { importing: boolean }) {
  const t = useTranslations();
  return (
    <div className="ca-panel px-6 py-10 text-center">
      <p className="text-[15px] font-medium">{t('app.dashboard.empty.title')}</p>
      <p className="ca-muted mx-auto mt-2 max-w-md text-[13px]">
        {importing ? t('app.dashboard.empty.importing') : t('app.dashboard.empty.body')}
      </p>
    </div>
  );
}

/** A failed refetch while usable numbers are still on screen: explain, offer, keep. */
/** "1 Aug - 7 Sep 2026": the period the server actually aggregated, under the title. */
function rangeLabel(range: DashboardRange, locale: string): string {
  const options: Intl.DateTimeFormatOptions = {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  };
  const format = (iso: string): string => {
    const parsed = Date.parse(`${iso}T00:00:00Z`);
    if (Number.isNaN(parsed)) {
      return iso;
    }
    try {
      return new Intl.DateTimeFormat(locale, options).format(new Date(parsed));
    } catch {
      return iso;
    }
  };
  return range.from === range.to
    ? format(range.from)
    : `${format(range.from)} — ${format(range.to)}`;
}
