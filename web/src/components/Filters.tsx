'use client';

/**
 * The dashboard filter row (§4.11 "Dashboard", §4.7 for the employee filter).
 *
 * It owns the *filter model* - the shape the page turns into one `GET /dashboard` query
 * string - because that model is the contract between the page and every chart below it.
 *
 * Three things here are not cosmetic:
 *
 *  * **Periods are calendar dates in the viewer's timezone**, never in the browser's
 *    (§4.6 `tz` claim; open question 9 resolves "today" to the viewer's zone). All date
 *    maths below happens on `YYYY-MM-DD` strings through UTC midnight, so a portal in
 *    another region reads its own clock and no DST transition can move a day boundary.
 *  * **The custom period is capped at `MAX_PERIOD_DAYS` (366)** on the client as well as
 *    server-side, so the cap is something a moderator can see rather than a 400 they have
 *    to provoke. `ui/DateRange` enforces it as geometry - the days past the cap are simply
 *    not reachable - which is why this file no longer clamps a range after the fact.
 *  * **`acc='own'` hides the employee filter entirely** (§4.7): the scope is already
 *    pinned server-side by `scope_filter`, and a control that can only ever select the
 *    viewer is a control that suggests the data might be someone else's.
 */

import { useTranslations } from 'next-intl';
import { useMemo } from 'react';

import {
  DateRange,
  Field,
  Select,
  SegmentedControl,
  type SegmentedOption,
  type SelectOption,
} from '@/components/ui';
import { RESULT_GROUPS } from '@/lib/viz';

/** §10 step 5: the custom period is capped at 366 days (`MAX_PERIOD_DAYS`). */
export const MAX_PERIOD_DAYS = 366;

/** Documented `CALL_TYPE` values, in the order the filter offers them. */
const DIRECTION_VALUES = ['1', '2', '3', '4', '5'] as const;

/** `line` value standing for "built-in telephony", i.e. `rest_app_id IS NULL`. */
export const BUILTIN_LINE = 'builtin';

export type PeriodPreset = 'today' | 'd7' | 'd30' | 'custom';

/** Every filter the dashboard query carries. `''` always means "no predicate". */
export interface DashboardFilters {
  preset: PeriodPreset;
  /** Inclusive first calendar day, `YYYY-MM-DD` in the viewer's timezone. */
  from: string;
  /** Inclusive last calendar day, `YYYY-MM-DD` in the viewer's timezone. */
  to: string;
  /** `employees.portal_user_id` as a string, or `''` for all. */
  employee: string;
  /** `CALL_TYPE` as a string, or `''` for all. */
  direction: string;
  /** A `calls.result_group` value, or `''` for all. */
  result: string;
  /** `rest_app_id` as a string, {@link BUILTIN_LINE}, or `''` for all. */
  line: string;
}

/** One employee the portal has calls for (`GET /api/v1/filters`). */
export interface EmployeeOption {
  id: number;
  name?: string | null;
  /** Dismissed users keep their calls (§7) and stay selectable, marked as such. */
  active?: boolean | null;
  /** `UF_PHONE_INNER` (§7): the internal extension, shown after the name. */
  phone_inner?: string | null;
}

/**
 * The longest extension this filter will render.
 *
 * A real internal number is two to six characters, but `UF_PHONE_INNER` is a free-text
 * field and some portals put a whole phone number in it. Rendering that would push every
 * row of the dropdown out to its width cap to carry one row's value.
 */
const EXTENSION_MAX_CHARS = 8;

/** One telephony line / integration (`GET /api/v1/filters`). */
export interface LineOption {
  /** `rest_app_id`; `null` is built-in telephony. */
  id: number | null;
  name?: string | null;
}

export interface FilterOptions {
  employees: EmployeeOption[];
  lines: LineOption[];
}

export const EMPTY_FILTER_OPTIONS: FilterOptions = { employees: [], lines: [] };

// --- calendar-date helpers (viewer timezone) ------------------------------------------

const DAY_MS = 86_400_000;

/** Today as `YYYY-MM-DD` in `timeZone`; the browser's zone if that zone is unusable. */
export function todayInZone(timeZone: string | null | undefined): string {
  try {
    // `en-CA` is the one widely available locale whose short date *is* ISO order.
    return new Intl.DateTimeFormat('en-CA', {
      timeZone: timeZone ?? undefined,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date());
  } catch {
    const now = new Date();
    const pad = (value: number) => String(value).padStart(2, '0');
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  }
}

/** A `YYYY-MM-DD` day as UTC midnight, so day maths never meets a DST boundary. */
export function dayToUtc(iso: string): number {
  const parsed = Date.parse(`${iso}T00:00:00Z`);
  return Number.isNaN(parsed) ? Number.NaN : parsed;
}

/** `YYYY-MM-DD` + n days, as `YYYY-MM-DD`. */
export function addDays(iso: string, days: number): string {
  const base = dayToUtc(iso);
  if (Number.isNaN(base)) {
    return iso;
  }
  return new Date(base + days * DAY_MS).toISOString().slice(0, 10);
}

/** Inclusive length of `[from, to]` in days; `0` if either end is unparsable. */
export function daysBetween(from: string, to: string): number {
  const a = dayToUtc(from);
  const b = dayToUtc(to);
  if (Number.isNaN(a) || Number.isNaN(b)) {
    return 0;
  }
  return Math.floor((b - a) / DAY_MS) + 1;
}

/** The `[from, to]` a preset means today, in the viewer's timezone. */
export function rangeForPreset(
  preset: PeriodPreset,
  timeZone: string | null | undefined,
): { from: string; to: string } {
  const today = todayInZone(timeZone);
  switch (preset) {
    case 'today':
      return { from: today, to: today };
    case 'd7':
      return { from: addDays(today, -6), to: today };
    case 'd30':
      return { from: addDays(today, -29), to: today };
    default:
      return { from: addDays(today, -29), to: today };
  }
}

/** The filter set the dashboard opens with: the last 30 days, nothing else applied. */
export function defaultFilters(timeZone: string | null | undefined): DashboardFilters {
  const { from, to } = rangeForPreset('d30', timeZone);
  return { preset: 'd30', from, to, employee: '', direction: '', result: '', line: '' };
}

/** True when anything beyond the period is narrowing the data. */
export function hasNarrowingFilter(filters: DashboardFilters): boolean {
  return Boolean(filters.employee || filters.direction || filters.result || filters.line);
}

/** The `GET /api/v1/dashboard` query string for a filter set. */
export function toQuery(filters: DashboardFilters): string {
  // `period=custom` is required, not decorative: the API reads `from`/`to` only under
  // that preset and otherwise answers its 7-day default. Omitting it makes every
  // period control silently do nothing while the page still renders a plausible chart,
  // which is the worst kind of bug to ship.
  const params = new URLSearchParams({
    period: 'custom',
    from: filters.from,
    to: filters.to,
  });
  if (filters.employee) {
    params.set('employee', filters.employee);
  }
  if (filters.direction) {
    params.set('direction', filters.direction);
  }
  if (filters.result) {
    params.set('result', filters.result);
  }
  if (filters.line) {
    params.set('line', filters.line);
  }
  return params.toString();
}

// --- the control row ------------------------------------------------------------------

export interface FiltersProps {
  value: DashboardFilters;
  onChange: (next: DashboardFilters) => void;
  options: FilterOptions;
  /** `false` for `acc='own'`: the employee control is hidden, not disabled (§4.7). */
  showEmployee: boolean;
  /** The viewer's IANA zone (§4.6 `tz`), which is what "today" means here. */
  timeZone: string | null | undefined;
  /** Dims the row while a refetch is in flight; the controls stay usable. */
  busy?: boolean;
}

const PRESETS: readonly PeriodPreset[] = ['today', 'd7', 'd30', 'custom'];

/**
 * The row is two tiers, not one wrapping line.
 *
 * A single `flex-wrap` row of content-sized controls is what produced the defect this
 * replaces: at 1440px the last control ("Line") wrapped alone onto a second line beside a
 * large empty margin, and at 375px the whole row became a ragged staircase. The period is
 * one decision and the four narrowing filters are another, so they get one tier each, and
 * the narrowing tier is a grid whose columns are computed from the available width - four
 * across on a desktop, one per line on a phone, with no breakpoint list to keep in step.
 */
export function Filters({
  value,
  onChange,
  options,
  showEmployee,
  timeZone,
  busy = false,
}: FiltersProps) {
  const t = useTranslations();

  const selectPreset = (preset: PeriodPreset): void => {
    if (preset === 'custom') {
      onChange({ ...value, preset });
      return;
    }
    const range = rangeForPreset(preset, timeZone);
    onChange({ ...value, preset, from: range.from, to: range.to });
  };

  const allLabel = t('app.dashboard.filter.all');

  const presetOptions = useMemo(
    (): readonly SegmentedOption<PeriodPreset>[] =>
      PRESETS.map((preset) => ({ value: preset, label: t(`app.dashboard.period.${preset}`) })),
    [t],
  );

  /**
   * An employee row carries two things beside the name, and they are in different slots
   * for reasons that are worth stating together, because they look inconsistent.
   *
   * **"Dismissed" is the `hint`.** A dismissed user keeps their calls (§7) and stays
   * selectable, but as a label suffix the word was what made these rows the longest in
   * the list and pushed the control wider than every other control in the row.
   *
   * **The extension is part of the `label`.** In a portal with two Ivanovs the name is
   * not an identifier and the extension is - so it has to survive selection, and the
   * trigger renders the label only: in the `hint` it would vanish the moment the reader
   * picked that employee, leaving a filter whose chosen state says less than its options
   * did. It is also five or six characters against the word's nine or ten, and
   * {@link EXTENSION_MAX_CHARS} keeps it that way, so the width finding above does not
   * repeat here.
   *
   * No extension is offered beside "User #17": two numbers side by side, meaning
   * different things, is worse than one.
   */
  const employeeOptions = useMemo(
    (): readonly SelectOption[] => [
      { value: '', label: allLabel },
      ...options.employees.map((employee) => {
        const name = employee.name?.trim();
        const extension = employee.phone_inner?.trim();
        return {
          value: String(employee.id),
          label: name
            ? extension && extension.length <= EXTENSION_MAX_CHARS
              ? `${name} (${extension})`
              : name
            : t('app.dashboard.filter.unknownEmployee', { id: String(employee.id) }),
          hint:
            employee.active === false ? t('app.dashboard.filter.dismissed') : undefined,
        };
      }),
    ],
    [allLabel, options.employees, t],
  );

  const directionOptions = useMemo(
    (): readonly SelectOption[] => [
      { value: '', label: allLabel },
      ...DIRECTION_VALUES.map((code) => ({ value: code, label: t(`call.direction.${code}`) })),
    ],
    [allLabel, t],
  );

  const resultOptions = useMemo(
    (): readonly SelectOption[] => [
      { value: '', label: allLabel },
      ...RESULT_GROUPS.map((group) => ({
        value: group,
        label: t(`app.dashboard.series.${group}`),
      })),
    ],
    [allLabel, t],
  );

  const lineOptions = useMemo(
    (): readonly SelectOption[] => [
      { value: '', label: allLabel },
      ...options.lines.map((line) => {
        if (line.id === null) {
          return { value: BUILTIN_LINE, label: t('app.dashboard.filter.builtin') };
        }
        const name = line.name?.trim();
        return {
          value: String(line.id),
          label: name || t('app.dashboard.filter.unknownLine', { id: String(line.id) }),
        };
      }),
    ],
    [allLabel, options.lines, t],
  );

  const setField = (field: 'employee' | 'direction' | 'result' | 'line') => {
    return (next: string): void => {
      onChange({ ...value, [field]: next });
    };
  };

  return (
    <div className={busy ? 'ca-viz-dim' : undefined}>
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-end gap-3">
          <SegmentedControl<PeriodPreset>
            label={t('app.dashboard.period.label')}
            value={value.preset}
            onChange={selectPreset}
            options={presetOptions}
            className="min-w-0 max-w-full"
          />

          {value.preset === 'custom' ? (
            // The picker owns the 366-day cap now, and shows it as unreachable days rather
            // than moving the end the moderator just set and explaining it afterwards -
            // which is why the old `tooLong` notice is gone from this row.
            <Field
              group
              label={t('app.dashboard.period.custom')}
              className="min-w-0 flex-1 basis-[240px] sm:max-w-[360px]"
            >
              {(control) => (
                <DateRange
                  id={control.controlId}
                  aria-labelledby={control.labelId}
                  aria-describedby={control.describedBy}
                  value={{ from: value.from, to: value.to }}
                  onChange={(next) =>
                    onChange({ ...value, preset: 'custom', from: next.from, to: next.to })
                  }
                  timeZone={timeZone}
                  maxSpanDays={MAX_PERIOD_DAYS}
                />
              )}
            </Field>
          ) : null}
        </div>

        <div className="grid items-end gap-3 [grid-template-columns:repeat(auto-fit,minmax(176px,1fr))]">
          {showEmployee ? (
            <Select
              label={t('app.dashboard.filter.employee')}
              value={value.employee}
              onChange={setField('employee')}
              options={employeeOptions}
            />
          ) : null}

          <Select
            label={t('app.dashboard.filter.direction')}
            value={value.direction}
            onChange={setField('direction')}
            options={directionOptions}
          />

          <Select
            label={t('app.dashboard.filter.result')}
            value={value.result}
            onChange={setField('result')}
            options={resultOptions}
          />

          <Select
            label={t('app.dashboard.filter.line')}
            value={value.line}
            onChange={setField('line')}
            options={lineOptions}
          />

          {hasNarrowingFilter(value) ? (
            <div className="flex">
              <button
                type="button"
                className="ca-button ca-button-quiet w-full"
                style={{ minHeight: 'var(--ca-control-h)' }}
                onClick={() =>
                  onChange({ ...value, employee: '', direction: '', result: '', line: '' })
                }
              >
                {t('app.dashboard.filter.reset')}
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

export default Filters;
