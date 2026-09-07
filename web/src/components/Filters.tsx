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
 *  * **The custom period is capped at `MAX_PERIOD_DAYS` (366)** and clamped here rather
 *    than only server-side, so a moderator dragging a date picker gets a sentence
 *    instead of a 400.
 *  * **`acc='own'` hides the employee filter entirely** (§4.7): the scope is already
 *    pinned server-side by `scope_filter`, and a control that can only ever select the
 *    viewer is a control that suggests the data might be someone else's.
 */

import { useTranslations } from 'next-intl';
import { useId, useMemo, useState, type ChangeEvent } from 'react';

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
}

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

export function Filters({
  value,
  onChange,
  options,
  showEmployee,
  timeZone,
  busy = false,
}: FiltersProps) {
  const t = useTranslations();
  const fieldId = useId();
  const [notice, setNotice] = useState<string | null>(null);

  const today = useMemo(() => todayInZone(timeZone), [timeZone]);

  const selectPreset = (preset: PeriodPreset): void => {
    setNotice(null);
    if (preset === 'custom') {
      onChange({ ...value, preset });
      return;
    }
    const range = rangeForPreset(preset, timeZone);
    onChange({ ...value, preset, from: range.from, to: range.to });
  };

  /**
   * Move one end of a custom period, keeping `[from, to]` ordered and <= 366 days.
   *
   * The end the moderator did *not* touch is the one that gives way, which is the only
   * behaviour that never fights the pointer.
   */
  const moveEnd = (end: 'from' | 'to', raw: string): void => {
    if (!raw || Number.isNaN(dayToUtc(raw))) {
      return;
    }
    let from = end === 'from' ? raw : value.from;
    let to = end === 'to' ? raw : value.to;
    let clamped = false;

    if (dayToUtc(to) < dayToUtc(from)) {
      if (end === 'from') {
        to = from;
      } else {
        from = to;
      }
    }
    if (daysBetween(from, to) > MAX_PERIOD_DAYS) {
      clamped = true;
      if (end === 'from') {
        to = addDays(from, MAX_PERIOD_DAYS - 1);
      } else {
        from = addDays(to, -(MAX_PERIOD_DAYS - 1));
      }
    }
    setNotice(clamped ? t('app.dashboard.period.tooLong', { days: MAX_PERIOD_DAYS }) : null);
    onChange({ ...value, preset: 'custom', from, to });
  };

  const setField = (field: 'employee' | 'direction' | 'result' | 'line') => {
    return (event: ChangeEvent<HTMLSelectElement>): void => {
      onChange({ ...value, [field]: event.target.value });
    };
  };

  const employeeLabel = (employee: EmployeeOption): string => {
    const name = employee.name?.trim();
    const base = name || t('app.dashboard.filter.unknownEmployee', { id: String(employee.id) });
    return employee.active === false
      ? `${base} · ${t('app.dashboard.filter.dismissed')}`
      : base;
  };

  const lineLabel = (line: LineOption): string => {
    if (line.id === null) {
      return t('app.dashboard.filter.builtin');
    }
    const name = line.name?.trim();
    return name || t('app.dashboard.filter.unknownLine', { id: String(line.id) });
  };

  return (
    <div className={busy ? 'ca-viz-dim' : undefined}>
      <div className="flex flex-wrap items-end gap-x-6 gap-y-4">
        <div>
          <span className="ca-viz-label mb-1.5 block">{t('app.dashboard.period.label')}</span>
          <div
            className="inline-flex rounded-lg border"
            style={{ borderColor: 'var(--ca-border)' }}
            role="group"
            aria-label={t('app.dashboard.period.label')}
          >
            {PRESETS.map((preset, index) => {
              const active = value.preset === preset;
              return (
                <button
                  key={preset}
                  type="button"
                  aria-pressed={active}
                  onClick={() => selectPreset(preset)}
                  className="px-3 py-[7px] text-[13px] font-medium"
                  style={{
                    minHeight: 32,
                    background: active ? 'var(--ca-accent-soft)' : 'transparent',
                    color: active ? 'var(--ca-accent)' : 'var(--ca-muted)',
                    borderLeft: index === 0 ? 'none' : '1px solid var(--ca-border)',
                    borderRadius:
                      index === 0
                        ? '7px 0 0 7px'
                        : index === PRESETS.length - 1
                          ? '0 7px 7px 0'
                          : undefined,
                  }}
                >
                  {t(`app.dashboard.period.${preset}`)}
                </button>
              );
            })}
          </div>
        </div>

        {value.preset === 'custom' ? (
          <>
            <DateField
              id={`${fieldId}-from`}
              label={t('app.dashboard.period.from')}
              value={value.from}
              max={today}
              onChange={(raw) => moveEnd('from', raw)}
            />
            <DateField
              id={`${fieldId}-to`}
              label={t('app.dashboard.period.to')}
              value={value.to}
              max={today}
              onChange={(raw) => moveEnd('to', raw)}
            />
          </>
        ) : null}

        {showEmployee ? (
          <SelectField
            id={`${fieldId}-employee`}
            label={t('app.dashboard.filter.employee')}
            value={value.employee}
            onChange={setField('employee')}
            allLabel={t('app.dashboard.filter.all')}
            items={options.employees.map((employee) => ({
              value: String(employee.id),
              label: employeeLabel(employee),
            }))}
          />
        ) : null}

        <SelectField
          id={`${fieldId}-direction`}
          label={t('app.dashboard.filter.direction')}
          value={value.direction}
          onChange={setField('direction')}
          allLabel={t('app.dashboard.filter.all')}
          items={DIRECTION_VALUES.map((code) => ({
            value: code,
            label: t(`call.direction.${code}`),
          }))}
        />

        <SelectField
          id={`${fieldId}-result`}
          label={t('app.dashboard.filter.result')}
          value={value.result}
          onChange={setField('result')}
          allLabel={t('app.dashboard.filter.all')}
          items={RESULT_GROUPS.map((group) => ({
            value: group,
            label: t(`app.dashboard.series.${group}`),
          }))}
        />

        <SelectField
          id={`${fieldId}-line`}
          label={t('app.dashboard.filter.line')}
          value={value.line}
          onChange={setField('line')}
          allLabel={t('app.dashboard.filter.all')}
          items={options.lines.map((line) => ({
            value: line.id === null ? BUILTIN_LINE : String(line.id),
            label: lineLabel(line),
          }))}
        />

        {hasNarrowingFilter(value) ? (
          <button
            type="button"
            className="ca-button ca-button-quiet"
            style={{ minHeight: 32 }}
            onClick={() =>
              onChange({ ...value, employee: '', direction: '', result: '', line: '' })
            }
          >
            {t('app.dashboard.filter.reset')}
          </button>
        ) : null}
      </div>

      {notice ? (
        <p className="ca-muted mt-2 text-[12px]" role="status">
          {notice}
        </p>
      ) : null}
    </div>
  );
}

interface SelectItem {
  value: string;
  label: string;
}

/**
 * A native `<select>`.
 *
 * Native on purpose: it inherits the portal's own font, it opens above the iframe edge
 * instead of being clipped by the slider, and it is keyboard- and screen-reader-correct
 * without a line of code. A hand-rolled listbox inside a frame we do not size is how a
 * dropdown ends up unreachable.
 */
function SelectField({
  id,
  label,
  value,
  onChange,
  allLabel,
  items,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (event: ChangeEvent<HTMLSelectElement>) => void;
  allLabel: string;
  items: SelectItem[];
}) {
  return (
    <div className="min-w-0">
      <label className="ca-viz-label mb-1.5 block" htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={onChange}
        className="rounded-lg px-2.5 text-[13px]"
        style={{
          minHeight: 32,
          maxWidth: 220,
          background: 'var(--ca-surface)',
          color: 'var(--ca-text)',
          border: '1px solid var(--ca-border)',
        }}
      >
        <option value="">{allLabel}</option>
        {items.map((item) => (
          <option key={item.value} value={item.value}>
            {item.label}
          </option>
        ))}
      </select>
    </div>
  );
}

function DateField({
  id,
  label,
  value,
  max,
  onChange,
}: {
  id: string;
  label: string;
  value: string;
  max: string;
  onChange: (raw: string) => void;
}) {
  return (
    <div>
      <label className="ca-viz-label mb-1.5 block" htmlFor={id}>
        {label}
      </label>
      <input
        id={id}
        type="date"
        value={value}
        max={max}
        onChange={(event) => onChange(event.target.value)}
        className="ca-viz-num rounded-lg px-2.5 text-[13px]"
        style={{
          minHeight: 32,
          background: 'var(--ca-surface)',
          color: 'var(--ca-text)',
          border: '1px solid var(--ca-border)',
        }}
      />
    </div>
  );
}

export default Filters;
