'use client';

/**
 * The custom-period control: one trigger showing the current range, one panel to change it.
 *
 * It replaces the pair of bare `<input type="date">` fields the filter row used to reveal
 * under "Custom". Those render as three different widgets across the browsers a Bitrix24
 * portal is opened in, cannot be styled to match the rest of the row, and - the real
 * problem - show the two ends as two unrelated fields, so nobody can see the period they
 * have actually asked for until the charts redraw.
 *
 * Four things here are load-bearing rather than decorative:
 *
 *  * **Calendar dates, never instants.** Every value in and out is `YYYY-MM-DD` built from
 *    local year/month/day parts (`lib/dates.ts`). The dashboard aggregates in the viewer's
 *    zone (§4.6 `tz`), so a single `toISOString()` here would silently drop a day of calls
 *    off one end of every period for every viewer west of UTC.
 *  * **The server's limits are shown as geometry, not discovered as an error.** Once a
 *    start is chosen the reachable days end at `start + maxSpanDays - 1`, so the 366-day
 *    cap (§10 step 5) is something the user can see; and the calendar is clamped to
 *    `[min, max]` so the range that makes `parse_filters` answer `bad_period` - a date at
 *    the extreme ends of the proleptic calendar, where the previous-period comparison has
 *    no dates to be about - cannot be constructed at all.
 *  * **The panel is a portal, positioned `fixed`.** The filter row sits inside cards and
 *    scroll containers, and a Bitrix24 slider is not a viewport this app controls; an
 *    absolutely-positioned panel would be clipped by the first ancestor with `overflow`.
 *  * **Nothing is applied until Apply.** A picker that closes and refetches on the second
 *    click takes the decision away mid-selection, and inside an iframe the refetch also
 *    resizes the slider under the pointer.
 *
 * It is a member of the control kit, not a lookalike: the trigger is `Field` + `.ca-control`
 * and the panel is `.ca-pop`, so it shares the label/hint/error frame, the height, the
 * border, the shadow and the open/close animation with `Select` and `Input` instead of
 * carrying a second, slightly different version of each. What this file adds on top is only
 * what a calendar needs and a listbox does not.
 *
 * Locale comes from `Intl` alone - month and weekday names, and the range in words - so a
 * third locale is a message bundle, never a change to this file. The week starts on Monday
 * for every locale, because the rest of the dashboard (the hour x weekday heatmap, §4.11)
 * already does.
 */

import { useLocale, useTranslations } from 'next-intl';
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';

import {
  addDays,
  addMonths,
  clampIso,
  endOfMonth,
  fromIso,
  isIsoDate,
  monthGrid,
  startOfMonth,
  todayIso,
  type IsoDate,
} from '@/lib/dates';

import { Field, type FieldControl } from './Field';

// --- public shape ---------------------------------------------------------------------

/** An inclusive calendar period. Both ends are `YYYY-MM-DD` in the viewer's timezone. */
export interface DateRangeValue {
  from: IsoDate;
  to: IsoDate;
}

/**
 * Overrides for the words inside the panel.
 *
 * Every one already has a default from `app.dashboard.period.*` in the shared catalogue
 * (§8), so the common caller passes none of this. Month names, weekday names and the range
 * in words are deliberately absent: they come from `Intl`, and are therefore already right
 * in a locale nobody has added yet. The control's own name is `label`, on the field.
 */
export interface DateRangeLabels {
  /** Dismisses the panel, leaving the applied range untouched. Default `period.cancel`. */
  cancel?: string;
  /** Commits the drafted range. Default `period.apply`. */
  apply?: string;
  /** Previous-month button. Default `period.prevMonth`. */
  previousMonth?: string;
  /** Next-month button. Default `period.nextMonth`. */
  nextMonth?: string;
  /** Shown once a start is chosen and the end is still open. Default `period.pickEnd`. */
  pickEnd?: string;
  /** Shown when the span cap is what limits the reachable days. Default `period.maxDays`. */
  maxSpan?: string;
}

export interface DateRangeProps {
  /** The applied period. The panel drafts a copy and never mutates this. */
  value: DateRangeValue;
  /** Called only from Apply, with an ordered range inside every limit below. */
  onChange: (next: DateRangeValue) => void;
  /** The field's visible name. Defaults to the catalogue's "Period". */
  label?: string;
  /** Standing helper text under the control, as on every other control in the kit. */
  hint?: ReactNode;
  /** A problem to state under the control; its presence marks the trigger invalid. */
  error?: ReactNode;
  /** Optional: every word in the panel already has a default from the catalogue. */
  labels?: DateRangeLabels;
  /** The viewer's IANA zone (§4.6 `tz`); what "today" and the default `max` mean. */
  timeZone?: string | null;
  /** Overrides the app locale; only useful in tests. */
  locale?: string;
  /** Earliest reachable day. Default {@link CALENDAR_FLOOR}. */
  min?: IsoDate;
  /** Latest reachable day. Default: today in `timeZone` - there are no future calls. */
  max?: IsoDate;
  /** Inclusive span cap. Default {@link DEFAULT_MAX_SPAN_DAYS}, the server's `MAX_PERIOD_DAYS`. */
  maxSpanDays?: number;
  disabled?: boolean;
  /** Extra classes on the field wrapper, so the caller's grid can size and place it. */
  className?: string;
  /**
   * Bring your own field.
   *
   * Like `Select` and `Input`, this control renders its own `Field` - that is the kit's
   * convention, and it is what stops a caller shipping a control with no real `<label>`.
   * A caller that has already built the frame itself (a `Field` whose label covers a whole
   * group, say) passes the ids from its render prop instead, and the internal `Field` steps
   * aside rather than producing a second label for the same control.
   */
  id?: string;
  'aria-labelledby'?: string;
  'aria-describedby'?: string;
}

/** The server's `MAX_PERIOD_DAYS` (§10 step 5, §11 assumption 16). */
export const DEFAULT_MAX_SPAN_DAYS = 366;

/**
 * The bottom of the reachable calendar.
 *
 * Not arbitrary caution: `parse_filters` derives a *previous* period of the same length for
 * the summary tiles' comparison, so a range near year 1 overflows and the API answers
 * `bad_period`. Bitrix24 telephony did not exist in 1999 either, so nothing real is lost by
 * making the bottom of the calendar unreachable instead of refusable.
 */
export const CALENDAR_FLOOR: IsoDate = '2000-01-01';

// --- geometry -------------------------------------------------------------------------

/**
 * Widest the panel goes, and the number is derived rather than chosen: two months of seven
 * columns that each clear the 44px touch floor, plus the gutter, the padding and the
 * borders - `2 x (7 x 44) + 20 + 24 + 2`. Round it down and the day cells measure 43.9px
 * under a finger, which is a real (if small) miss and one a target audit reports.
 * Clamped to the viewport, so on a narrow frame it is the frame that decides.
 */
const PANEL_MAX_W = 668;
const PANEL_MIN_W = 268;
/** Tallest the panel goes; past this the month grid scrolls inside it. */
const PANEL_MAX_H = 520;
/** Below this a flip is not worth making; it scrolls instead. */
const PANEL_MIN_H = 220;
/** Gap between trigger and panel, and keep-away from the viewport edge, in px. */
const PANEL_GAP = 6;
const VIEWPORT_EDGE = 8;
/** Belt and braces: if `animationend` never arrives, the exiting panel still unmounts. */
const EXIT_FALLBACK_MS = 600;
/**
 * The one place the two-month breakpoint is written down.
 *
 * It used to be here *and* in a Tailwind `sm:grid-cols-2`, which is a latent bug: move one
 * and the other renders a single month into a two-column grid at half width. The grid now
 * follows the month count this query produces, so there is nothing to keep in step.
 *
 * 684px and not Tailwind's 640px, because the number two months actually need is derived,
 * not chosen: `PANEL_MAX_W` plus the viewport keep-away on both sides. At 640 the panel
 * still fitted, but by squeezing each day column to ~42px - under the touch floor on its
 * horizontal axis, on exactly the devices (small tablets, a narrow slider) that have a
 * finger rather than a cursor. One month at a comfortable size beats two cramped ones.
 */
const WIDE_QUERY = `(min-width: ${PANEL_MAX_W + VIEWPORT_EDGE * 2}px)`;

type Phase = 'closed' | 'open' | 'closing';

interface PanelPosition {
  top: number;
  left: number;
  width: number;
  maxHeight: number;
  placement: 'bottom' | 'top';
}

// --- component ------------------------------------------------------------------------

export function DateRange({
  value,
  onChange,
  label,
  hint,
  error,
  labels: labelOverrides,
  timeZone,
  locale: localeProp,
  min = CALENDAR_FLOOR,
  max,
  maxSpanDays = DEFAULT_MAX_SPAN_DAYS,
  disabled = false,
  className,
  id,
  'aria-labelledby': ariaLabelledBy,
  'aria-describedby': ariaDescribedBy,
}: DateRangeProps) {
  const contextLocale = useLocale();
  const locale = localeProp ?? contextLocale;
  const fallbackId = useId();

  // §8: the words come from the one catalogue, not from this file and not from the caller
  // unless it has a reason. `label` is the "Period" the filter row already uses.
  const t = useTranslations('app.dashboard.period');
  const fieldLabel = label ?? t('label');
  const labels: Required<DateRangeLabels> = {
    cancel: labelOverrides?.cancel ?? t('cancel'),
    apply: labelOverrides?.apply ?? t('apply'),
    previousMonth: labelOverrides?.previousMonth ?? t('prevMonth'),
    nextMonth: labelOverrides?.nextMonth ?? t('nextMonth'),
    pickEnd: labelOverrides?.pickEnd ?? t('pickEnd'),
    maxSpan: labelOverrides?.maxSpan ?? t('maxDays', { days: maxSpanDays }),
  };

  const today = useMemo(() => todayIso(timeZone), [timeZone]);
  const upperLimit = isIsoDate(max) ? max : today;
  const lowerLimit = isIsoDate(min) && min <= upperLimit ? min : CALENDAR_FLOOR;

  // The applied range, made safe: a stale or hand-edited query string reaches this
  // component too, and a control that renders nothing for a bad value is a blank frame.
  const applied = useMemo<DateRangeValue>(() => {
    const from = isIsoDate(value.from) ? clampIso(value.from, lowerLimit, upperLimit) : today;
    const to = isIsoDate(value.to) ? clampIso(value.to, lowerLimit, upperLimit) : from;
    return to < from ? { from: to, to: from } : { from, to };
  }, [value.from, value.to, lowerLimit, upperLimit, today]);

  const [phase, setPhase] = useState<Phase>('closed');
  const [position, setPosition] = useState<PanelPosition | null>(null);
  /** The range being drawn. `to === null` means "a start is chosen, the end is open". */
  const [draft, setDraft] = useState<{ from: IsoDate; to: IsoDate | null }>(applied);
  const [hover, setHover] = useState<IsoDate | null>(null);
  const [view, setView] = useState<{ month: IsoDate; dir: -1 | 0 | 1 }>({
    month: startOfMonth(applied.from),
    dir: 0,
  });
  const [focusDay, setFocusDay] = useState<IsoDate>(applied.from);

  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const footerRef = useRef<HTMLDivElement | null>(null);
  /** Set by the keyboard paths only, so a mouse never has focus yanked to a day cell. */
  const wantsDayFocus = useRef(false);

  const open = phase === 'open';
  const wide = useMediaQuery(WIDE_QUERY);
  const monthsShown = wide ? 2 : 1;

  /**
   * The reachable days, as one interval.
   *
   * It stays contiguous by construction - days *before* the chosen start remain reachable,
   * because clicking one restarts the range rather than being refused - which is what lets
   * keyboard navigation be a clamp instead of a search for the next enabled cell.
   */
  const reachTo =
    draft.to === null ? minIso(upperLimit, addDays(draft.from, maxSpanDays - 1)) : upperLimit;
  const reachFrom = lowerLimit;
  const capBites = draft.to === null && reachTo < upperLimit;

  const isReachable = useCallback(
    (day: IsoDate): boolean => day >= reachFrom && day <= reachTo,
    [reachFrom, reachTo],
  );

  // --- open / close -------------------------------------------------------------------

  const closePanel = useCallback((returnFocus: boolean) => {
    setPhase((current) => (current === 'open' ? 'closing' : current));
    setHover(null);
    if (returnFocus) {
      triggerRef.current?.focus();
    }
  }, []);

  const openPanel = useCallback(() => {
    if (disabled) {
      return;
    }
    setDraft({ from: applied.from, to: applied.to });
    setView({ month: startOfMonth(applied.from), dir: 0 });
    setFocusDay(applied.from);
    setHover(null);
    setPosition(null);
    wantsDayFocus.current = true;
    setPhase('open');
  }, [applied.from, applied.to, disabled]);

  // The exit animation has to finish before the node goes, so the panel stays mounted for
  // one more beat. `data-phase` is read off the DOM rather than off a closure, so a panel
  // reopened mid-exit is not torn down by its own stale animation.
  useEffect(() => {
    if (phase !== 'closing') {
      return;
    }
    const timer = window.setTimeout(() => setPhase('closed'), EXIT_FALLBACK_MS);
    return () => window.clearTimeout(timer);
  }, [phase]);

  const apply = useCallback(() => {
    const from = draft.from;
    const to = draft.to ?? draft.from;
    closePanel(true);
    if (from !== applied.from || to !== applied.to) {
      onChange({ from, to });
    }
  }, [applied.from, applied.to, closePanel, draft.from, draft.to, onChange]);

  // A pointer outside both the trigger and the panel dismisses without applying, which is
  // the same promise Cancel makes. `pointerdown`, not `click`: the panel must be gone
  // before the click lands on whatever is underneath it.
  useEffect(() => {
    if (!open) {
      return;
    }
    const onPointerDown = (event: PointerEvent): void => {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      if (panelRef.current?.contains(target) || triggerRef.current?.contains(target)) {
        return;
      }
      closePanel(false);
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [open, closePanel]);

  // --- placement ----------------------------------------------------------------------

  useLayoutEffect(() => {
    if (phase === 'closed') {
      return;
    }
    const update = (): void => {
      const trigger = triggerRef.current;
      const panel = panelRef.current;
      if (!trigger || !panel) {
        return;
      }
      const rect = trigger.getBoundingClientRect();
      const viewportW = document.documentElement.clientWidth;
      const viewportH = document.documentElement.clientHeight;

      // Scrolled past its own trigger, the panel is a calendar belonging to nothing
      // visible. Pinning it to the viewport edge would be worse than dismissing it.
      if (phase === 'open' && (rect.bottom < 0 || rect.top > viewportH)) {
        closePanel(false);
        return;
      }

      const width = Math.max(PANEL_MIN_W, Math.min(PANEL_MAX_W, viewportW - VIEWPORT_EDGE * 2));
      // Measured off the content rather than off the panel: the panel's own height is
      // already capped by `maxHeight`, so reading it back would ratchet the panel smaller
      // every time this runs. The scroller's `scrollHeight` is what the months really need.
      const natural =
        (scrollRef.current?.scrollHeight ?? 0) + (footerRef.current?.offsetHeight ?? 0) + 2;
      const wanted = Math.min(Math.max(natural, PANEL_MIN_H), PANEL_MAX_H);

      const below = viewportH - rect.bottom - PANEL_GAP - VIEWPORT_EDGE;
      const above = rect.top - PANEL_GAP - VIEWPORT_EDGE;
      // Down unless down does not fit and up fits better: the short iframe is exactly the
      // case where a panel that never flips opens off the bottom edge.
      const placement: PanelPosition['placement'] =
        below >= wanted || below >= above ? 'bottom' : 'top';
      const room = placement === 'bottom' ? below : above;
      const height = Math.min(wanted, Math.max(PANEL_MIN_H, room));

      const rawTop =
        placement === 'bottom' ? rect.bottom + PANEL_GAP : rect.top - PANEL_GAP - height;
      const top = Math.max(VIEWPORT_EDGE, Math.min(rawTop, viewportH - height - VIEWPORT_EDGE));
      const left = Math.max(
        VIEWPORT_EDGE,
        Math.min(rect.left, viewportW - width - VIEWPORT_EDGE),
      );

      setPosition((current) =>
        current &&
        current.top === top &&
        current.left === left &&
        current.width === width &&
        current.maxHeight === height &&
        current.placement === placement
          ? current
          : { top, left, width, maxHeight: height, placement },
      );
    };

    update();
    // `true` so an ancestor scrolling - a card, the page, the slider - moves the panel with
    // its trigger instead of leaving it stranded.
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
    return () => {
      window.removeEventListener('scroll', update, true);
      window.removeEventListener('resize', update);
    };
  }, [closePanel, phase, monthsShown]);

  // --- roving focus -------------------------------------------------------------------

  /**
   * A layout effect keyed on `position`, and not a passive one: until the panel has been
   * placed it is `visibility: hidden`, and a hidden element cannot take focus. Running in
   * the commit that reveals it is what makes the first arrow key land on a day rather than
   * scroll the page.
   */
  useLayoutEffect(() => {
    if (!open || !position || !wantsDayFocus.current) {
      return;
    }
    wantsDayFocus.current = false;
    const cell = panelRef.current?.querySelector<HTMLButtonElement>(
      `[data-day="${cssEscape(focusDay)}"]`,
    );
    // `preventScroll`: the panel is `fixed`, and scrolling the document to reach it would
    // slide the trigger - and therefore the panel - out from under the pointer.
    cell?.focus({ preventScroll: true });
  }, [open, position, focusDay, view.month, monthsShown]);

  /** Bring `day` into the visible months, remembering which way the calendar travelled. */
  const revealMonth = useCallback((day: IsoDate, months: number) => {
    setView((current) => {
      const target = startOfMonth(day);
      const last = months === 2 ? addMonths(current.month, 1) : current.month;
      if (target >= current.month && target <= last) {
        return current;
      }
      const backwards = target < current.month;
      return {
        month: backwards || months === 1 ? target : addMonths(target, -1),
        dir: backwards ? -1 : 1,
      };
    });
  }, []);

  const moveFocus = useCallback(
    (next: IsoDate) => {
      const target = clampIso(next, reachFrom, reachTo);
      wantsDayFocus.current = true;
      setFocusDay(target);
      revealMonth(target, monthsShown);
    },
    [monthsShown, reachFrom, reachTo, revealMonth],
  );

  const pageMonths = useCallback((delta: number) => {
    setView((current) => ({ month: addMonths(current.month, delta), dir: delta < 0 ? -1 : 1 }));
  }, []);

  /** First click sets the start; a second one after it sets the end; anything else restarts. */
  const pick = useCallback(
    (day: IsoDate) => {
      if (!isReachable(day)) {
        return;
      }
      setHover(null);
      setDraft((current) =>
        current.to === null && day >= current.from
          ? { from: current.from, to: day }
          : { from: day, to: null },
      );
      setFocusDay(day);
    },
    [isReachable],
  );

  const onGridKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      const day = focusDay;
      switch (event.key) {
        case 'ArrowLeft':
          moveFocus(addDays(day, -1));
          break;
        case 'ArrowRight':
          moveFocus(addDays(day, 1));
          break;
        case 'ArrowUp':
          moveFocus(addDays(day, -7));
          break;
        case 'ArrowDown':
          moveFocus(addDays(day, 7));
          break;
        case 'PageUp':
          moveFocus(addMonths(day, event.shiftKey ? -12 : -1));
          break;
        case 'PageDown':
          moveFocus(addMonths(day, event.shiftKey ? 12 : 1));
          break;
        case 'Home':
          moveFocus(startOfWeek(day));
          break;
        case 'End':
          moveFocus(addDays(startOfWeek(day), 6));
          break;
        case 'Enter':
        case ' ':
          pick(day);
          break;
        default:
          return;
      }
      // Only reached when the key was one of ours: PageDown must not also scroll the frame,
      // and Space must not activate the button a second time.
      event.preventDefault();
      event.stopPropagation();
    },
    [focusDay, moveFocus, pick],
  );

  // --- formatting (Intl only: a new locale is a bundle, not a change here) -------------

  const dayNameFormat = useMemo(
    () => new Intl.DateTimeFormat(locale, { dateStyle: 'full' }),
    [locale],
  );
  const monthTitleFormat = useMemo(
    () => new Intl.DateTimeFormat(locale, { month: 'long', year: 'numeric' }),
    [locale],
  );
  const weekdays = useMemo(() => weekdayNames(locale), [locale]);

  const monthTitle = useCallback(
    (month: IsoDate): string => {
      const date = fromIso(month);
      return date ? monthTitleFormat.format(date) : month;
    },
    [monthTitleFormat],
  );

  const draftEnd = draft.to ?? draft.from;
  const draftWords = useMemo(
    () => formatRangeWords(locale, draft.from, draftEnd),
    [locale, draft.from, draftEnd],
  );
  const appliedWords = useMemo(
    () => formatRangeWords(locale, applied.from, applied.to),
    [locale, applied.from, applied.to],
  );

  // --- the highlighted band -----------------------------------------------------------

  // While the end is open, hovering *after* the start previews the range that click would
  // select; hovering *before* it previews nothing, because that click restarts the range,
  // and drawing a band the click would not produce is a lie the pointer has to discover.
  const previewEnd = draft.to === null && hover !== null && hover >= draft.from ? hover : null;
  const bandFrom = draft.from;
  const bandTo = draft.to ?? previewEnd ?? draft.from;
  const restartPreview = draft.to === null && hover !== null && hover < draft.from ? hover : null;

  const months = useMemo(
    () => Array.from({ length: monthsShown }, (_, index) => addMonths(view.month, index)),
    [monthsShown, view.month],
  );
  const lastMonth = months[months.length - 1] ?? view.month;
  const prevDisabled = endOfMonth(addMonths(view.month, -1)) < reachFrom;
  const nextDisabled = startOfMonth(addMonths(lastMonth, 1)) > upperLimit;

  const renderControl = (control: FieldControl): ReactNode => (
        <div className="ca-daterange">
          <style href={DATE_RANGE_STYLE_ID} precedence="default">
            {DATE_RANGE_CSS}
          </style>

          <button
            ref={triggerRef}
            type="button"
            id={control.controlId}
            className="ca-control ca-daterange-trigger"
            disabled={disabled}
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-describedby={control.describedBy}
            aria-invalid={control.invalid || undefined}
            onClick={() => (open ? closePanel(true) : openPanel())}
          >
            <CalendarIcon />
            {/*
             * `suppressHydrationWarning` for one specific reason: `Intl` on the Node that
             * server-renders this and `Intl` in the browser that hydrates it ship different
             * ICU versions, and they disagree about the *separator* inside a formatted date
             * range - a thin space here, a plain one there. The dates are identical; without
             * this React calls the subtree a mismatch, logs it and re-renders it on every
             * load, which inside a Bitrix24 slider is a console error a moderator sees
             * (§4.11). The panel's own formatted text needs no such marker: it never
             * server-renders, because it does not exist until the trigger is pressed.
             */}
            <span className="ca-daterange-value" suppressHydrationWarning>
              {appliedWords}
            </span>
            <ChevronIcon direction="down" className="ca-daterange-chevron" />
          </button>

          {phase !== 'closed' && typeof document !== 'undefined'
            ? createPortal(
                <div
                  ref={panelRef}
                  role="dialog"
                  aria-modal={false}
                  aria-labelledby={control.labelId}
                  aria-hidden={phase === 'closing' ? true : undefined}
                  className="ca-pop ca-daterange-pop"
                  data-placement={position?.placement ?? 'bottom'}
                  data-phase={phase}
                  style={{
                    top: position?.top ?? 0,
                    left: position?.left ?? 0,
                    width: position?.width ?? PANEL_MIN_W,
                    maxHeight: position?.maxHeight ?? PANEL_MAX_H,
                    visibility: position ? 'visible' : 'hidden',
                  }}
                  onAnimationEnd={(event) => {
                    if (event.currentTarget.dataset['phase'] === 'closing') {
                      setPhase('closed');
                    }
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'Escape') {
                      event.preventDefault();
                      event.stopPropagation();
                      closePanel(true);
                    }
                  }}
                >
                  <div ref={scrollRef} className="ca-daterange-scroll">
                    <div
                      // Remounting on the month restarts the entry animation; the direction
                      // of travel is what makes it read as movement, not as a flicker.
                      key={view.month}
                      data-dir={view.dir}
                      className={`ca-daterange-months grid gap-x-5 gap-y-4 p-3 ${
                        monthsShown === 2 ? 'grid-cols-2' : 'grid-cols-1'
                      }`}
                      onKeyDown={onGridKeyDown}
                      onMouseLeave={() => setHover(null)}
                    >
                      {months.map((month, index) => {
                        const headingId = `${control.controlId}m${index}`;
                        const isFirst = index === 0;
                        const isLast = index === months.length - 1;
                        return (
                          <div key={month} className="min-w-0">
                            <div className="relative mb-1 flex items-center justify-center">
                              {isFirst ? (
                                <button
                                  type="button"
                                  className="ca-daterange-nav absolute left-0"
                                  disabled={prevDisabled}
                                  aria-label={labels.previousMonth}
                                  onClick={() => pageMonths(-1)}
                                >
                                  <ChevronIcon direction="left" />
                                </button>
                              ) : null}
                              <span id={headingId} className="ca-daterange-title">
                                {monthTitle(month)}
                              </span>
                              {isLast ? (
                                <button
                                  type="button"
                                  className="ca-daterange-nav absolute right-0"
                                  disabled={nextDisabled}
                                  aria-label={labels.nextMonth}
                                  onClick={() => pageMonths(1)}
                                >
                                  <ChevronIcon direction="right" />
                                </button>
                              ) : null}
                            </div>

                            <table
                              role="grid"
                              aria-labelledby={headingId}
                              className="ca-daterange-grid"
                            >
                              <thead>
                                <tr>
                                  {weekdays.map((weekday) => (
                                    <th
                                      key={weekday.long}
                                      scope="col"
                                      abbr={weekday.long}
                                      className="ca-daterange-wd"
                                    >
                                      {weekday.short}
                                    </th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody>
                                {monthGrid(month).map((week, weekIndex) => (
                                  <tr key={weekIndex}>
                                    {week.map((day, slot) => {
                                      if (!day) {
                                        return <td key={slot} className="ca-daterange-cell" />;
                                      }
                                      const inBand = day >= bandFrom && day <= bandTo;
                                      const isStart = day === bandFrom;
                                      const isEnd = day === bandTo;
                                      const unreachable = !isReachable(day);
                                      // The band is one continuous strip across a week, so
                                      // it has to round wherever it stops - at an endpoint,
                                      // or at the edge of the row when it carries on into
                                      // the next one.
                                      const roundStart = isStart || slot === 0 || !week[slot - 1];
                                      const roundEnd = isEnd || slot === 6 || !week[slot + 1];
                                      return (
                                        <td
                                          key={slot}
                                          role="gridcell"
                                          aria-selected={inBand}
                                          className="ca-daterange-cell"
                                        >
                                          <button
                                            type="button"
                                            data-day={day}
                                            data-in={inBand ? '1' : undefined}
                                            data-edge={isStart || isEnd ? '1' : undefined}
                                            data-l={inBand && roundStart ? '1' : undefined}
                                            data-r={inBand && roundEnd ? '1' : undefined}
                                            data-today={day === today ? '1' : undefined}
                                            data-ghost={day === restartPreview ? '1' : undefined}
                                            className="ca-daterange-day"
                                            tabIndex={day === focusDay ? 0 : -1}
                                            disabled={unreachable}
                                            aria-disabled={unreachable || undefined}
                                            aria-current={day === today ? 'date' : undefined}
                                            aria-label={dayLabel(dayNameFormat, day)}
                                            onClick={() => pick(day)}
                                            onFocus={() => setFocusDay(day)}
                                            onMouseEnter={() => setHover(day)}
                                          >
                                            <span className="ca-daterange-box">
                                              {dayNumber(day)}
                                            </span>
                                          </button>
                                        </td>
                                      );
                                    })}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  <div ref={footerRef} className="ca-daterange-footer">
                    <div className="min-w-0">
                      <div className="ca-daterange-summary" aria-live="polite">
                        {draftWords}
                      </div>
                      {draft.to === null ? (
                        <div className="ca-daterange-hint">
                          {capBites ? labels.maxSpan : labels.pickEnd}
                        </div>
                      ) : null}
                    </div>
                    <div className="ca-daterange-actions">
                      <button
                        type="button"
                        className="ca-button ca-button-quiet"
                        onClick={() => closePanel(true)}
                      >
                        {labels.cancel}
                      </button>
                      <button type="button" className="ca-button" onClick={apply}>
                        {labels.apply}
                      </button>
                    </div>
                  </div>
                </div>,
                document.body,
              )
            : null}
        </div>
  );

  // The caller already owns the frame: use its ids and render nothing that would label the
  // control a second time.
  if (ariaLabelledBy) {
    return renderControl({
      controlId: id ?? fallbackId,
      labelId: ariaLabelledBy,
      describedBy: ariaDescribedBy,
      invalid: Boolean(error),
    });
  }

  return (
    <Field label={fieldLabel} hint={hint} error={error} className={className}>
      {renderControl}
    </Field>
  );
}

export default DateRange;

// --- helpers --------------------------------------------------------------------------

function minIso(a: IsoDate, b: IsoDate): IsoDate {
  return a < b ? a : b;
}

/** Monday of `day`'s week - the week bound `Home`/`End` move to. */
function startOfWeek(day: IsoDate): IsoDate {
  const date = fromIso(day);
  return date ? addDays(day, -((date.getDay() + 6) % 7)) : day;
}

/** The day-of-month as it is printed in the cell. Locale-independent digits by design. */
function dayNumber(day: IsoDate): number {
  return Number(day.slice(8, 10));
}

function dayLabel(format: Intl.DateTimeFormat, day: IsoDate): string {
  const date = fromIso(day);
  return date ? format.format(date) : day;
}

/**
 * Monday-first weekday names.
 *
 * Deliberately *not* `Intl.Locale.getWeekInfo()`: this product's week starts on Monday in
 * every locale, because the hour x weekday heatmap beside it already does, and a picker
 * that started on Sunday for `en` would put the same call in a different column.
 *
 * No `timeZone` is passed to the formatters: the anchor below is a local-noon date, and
 * naming it in a *different* zone can be a day out either way.
 */
function weekdayNames(locale: string): { short: string; long: string }[] {
  const shortFormat = new Intl.DateTimeFormat(locale, { weekday: 'short' });
  const longFormat = new Intl.DateTimeFormat(locale, { weekday: 'long' });
  // 2024-01-01 was a Monday.
  const monday = new Date(2024, 0, 1, 12, 0, 0, 0);
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(monday.getTime());
    date.setDate(monday.getDate() + index);
    return { short: shortFormat.format(date), long: longFormat.format(date) };
  });
}

/**
 * The range in words: "9-15 Sept. 2026", collapsed to one date when both ends match.
 *
 * `formatRange` merges the shared parts in a way no join can ("9-15 September 2026", not
 * "9 September 2026 - 15 September 2026"), and it is not everywhere yet, so the fallback is
 * the plain join.
 */
function formatRangeWords(locale: string, from: IsoDate, to: IsoDate): string {
  const start = fromIso(from);
  const end = fromIso(to);
  if (!start || !end) {
    return from === to ? from : `${from} \u2013 ${to}`;
  }
  let format: Intl.DateTimeFormat;
  try {
    format = new Intl.DateTimeFormat(locale, { day: 'numeric', month: 'short', year: 'numeric' });
  } catch {
    format = new Intl.DateTimeFormat(undefined, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
    });
  }
  if (from === to) {
    return format.format(start);
  }
  const withRange = format as Intl.DateTimeFormat & {
    formatRange?: (start: Date, end: Date) => string;
  };
  if (typeof withRange.formatRange === 'function') {
    try {
      return withRange.formatRange(start, end);
    } catch {
      // fall through to the join
    }
  }
  return `${format.format(start)} \u2013 ${format.format(end)}`;
}

/** The ids here are `YYYY-MM-DD`, so this is belt and braces rather than a real escape. */
function cssEscape(value: string): string {
  return value.replace(/["\\]/g, '\\$&');
}

/**
 * A media query as React state.
 *
 * `useSyncExternalStore` rather than an effect: the server snapshot is "narrow", so the
 * first client paint cannot claim two months are on screen before it has measured.
 */
function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (notify: () => void) => {
      if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
        return () => undefined;
      }
      const list = window.matchMedia(query);
      list.addEventListener('change', notify);
      return () => list.removeEventListener('change', notify);
    },
    [query],
  );
  const snapshot = useCallback(
    () =>
      typeof window !== 'undefined' && typeof window.matchMedia === 'function'
        ? window.matchMedia(query).matches
        : false,
    [query],
  );
  return useSyncExternalStore(subscribe, snapshot, () => false);
}

// --- icons (1.5px stroke, `currentColor`) ---------------------------------------------

function CalendarIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className="ca-daterange-icon"
    >
      <rect
        x="3.25"
        y="5.25"
        width="17.5"
        height="15.5"
        rx="2.5"
        stroke="currentColor"
        strokeWidth="1.5"
      />
      <path
        d="M3.5 10h17M8 3.5v3.5M16 3.5v3.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

function ChevronIcon({
  direction,
  className,
}: {
  direction: 'left' | 'right' | 'down';
  className?: string;
}) {
  const path =
    direction === 'left'
      ? 'M14.5 5.5 8 12l6.5 6.5'
      : direction === 'right'
        ? 'M9.5 5.5 16 12l-6.5 6.5'
        : 'M6 9.5 12 15.5l6-6';
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={className}
    >
      <path
        d={path}
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

// --- styles ---------------------------------------------------------------------------

/** React 19 de-duplicates a hoisted `<style>` by this href, however many pickers render. */
const DATE_RANGE_STYLE_ID = 'ca-daterange';

/**
 * Only what a calendar needs on top of the kit.
 *
 * The trigger's box, the panel's chrome and its open/close animation all come from
 * `.ca-control` and `.ca-pop` in `Field.tsx`; what follows is the month grid, which no
 * other control has. Every colour and every duration is a `--ca-*` token, so light/dark and
 * `prefers-reduced-motion` stay solved in one place.
 *
 * The day cell's size is `--ca-control-h` rather than a number, which is what makes it 40px
 * against a mouse and 44px under a finger without a second layout: day cells are the
 * densest targets in the app, and the token already knows which pointer is in use.
 */
const DATE_RANGE_CSS = `
.ca-daterange{position:relative;width:100%;min-width:0;}
.ca-daterange-trigger{justify-content:flex-start;cursor:pointer;}
.ca-daterange-icon{flex:none;color:var(--ca-muted);}
.ca-daterange-value{flex:1 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}
.ca-daterange-chevron{flex:none;color:var(--ca-muted);transition:transform var(--ca-dur) var(--ca-ease),color var(--ca-dur-fast) var(--ca-ease);}
.ca-daterange-trigger[aria-expanded='true']{border-color:var(--ca-accent);}
.ca-daterange-trigger[aria-expanded='true'] .ca-daterange-chevron{transform:rotate(180deg);color:var(--ca-accent);}
.ca-daterange-trigger[aria-expanded='true'] .ca-daterange-icon{color:var(--ca-accent);}

/* A calendar is not a list: no list padding, and the months scroll inside the panel rather
 * than the panel scrolling as a whole, so the footer's Apply never scrolls out of reach. */
.ca-daterange-pop{display:flex;flex-direction:column;padding:0;overflow:hidden;transform-origin:top left;}
.ca-daterange-pop[data-placement='top']{transform-origin:bottom left;}
.ca-daterange-scroll{flex:1 1 auto;min-height:0;overflow-y:auto;overflow-x:hidden;overscroll-behavior:contain;}

@keyframes ca-daterange-next{from{opacity:0;transform:translateX(10px);}to{opacity:1;transform:none;}}
@keyframes ca-daterange-prev{from{opacity:0;transform:translateX(-10px);}to{opacity:1;transform:none;}}
.ca-daterange-months[data-dir='1']{animation:ca-daterange-next var(--ca-dur) var(--ca-ease-out) both;}
.ca-daterange-months[data-dir='-1']{animation:ca-daterange-prev var(--ca-dur) var(--ca-ease-out) both;}

.ca-daterange-title{font-size:13px;font-weight:600;color:var(--ca-text);}
/* Intl gives a lowercase month in Russian; the heading is a heading in both languages. */
.ca-daterange-title::first-letter{text-transform:uppercase;}

.ca-daterange-nav{display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;width:var(--ca-control-h);height:var(--ca-control-h);padding:0;border:0;border-radius:var(--ca-radius);background:transparent;color:var(--ca-muted);cursor:pointer;transition:background-color var(--ca-dur-fast) var(--ca-ease),color var(--ca-dur-fast) var(--ca-ease);}
.ca-daterange-nav:hover:not(:disabled){background:var(--ca-surface-soft);color:var(--ca-text);}
.ca-daterange-nav:disabled{opacity:.35;cursor:default;}

.ca-daterange-grid{width:100%;border-collapse:collapse;table-layout:fixed;}
.ca-daterange-wd{padding:2px 0 6px;font-size:11px;font-weight:500;color:var(--ca-muted);text-align:center;}
.ca-daterange-cell{padding:0;}

/* The hit target is the whole cell and its height is the kit's control height: 40px against
 * a mouse, 44px under a finger. The printed disc stays smaller, so a dense month still
 * reads as a grid rather than as a wall of circles. */
.ca-daterange-day{display:flex;align-items:center;justify-content:center;width:100%;height:var(--ca-control-h);margin:0;padding:0;border:0;background:transparent;color:var(--ca-text);font:inherit;font-size:13px;cursor:pointer;-webkit-tap-highlight-color:transparent;}
.ca-daterange-day[data-in='1']{background:var(--ca-accent-soft);}
.ca-daterange-day[data-l='1']{border-top-left-radius:999px;border-bottom-left-radius:999px;}
.ca-daterange-day[data-r='1']{border-top-right-radius:999px;border-bottom-right-radius:999px;}

.ca-daterange-box{display:flex;align-items:center;justify-content:center;box-sizing:border-box;width:calc(var(--ca-control-h) - 10px);height:calc(var(--ca-control-h) - 10px);border-radius:999px;transition:transform var(--ca-dur-fast) var(--ca-ease),background-color var(--ca-dur-fast) var(--ca-ease),color var(--ca-dur-fast) var(--ca-ease);}
.ca-daterange-day:hover:not(:disabled) .ca-daterange-box{background:var(--ca-border-soft);}
/* The endpoints are a filled disc and the interior is a flat band: the difference is a
 * shape, so it survives both colourblindness and a printed screenshot. */
.ca-daterange-day[data-edge='1'] .ca-daterange-box,
.ca-daterange-day[data-edge='1']:hover .ca-daterange-box{background:var(--ca-accent);color:var(--ca-accent-contrast);font-weight:600;}
.ca-daterange-day[data-today='1']:not([data-edge='1']) .ca-daterange-box{box-shadow:inset 0 0 0 1.5px var(--ca-accent);}
.ca-daterange-day[data-ghost='1'] .ca-daterange-box{box-shadow:inset 0 0 0 1.5px var(--ca-accent);}
/* Press feedback on the disc only, so no neighbour moves. */
.ca-daterange-day:active:not(:disabled) .ca-daterange-box{transform:scale(.86);}
.ca-daterange-day:disabled{color:var(--ca-muted);opacity:.34;cursor:default;}
.ca-daterange-day:focus-visible{outline:none;}
.ca-daterange-day:focus-visible .ca-daterange-box{outline:2px solid var(--ca-accent);outline-offset:2px;}

.ca-daterange-footer{flex:none;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px 12px;padding:10px 12px;border-top:1px solid var(--ca-border-soft);background:var(--ca-surface);}
.ca-daterange-summary{font-size:13px;font-weight:500;color:var(--ca-text);}
.ca-daterange-hint{margin-top:1px;font-size:12px;color:var(--ca-muted);}
.ca-daterange-actions{flex:none;display:flex;align-items:center;gap:8px;}
/* The footer buttons are controls, so they are the kit's control height - 40px against a
 * mouse, 44px under a finger. Shrinking them to look neat is how a primary action ends up
 * below the touch floor. */
.ca-daterange-actions .ca-button{min-height:var(--ca-control-h);}
`;
