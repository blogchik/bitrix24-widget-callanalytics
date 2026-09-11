'use client';

/**
 * The panel half of a select-shaped control: where it opens, and when it closes.
 *
 * Extracted from {@link Select} when a second control needed the same behaviour. None of
 * what follows is generic popup logic; every branch is a measurement taken against the
 * frame this app actually runs in, and that is exactly why it must not exist twice:
 *
 *  * **The panel is portalled to `<body>` and fixed-positioned**, so no ancestor's
 *    `overflow` can clip it. It is also why the position has to be computed rather than
 *    inherited - a fixed element knows nothing about the trigger it belongs to.
 *  * **It flips up when down does not fit.** A Bitrix24 slider is short, and a panel that
 *    never flips opens straight off the bottom edge of it.
 *  * **It dismisses itself when the trigger scrolls out of view.** Pinned to a viewport
 *    edge with its trigger gone, the panel is a menu belonging to nothing visible - worse
 *    than no menu.
 *  * **Scroll is listened for with `capture`**, so a card, the page or the slider scrolling
 *    moves the panel with its trigger instead of stranding it.
 *  * **The exit animation finishes before the node goes**, with a timer as the belt to
 *    `animationend`'s braces: if the event never arrives, the panel still unmounts.
 *
 * What the hook deliberately does NOT own is the listbox: the active row, the selection,
 * the keyboard and the typeahead all belong to the control, because a single-select and a
 * multi-select disagree about every one of them.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';

/** Gap between trigger and panel, in px. */
const PANEL_GAP = 6;

/** Keep-away from the viewport edge, in px. */
const VIEWPORT_EDGE = 8;

/** The panel never grows past this, in px, however many employees a portal has. */
export const PANEL_MAX_H = 320;

/** Below this the panel is not worth flipping for; it scrolls instead. */
const PANEL_MIN_H = 96;

/** Belt and braces: if `animationend` never arrives, the exiting panel still unmounts. */
const EXIT_FALLBACK_MS = 600;

export type PopoverPhase = 'closed' | 'open' | 'closing';

export interface PopoverPosition {
  top: number;
  left: number;
  /** The panel is at least as wide as the trigger, and may grow for a long label. */
  minWidth: number;
  maxHeight: number;
  placement: 'bottom' | 'top';
}

export interface PopoverOptions {
  /** A disabled control never opens, however it was asked to. */
  disabled?: boolean;
  onOpenChange?: (open: boolean) => void;
  /**
   * Re-measure when this changes.
   *
   * The panel's height follows its content, so a control whose row count changes while the
   * panel is open has to be measured again or the flip decision is made against the old
   * size. Pass the row count, or anything else that changes with the content.
   */
  measureKey?: unknown;
}

export interface Popover<T extends HTMLElement, P extends HTMLElement> {
  phase: PopoverPhase;
  /** `true` only while fully open: during the exit animation the control is not live. */
  open: boolean;
  position: PopoverPosition | null;
  triggerRef: RefObject<T | null>;
  panelRef: RefObject<P | null>;
  openPanel: () => void;
  /** `returnFocus` sends focus back to the trigger - right for Escape, wrong for a click
   *  elsewhere, which is a click on that other thing. */
  closePanel: (returnFocus: boolean) => void;
  /** Called by the panel's `onAnimationEnd` to drop the node once the exit has played. */
  finishExit: () => void;
  /** Scroll a row into the panel without `scrollIntoView`, which scrolls the page too. */
  revealRow: (node: HTMLElement | null) => void;
}

export function usePopover<T extends HTMLElement, P extends HTMLElement>(
  options: PopoverOptions = {},
): Popover<T, P> {
  const { disabled = false, onOpenChange, measureKey } = options;

  const [phase, setPhase] = useState<PopoverPhase>('closed');
  const [position, setPosition] = useState<PopoverPosition | null>(null);

  const triggerRef = useRef<T | null>(null);
  const panelRef = useRef<P | null>(null);

  const open = phase === 'open';

  const notifyOpen = useCallback(
    (next: boolean) => {
      onOpenChange?.(next);
    },
    [onOpenChange],
  );

  const openPanel = useCallback(() => {
    if (disabled) {
      return;
    }
    // Cleared, not kept: a stale position would paint the panel at the last trigger's
    // coordinates for one frame before the layout effect corrects it.
    setPosition(null);
    setPhase('open');
    notifyOpen(true);
  }, [disabled, notifyOpen]);

  const closePanel = useCallback(
    (returnFocus: boolean) => {
      setPhase((current) => (current === 'open' ? 'closing' : current));
      if (returnFocus) {
        triggerRef.current?.focus();
      }
      notifyOpen(false);
    },
    [notifyOpen],
  );

  const finishExit = useCallback(() => setPhase('closed'), []);

  // The exit animation has to finish before the node goes, so the panel stays mounted for
  // one more beat. `data-phase` is read off the DOM by the caller rather than off a
  // closure, so a panel reopened mid-exit is not torn down by its own stale animation.
  useEffect(() => {
    if (phase !== 'closing') {
      return;
    }
    const timer = window.setTimeout(() => setPhase('closed'), EXIT_FALLBACK_MS);
    return () => window.clearTimeout(timer);
  }, [phase]);

  // --- placement ------------------------------------------------------------------------

  useLayoutEffect(() => {
    if (phase === 'closed') {
      return;
    }
    const update = () => {
      const trigger = triggerRef.current;
      const panel = panelRef.current;
      if (!trigger || !panel) {
        return;
      }
      const rect = trigger.getBoundingClientRect();
      const viewportW = document.documentElement.clientWidth;
      const viewportH = document.documentElement.clientHeight;

      // Scrolled past its own trigger, the panel is a menu belonging to nothing visible.
      // Pinning it to the viewport edge would be worse than dismissing it.
      if (phase === 'open' && (rect.bottom < 0 || rect.top > viewportH)) {
        closePanel(false);
        return;
      }

      const below = viewportH - rect.bottom - PANEL_GAP - VIEWPORT_EDGE;
      const above = rect.top - PANEL_GAP - VIEWPORT_EDGE;
      const wanted = Math.min(panel.scrollHeight + 2, PANEL_MAX_H);

      // Down unless down does not fit and up fits better: the short iframe is exactly the
      // case where a panel that never flips opens off the bottom edge.
      const placement: PopoverPosition['placement'] =
        below >= wanted || below >= above ? 'bottom' : 'top';
      const room = placement === 'bottom' ? below : above;
      const height = Math.min(wanted, Math.max(PANEL_MIN_H, room));

      const width = Math.max(panel.offsetWidth, rect.width);
      const left = Math.max(
        VIEWPORT_EDGE,
        Math.min(rect.left, viewportW - width - VIEWPORT_EDGE),
      );
      const rawTop =
        placement === 'bottom' ? rect.bottom + PANEL_GAP : rect.top - PANEL_GAP - height;
      const top = Math.max(
        VIEWPORT_EDGE,
        Math.min(rawTop, viewportH - height - VIEWPORT_EDGE),
      );

      setPosition((current) => {
        if (
          current &&
          current.top === top &&
          current.left === left &&
          current.minWidth === rect.width &&
          current.maxHeight === height &&
          current.placement === placement
        ) {
          return current;
        }
        return { top, left, minWidth: rect.width, maxHeight: height, placement };
      });
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
  }, [closePanel, phase, measureKey]);

  // --- dismissal ------------------------------------------------------------------------

  useEffect(() => {
    if (!open) {
      return;
    }
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      if (triggerRef.current?.contains(target) || panelRef.current?.contains(target)) {
        return;
      }
      // A click elsewhere is a click on that thing, so focus is left where it landed.
      closePanel(false);
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    return () => document.removeEventListener('pointerdown', onPointerDown, true);
  }, [closePanel, open]);

  // --- keeping the active row visible ---------------------------------------------------

  const revealRow = useCallback((node: HTMLElement | null) => {
    const list = panelRef.current;
    if (!list || !node) {
      return;
    }
    const top = node.offsetTop;
    const bottom = top + node.offsetHeight;
    if (top < list.scrollTop) {
      list.scrollTop = Math.max(0, top - 4);
    } else if (bottom > list.scrollTop + list.clientHeight) {
      list.scrollTop = bottom - list.clientHeight + 4;
    }
  }, []);

  return {
    phase,
    open,
    position,
    triggerRef,
    panelRef,
    openPanel,
    closePanel,
    finishExit,
    revealRow,
  };
}
