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
 */

import { useTranslations } from 'next-intl';
import type { ReactNode } from 'react';

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

export function SummaryCards({ summary, locale }: SummaryCardsProps) {
  const t = useTranslations();

  const previous = summary.previous;
  const rate = answeredRate(summary);
  const previousRate = answeredRate(previous);
  const talk = averageTalk(summary);
  const previousTalk = averageTalk(previous);

  const ratePoints = rate !== null && previousRate !== null ? rate - previousRate : null;

  return (
    <div
      className="grid gap-3"
      style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(184px, 1fr))' }}
    >
      <Tile
        label={t('app.dashboard.summary.total')}
        value={formatCount(summary.total, locale)}
        delta={
          <Delta
            direction={directionOf(relativeDelta(summary.total, previous?.total))}
            text={
              relativeDelta(summary.total, previous?.total) === null
                ? null
                : signedPercent(relativeDelta(summary.total, previous?.total) as number, locale)
            }
          />
        }
      />

      <Tile
        label={t('app.dashboard.summary.answeredRate')}
        value={rate === null ? '—' : percent(rate, locale)}
        hint={t('app.dashboard.summary.answeredOf', {
          answered: formatCount(summary.answered, locale),
          total: formatCount(summary.total, locale),
        })}
        delta={
          <Delta
            direction={directionOf(ratePoints)}
            text={ratePoints === null ? null : t('app.dashboard.summary.points', {
              value: signedPoints(ratePoints, locale),
            })}
            tone="status"
          />
        }
      />

      <Tile
        label={t('app.dashboard.summary.missed')}
        value={formatCount(summary.missed, locale)}
        hint={
          summary.total > 0
            ? percent(summary.missed / summary.total, locale)
            : undefined
        }
        delta={
          <Delta
            direction={directionOf(relativeDelta(summary.missed, previous?.missed))}
            text={
              relativeDelta(summary.missed, previous?.missed) === null
                ? null
                : signedPercent(relativeDelta(summary.missed, previous?.missed) as number, locale)
            }
          />
        }
      />

      <Tile
        label={t('app.dashboard.summary.avgTalk')}
        value={talk === null ? '—' : formatDuration(Math.round(talk))}
        hint={t('app.dashboard.summary.talkTotal', {
          duration: formatDuration(summary.talk_seconds),
        })}
        delta={
          <Delta
            direction={directionOf(relativeDelta(talk ?? 0, previousTalk))}
            text={
              talk === null || relativeDelta(talk, previousTalk) === null
                ? null
                : signedPercent(relativeDelta(talk, previousTalk) as number, locale)
            }
          />
        }
      />
    </div>
  );
}

function Tile({
  label,
  value,
  hint,
  delta,
}: {
  label: string;
  value: string;
  hint?: string;
  delta: ReactNode;
}) {
  return (
    <div className="ca-panel px-4 py-3.5">
      <div className="ca-viz-label">{label}</div>
      <div
        className="ca-viz-num mt-1 font-semibold leading-none"
        style={{ fontSize: 27, color: 'var(--ca-viz-ink)' }}
      >
        {value}
      </div>
      {hint ? (
        <div className="ca-viz-num ca-muted mt-1.5 text-[12px]">{hint}</div>
      ) : null}
      <div className="mt-2">{delta}</div>
    </div>
  );
}

/**
 * The comparison line: an arrow, a number, and the word for the direction.
 *
 * `tone="status"` adds the permitted green/red - as a soft pill and the arrow's fill,
 * with the number and the word left in ink so the sentence stays legible at 12px
 * regardless of how the two hues score against the surface.
 */
function Delta({
  direction,
  text,
  tone = 'neutral',
}: {
  direction: Direction;
  text: string | null;
  tone?: 'neutral' | 'status';
}) {
  const t = useTranslations();

  if (text === null) {
    return <span className="ca-muted text-[12px]">{t('app.dashboard.summary.noPrevious')}</span>;
  }

  const statusVar = direction === 'up' ? 'var(--ca-viz-up)' : 'var(--ca-viz-down)';
  const colored = tone === 'status' && direction !== 'flat';

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full text-[12px]"
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
      <span className="ca-muted">{t('app.dashboard.summary.vsPrevious')}</span>
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
