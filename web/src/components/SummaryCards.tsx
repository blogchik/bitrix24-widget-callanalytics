'use client';

/**
 * The summary tiles above the charts (§4.11 "Dashboard").
 *
 * These are **stat tiles, not charts**: one big number, a short label, and a comparison
 * to the previous period of the same length. There is no sparkline and no bar inside a
 * tile - the shapes below are already drawn full size further down the page, and a
 * 40-pixel copy of them would only compete with the number it sits next to.
 *
 * Colour rule, from the chart specification: status green `#0ca30c` and red `#d03b3b`
 * may appear **only here**, and never as the only cue. The answered-rate tile is the one
 * that uses them, and it carries an arrow icon and the word ("up" / "down") beside the
 * number, so the direction survives a colourblind viewer, a greyscale print and a
 * screen reader. Every other tile shows its delta in ink.
 *
 * Two things the comparison chip is not allowed to do, because it did both:
 *
 *  * **It never wraps.** "-9 п.п. / снижение / к предыдущему периоду" broken over three
 *    lines is not a chip, it is a paragraph with a pink background - and it wrapped at
 *    1440 as readily as at 375. The chip is now the number and its direction only, on one
 *    line; "к предыдущему периоду" is the chip's `title`, where a phrase that is identical
 *    on all four tiles belongs.
 *  * **It never reports a four-digit percentage.** A portal whose previous period holds
 *    four calls produced "+2 108 % рост", which is arithmetic, not information. Past
 *    {@link MULTIPLIER_FROM}x the change is stated as a multiplier ("×22") and the base it
 *    is measured against moves into the title, so the reader can see for themselves that
 *    the comparison rests on almost nothing.
 */

import { useTranslations } from 'next-intl';

import { formatCount, formatDuration } from '@/lib/format';

/** Totals over one period, as `GET /api/v1/dashboard` returns them. */
export interface PeriodTotals {
  total: number;
  answered: number;
  missed: number;
  not_connected: number;
  /** Sum of `calls.call_duration` over the period, in seconds. */
  talk_seconds: number;
}

export interface DashboardSummary extends PeriodTotals {
  /**
   * The same totals over the immediately preceding window of equal length, or `null`
   * when the portal has no history that far back - in which case the tiles say so
   * rather than inventing a +100%.
   */
  previous: PeriodTotals | null;
}

export interface SummaryCardsProps {
  summary: DashboardSummary;
  locale: string;
}

/** Answered / total, or `null` when there is nothing to divide. */
function answeredRate(totals: PeriodTotals | null): number | null {
  if (!totals || totals.total <= 0) {
    return null;
  }
  return totals.answered / totals.total;
}

/**
 * Mean talk time over *answered* calls.
 *
 * `call_duration` is 0 for an unanswered call (§3), so dividing by `total` would report
 * a portal's missed-call rate as if it were a shorter conversation.
 */
function averageTalk(totals: PeriodTotals | null): number | null {
  if (!totals || totals.answered <= 0) {
    return null;
  }
  return totals.talk_seconds / totals.answered;
}

function percent(value: number, locale: string, digits = 0): string {
  try {
    return new Intl.NumberFormat(locale, {
      style: 'percent',
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  } catch {
    return `${Math.round(value * 100)}%`;
  }
}

function signedPercent(value: number, locale: string): string {
  try {
    return new Intl.NumberFormat(locale, {
      style: 'percent',
      maximumFractionDigits: 0,
      signDisplay: 'exceptZero',
    }).format(value);
  } catch {
    const rounded = Math.round(value * 100);
    return `${rounded > 0 ? '+' : ''}${rounded}%`;
  }
}

function signedPoints(value: number, locale: string): string {
  try {
    return new Intl.NumberFormat(locale, {
      maximumFractionDigits: 1,
      signDisplay: 'exceptZero',
    }).format(value * 100);
  } catch {
    const rounded = Math.round(value * 1000) / 10;
    return `${rounded > 0 ? '+' : ''}${rounded}`;
  }
}

/**
 * Growth at or beyond this ratio is shown as "×N" rather than as a percentage.
 *
 * Ten times over is where a percentage stops being read as a quantity: nobody converts
 * "+2 108 %" back into "twenty-two times as many", and the four digits only advertise
 * that the denominator was tiny. Decline needs no such rule - it is bounded at -100 %.
 */
const MULTIPLIER_FROM = 10;

/** `×22`, `×1,4` - whole once the ratio is large enough for a fraction to be noise. */
function multiplier(ratio: number, locale: string): string {
  const digits = ratio >= MULTIPLIER_FROM ? 0 : 1;
  try {
    return `×${new Intl.NumberFormat(locale, {
      minimumFractionDigits: 0,
      maximumFractionDigits: digits,
    }).format(ratio)}`;
  } catch {
    return `×${digits === 0 ? Math.round(ratio) : Math.round(ratio * 10) / 10}`;
  }
}

/** The chip is one line at every width, and `max-w-full` keeps it inside its tile. */
const CHIP_CLASS =
  'inline-flex max-w-full items-center gap-1.5 whitespace-nowrap rounded-full text-[12px]';

type Direction = 'up' | 'down' | 'flat';

function directionOf(delta: number | null): Direction {
  if (delta === null || Math.abs(delta) < 1e-9) {
    return 'flat';
  }
  return delta > 0 ? 'up' : 'down';
}

/** Relative change, or `null` when the previous period cannot supply a denominator. */
function relativeDelta(current: number, previous: number | null | undefined): number | null {
  if (previous === null || previous === undefined || previous <= 0) {
    return null;
  }
  return (current - previous) / previous;
}

/** What one tile says about the period before it. A `null` `text` means "nothing to say". */
interface Comparison {
  direction: Direction;
  text: string | null;
  /** The previous value, already formatted, when the chip had to fall back to a ratio. */
  base?: string;
}

/**
 * A count-like measure against the same measure one period ago.
 *
 * The percentage is the normal answer; past {@link MULTIPLIER_FROM}x it becomes a
 * multiplier, and the base comes back with it so the title can name what the multiple is
 * a multiple *of*.
 */
function compareCounts(
  current: number | null,
  previous: number | null | undefined,
  locale: string,
  formatBase: (value: number) => string = (value) => formatCount(value, locale),
): Comparison {
  if (current === null || previous === null || previous === undefined) {
    return { direction: 'flat', text: null };
  }
  const delta = relativeDelta(current, previous);
  if (delta === null) {
    return { direction: 'flat', text: null };
  }
  const ratio = current / previous;
  if (ratio >= MULTIPLIER_FROM) {
    return { direction: 'up', text: multiplier(ratio, locale), base: formatBase(previous) };
  }
  return { direction: directionOf(delta), text: signedPercent(delta, locale) };
}

export function SummaryCards({ summary, locale }: SummaryCardsProps) {
  const t = useTranslations();

  const previous = summary.previous;
  const rate = answeredRate(summary);
  const previousRate = answeredRate(previous);
  const talk = averageTalk(summary);
  const previousTalk = averageTalk(previous);

  const ratePoints = rate !== null && previousRate !== null ? rate - previousRate : null;

  // One column on a phone, two from `sm`, four from `lg`. `auto-fit` with a 184px floor
  // produced five cramped columns on a wide slider and a chip that wrapped inside every
  // one of them; a stated column count is what keeps the chip on one line at every width.
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Tile
        label={t('app.dashboard.summary.total')}
        value={formatCount(summary.total, locale)}
        comparison={compareCounts(summary.total, previous?.total, locale)}
      />

      <Tile
        label={t('app.dashboard.summary.answeredRate')}
        value={rate === null ? '—' : percent(rate, locale)}
        hint={t('app.dashboard.summary.answeredOf', {
          answered: formatCount(summary.answered, locale),
          total: formatCount(summary.total, locale),
        })}
        // Points, never a percentage of a percentage: a rate that moved from 4 % to 8 %
        // did not grow by 100 %, it grew by four points, and only one of those two
        // sentences survives being read quickly. This is also the one tile allowed the
        // status hues, so its chip carries the arrow and the word beside the number.
        comparison={{
          direction: directionOf(ratePoints),
          text:
            ratePoints === null
              ? null
              : t('app.dashboard.summary.points', { value: signedPoints(ratePoints, locale) }),
        }}
        tone="status"
      />

      <Tile
        label={t('app.dashboard.summary.missed')}
        value={formatCount(summary.missed, locale)}
        hint={summary.total > 0 ? percent(summary.missed / summary.total, locale) : undefined}
        comparison={compareCounts(summary.missed, previous?.missed, locale)}
      />

      <Tile
        label={t('app.dashboard.summary.avgTalk')}
        value={talk === null ? '—' : formatDuration(Math.round(talk))}
        hint={t('app.dashboard.summary.talkTotal', {
          duration: formatDuration(summary.talk_seconds),
        })}
        comparison={compareCounts(talk, previousTalk, locale, (seconds) =>
          formatDuration(Math.round(seconds)),
        )}
      />
    </div>
  );
}

function Tile({
  label,
  value,
  hint,
  comparison,
  tone = 'neutral',
}: {
  label: string;
  value: string;
  hint?: string;
  comparison: Comparison;
  tone?: 'neutral' | 'status';
}) {
  return (
    // `min-w-0` so a long grouped number shrinks the tile's contents rather than its grid
    // track; `mt-auto` on the chip puts all four chips on one baseline even though only
    // three of the tiles carry a hint line above it.
    <div className="ca-panel flex min-w-0 flex-col px-4 py-3.5">
      <div className="ca-viz-label">{label}</div>
      <div
        className="ca-viz-num mt-1 font-semibold leading-none"
        style={{ fontSize: 27, color: 'var(--ca-viz-ink)' }}
      >
        {value}
      </div>
      {hint ? <div className="ca-viz-num ca-muted mt-1.5 text-[12px]">{hint}</div> : null}
      <div className="mt-auto pt-2">
        <Delta comparison={comparison} tone={tone} />
      </div>
    </div>
  );
}

/**
 * The comparison line: an arrow, a number, and the word for the direction. One line.
 *
 * `tone="status"` adds the permitted green/red - as a soft pill and the arrow's fill,
 * with the number and the word left in ink so the sentence stays legible at 12px
 * regardless of how the two hues score against the surface.
 *
 * What the chip does *not* carry is "к предыдущему периоду". It is the same six words on
 * every tile, it is what a comparison chip means anyway, and it was what forced the chip
 * onto a third line. It is the `title` instead - together with the previous value itself
 * whenever the change was large enough to be shown as a multiplier.
 */
function Delta({
  comparison,
  tone = 'neutral',
}: {
  comparison: Comparison;
  tone?: 'neutral' | 'status';
}) {
  const t = useTranslations();
  const { direction, text, base } = comparison;

  if (text === null) {
    return <span className="ca-muted text-[12px]">{t('app.dashboard.summary.noPrevious')}</span>;
  }

  const statusVar = direction === 'up' ? 'var(--ca-viz-up)' : 'var(--ca-viz-down)';
  const colored = tone === 'status' && direction !== 'flat';
  const vsPrevious = t('app.dashboard.summary.vsPrevious');

  return (
    <span
      className={CHIP_CLASS}
      title={base === undefined ? vsPrevious : `${vsPrevious}: ${base}`}
      style={{
        padding: colored ? '2px 8px' : undefined,
        background: colored
          ? direction === 'up'
            ? 'var(--ca-viz-up-soft)'
            : 'var(--ca-viz-down-soft)'
          : undefined,
      }}
    >
      <Arrow direction={direction} color={colored ? statusVar : 'var(--ca-viz-ink-2)'} />
      <span className="ca-viz-num" style={{ color: 'var(--ca-viz-ink)' }}>
        {text}
      </span>
      <span style={{ color: 'var(--ca-viz-ink-2)' }}>
        {t(`app.dashboard.summary.${direction}`)}
      </span>
    </span>
  );
}

/** The icon half of the redundant cue. Never rendered without the word beside it. */
function Arrow({ direction, color }: { direction: Direction; color: string }) {
  const path =
    direction === 'up'
      ? 'M5 1.5 L9 7 L1 7 Z'
      : direction === 'down'
        ? 'M5 8.5 L1 3 L9 3 Z'
        : 'M1 4.4 h8 v1.2 h-8 Z';
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true" focusable="false">
      <path d={path} fill={color} />
    </svg>
  );
}

export default SummaryCards;
