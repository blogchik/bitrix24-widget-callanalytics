'use client';

/**
 * A listbox that replaces the native `<select>`.
 *
 * The native element was the right first answer - it is keyboard-correct for free and it
 * escapes the iframe - and it was replaced for reasons that are measured, not aesthetic:
 *
 *  * **It cannot be sized.** A native select is content-sized and 32px tall in this app, so
 *    a filter row of them is a staircase of mismatched boxes and every one of them misses
 *    the 44px touch floor on a phone. `width: 100%` and one shared control height fix that,
 *    and the native element honours neither reliably.
 *  * **It renders as three different controls.** Windows, macOS and Android each draw their
 *    own popup, so the one row a moderator sees never matches the screenshot in the docs.
 *
 * What the native element gave us has to be paid back by hand, and is:
 *
 *  * Full ARIA 1.2 select-only combobox semantics - `role="combobox"` on the trigger with
 *    `aria-expanded` / `aria-controls` / `aria-activedescendant`, `role="listbox"` on the
 *    panel, `role="option"` with `aria-selected` on each row. Focus never leaves the
 *    trigger, which is what makes Tab behave and what keeps the browser from scrolling the
 *    page to a focused option.
 *  * The whole keyboard contract: Enter / Space / ArrowDown / ArrowUp open, arrows move,
 *    Home and End jump, typing jumps to a matching option (and, while closed, picks it the
 *    way a native select does), Enter selects, Escape closes and returns focus, Tab closes.
 *  * **A panel that is portalled to `<body>` and fixed-positioned.** An ancestor's
 *    `overflow` therefore cannot clip it, and it flips above the trigger when there is not
 *    enough room below. This runs in a Bitrix24 slider whose iframe is short: a panel that
 *    only ever opens downward is a panel that opens off the bottom edge.
 *
 * The selected row is marked with a check as well as with the accent colour, because a
 * colour on its own is not a signal for everyone who has to read it.
 */

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { createPortal } from 'react-dom';

import { Field, type FieldControl } from './Field';

/** One row of the listbox. */
export interface SelectOption {
  /** The value handed back to `onChange`; `''` is a legitimate value ("all"). */
  value: string;
  /** The row's text, already translated - this component never touches the catalogue. */
  label: string;
  /** Renders the row unselectable, for an option that exists but cannot apply right now. */
  disabled?: boolean;
  /** A muted second phrase on the row ("dismissed", "built-in"), so the label itself stays short. */
  hint?: string;
}

export interface SelectProps {
  /** The field's visible name; `Field` binds a real `<label>` to the trigger. */
  label: string;
  /** The selected `SelectOption.value`. Controlled: the parent owns the filter model. */
  value: string;
  /** Called with the chosen value only - the caller never has to dig through an event. */
  onChange: (value: string) => void;
  /** Every row, in display order. Include the "all" row yourself; nothing is prepended. */
  options: readonly SelectOption[];
  /** Trigger text when `value` matches no option, drawn muted. Not a substitute for `label`. */
  placeholder?: string;
  /** Standing helper text under the control, shown whether or not there is an error. */
  hint?: string;
  /** The problem with the current selection; renders below the control with `role="alert"`. */
  error?: string;
  /** Blocks opening, e.g. while the option list is still being fetched. */
  disabled?: boolean;
  /** Shown inside the panel when `options` is empty, so an empty list still says something. */
  emptyText?: string;
  /** Extra classes on the field wrapper, so the caller's grid can size and place it. */
  className?: string;
  /** Emits a hidden input under this name, for the rare case the control sits in a form. */
  name?: string;
  /** Told whenever the panel opens or closes, e.g. to pause a background refresh. */
  onOpenChange?: (open: boolean) => void;
}

/** Gap between trigger and panel, in px. */
const PANEL_GAP = 6;

/** Keep-away from the viewport edge, in px. */
const VIEWPORT_EDGE = 8;

/** The panel never grows past this, in px, however many employees a portal has. */
const PANEL_MAX_H = 320;

/** Below this the panel is not worth flipping for; it scrolls instead. */
const PANEL_MIN_H = 96;

/** A typed run is one word: this long a pause starts a new one. */
const TYPEAHEAD_RESET_MS = 700;

/** Belt and braces: if `animationend` never arrives, the exiting panel still unmounts. */
const EXIT_FALLBACK_MS = 600;

type Phase = 'closed' | 'open' | 'closing';

interface PanelPosition {
  top: number;
  left: number;
  /** The panel is at least as wide as the trigger, and may grow for a long label. */
  minWidth: number;
  maxHeight: number;
  placement: 'bottom' | 'top';
}

export function Select({
  label,
  value,
  onChange,
  options,
  placeholder,
  hint,
  error,
  disabled = false,
  emptyText,
  className,
  name,
  onOpenChange,
}: SelectProps) {
  const base = useId();
  const listId = `${base}list`;
  const optionId = (index: number) => `${base}o${index}`;

  const [phase, setPhase] = useState<Phase>('closed');
  const [activeIndex, setActiveIndex] = useState(-1);
  const [position, setPosition] = useState<PanelPosition | null>(null);

  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLUListElement | null>(null);
  const optionRefs = useRef<Array<HTMLLIElement | null>>([]);
  const typeahead = useRef<{ buffer: string; at: number }>({ buffer: '', at: 0 });

  const open = phase === 'open';
  const selectedIndex = options.findIndex((option) => option.value === value);
  const selected = selectedIndex >= 0 ? options[selectedIndex] : undefined;

  const notifyOpen = useCallback(
    (next: boolean) => {
      onOpenChange?.(next);
    },
    [onOpenChange],
  );

  // --- open / close -------------------------------------------------------------------

  const openPanel = useCallback(
    (start: 'selected' | 'first' | 'last') => {
      if (disabled) {
        return;
      }
      const enabled = options
        .map((option, index) => (option.disabled ? -1 : index))
        .filter((index) => index >= 0);
      const fallback = start === 'last' ? enabled[enabled.length - 1] : enabled[0];
      const preferred = selectedIndex >= 0 && !options[selectedIndex]?.disabled ? selectedIndex : undefined;
      setActiveIndex(preferred ?? fallback ?? -1);
      setPosition(null);
      setPhase('open');
      notifyOpen(true);
    },
    [disabled, notifyOpen, options, selectedIndex],
  );

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

  // The exit animation has to finish before the node goes, so the panel stays mounted for
  // one more beat. `data-phase` is read off the DOM rather than off a closure, so a panel
  // that was reopened mid-exit is not torn down by its own stale animation.
  useEffect(() => {
    if (phase !== 'closing') {
      return;
    }
    const timer = window.setTimeout(() => setPhase('closed'), EXIT_FALLBACK_MS);
    return () => window.clearTimeout(timer);
  }, [phase]);

  const commit = useCallback(
    (index: number) => {
      const option = options[index];
      if (!option || option.disabled) {
        return;
      }
      onChange(option.value);
      closePanel(true);
    },
    [closePanel, onChange, options],
  );

  // --- placement ----------------------------------------------------------------------

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
      const placement: PanelPosition['placement'] =
        below >= wanted || below >= above ? 'bottom' : 'top';
      const room = placement === 'bottom' ? below : above;
      const height = Math.min(wanted, Math.max(PANEL_MIN_H, room));

      const width = Math.max(panel.offsetWidth, rect.width);
      const left = Math.max(
        VIEWPORT_EDGE,
        Math.min(rect.left, viewportW - width - VIEWPORT_EDGE),
      );
      const rawTop = placement === 'bottom' ? rect.bottom + PANEL_GAP : rect.top - PANEL_GAP - height;
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
  }, [closePanel, phase, options.length]);

  // Keep the active row in view without `scrollIntoView`, which would also scroll the page.
  useLayoutEffect(() => {
    if (!open || activeIndex < 0) {
      return;
    }
    const list = panelRef.current;
    const node = optionRefs.current[activeIndex];
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
  }, [activeIndex, open, position]);

  // --- dismissal ----------------------------------------------------------------------

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

  // --- keyboard -----------------------------------------------------------------------

  const step = useCallback(
    (from: number, delta: number): number => {
      if (options.length === 0) {
        return -1;
      }
      let index = from;
      for (let guard = 0; guard < options.length; guard += 1) {
        index += delta;
        if (index < 0) {
          index = 0;
        }
        if (index > options.length - 1) {
          index = options.length - 1;
        }
        if (!options[index]?.disabled) {
          return index;
        }
        if ((delta < 0 && index === 0) || (delta > 0 && index === options.length - 1)) {
          break;
        }
      }
      return from;
    },
    [options],
  );

  const edge = useCallback(
    (which: 'first' | 'last'): number => {
      if (which === 'first') {
        const index = options.findIndex((option) => !option.disabled);
        return index;
      }
      for (let index = options.length - 1; index >= 0; index -= 1) {
        if (!options[index]?.disabled) {
          return index;
        }
      }
      return -1;
    },
    [options],
  );

  /** The next option matching what has been typed so far, or -1. */
  const matchTyped = useCallback(
    (character: string, from: number): number => {
      const now = Date.now();
      const state = typeahead.current;
      const buffer =
        now - state.at > TYPEAHEAD_RESET_MS ? character : state.buffer + character;
      typeahead.current = { buffer, at: now };

      const needle = buffer.toLocaleLowerCase();
      // One character typed twice means "the next option starting with it", which is how a
      // native select cycles through a run of same-initial names.
      const repeated = buffer.length > 1 && [...buffer].every((c) => c === buffer[0]);
      const query = repeated ? needle.slice(0, 1) : needle;
      const start = repeated || buffer.length === 1 ? from + 1 : from;

      for (let offset = 0; offset < options.length; offset += 1) {
        const index = (start + offset + options.length) % options.length;
        const option = options[index];
        if (!option || option.disabled) {
          continue;
        }
        if (option.label.toLocaleLowerCase().startsWith(query)) {
          return index;
        }
      }
      return -1;
    },
    [options],
  );

  const onTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (disabled) {
      return;
    }
    const { key } = event;
    const printable =
      key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey;

    if (!open) {
      if (key === 'ArrowDown' || key === 'ArrowUp' || key === 'Enter' || key === ' ') {
        // `preventDefault` here also cancels the click the browser would synthesise from
        // Enter/Space on a <button>, so the panel is not opened and closed in one stroke.
        event.preventDefault();
        openPanel(key === 'ArrowUp' ? 'last' : 'first');
        return;
      }
      if (printable) {
        event.preventDefault();
        const index = matchTyped(key, selectedIndex);
        if (index >= 0) {
          const option = options[index];
          if (option) {
            onChange(option.value);
          }
        }
      }
      return;
    }

    switch (key) {
      case 'Escape':
        event.preventDefault();
        closePanel(true);
        return;
      case 'Tab':
        // No `preventDefault`: the panel closes and focus carries on to the next control.
        closePanel(false);
        return;
      case 'Enter':
        event.preventDefault();
        if (activeIndex >= 0) {
          commit(activeIndex);
        } else {
          closePanel(true);
        }
        return;
      case ' ':
        if (typeahead.current.buffer && Date.now() - typeahead.current.at <= TYPEAHEAD_RESET_MS) {
          break; // a space inside a typed phrase
        }
        event.preventDefault();
        if (activeIndex >= 0) {
          commit(activeIndex);
        }
        return;
      case 'ArrowDown':
        event.preventDefault();
        setActiveIndex((current) => (current < 0 ? edge('first') : step(current, 1)));
        return;
      case 'ArrowUp':
        event.preventDefault();
        if (event.altKey) {
          closePanel(true);
          return;
        }
        setActiveIndex((current) => (current < 0 ? edge('last') : step(current, -1)));
        return;
      case 'Home':
        event.preventDefault();
        setActiveIndex(edge('first'));
        return;
      case 'End':
        event.preventDefault();
        setActiveIndex(edge('last'));
        return;
      default:
        break;
    }

    if (printable) {
      event.preventDefault();
      const index = matchTyped(key, activeIndex);
      if (index >= 0) {
        setActiveIndex(index);
      }
    }
  };

  // Keeping the default here keeps focus on the trigger, which is what
  // `aria-activedescendant` requires: the panel is never focused, so it never has to
  // hand focus back.
  const keepFocus = (event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
  };

  const triggerText = selected ? selected.label : (placeholder ?? '');

  return (
    <Field label={label} hint={hint} error={error} className={className}>
      {(control: FieldControl) => (
        <div className="ca-select">
          <button
            ref={triggerRef}
            type="button"
            id={control.controlId}
            role="combobox"
            aria-expanded={open}
            aria-controls={listId}
            aria-haspopup="listbox"
            aria-activedescendant={open && activeIndex >= 0 ? optionId(activeIndex) : undefined}
            aria-describedby={control.describedBy}
            aria-invalid={control.invalid || undefined}
            disabled={disabled}
            className="ca-control ca-select-trigger"
            onClick={() => (open ? closePanel(true) : openPanel('selected'))}
            onKeyDown={onTriggerKeyDown}
          >
            <span className="ca-select-value" data-placeholder={selected ? undefined : 'true'}>
              {triggerText}
            </span>
            <ChevronIcon />
          </button>

          {name ? <input type="hidden" name={name} value={value} /> : null}

          {phase !== 'closed' && typeof document !== 'undefined'
            ? createPortal(
                <ul
                  ref={panelRef}
                  id={listId}
                  role="listbox"
                  aria-labelledby={control.labelId}
                  aria-hidden={phase === 'closing' ? true : undefined}
                  className="ca-pop"
                  data-placement={position?.placement ?? 'bottom'}
                  data-phase={phase}
                  style={{
                    top: position?.top ?? 0,
                    left: position?.left ?? 0,
                    minWidth: position?.minWidth ?? 0,
                    maxWidth: 'min(92vw, 26rem)',
                    maxHeight: position?.maxHeight ?? PANEL_MAX_H,
                    visibility: position ? 'visible' : 'hidden',
                  }}
                  onPointerDown={keepFocus}
                  onAnimationEnd={(event) => {
                    if (event.currentTarget.dataset['phase'] === 'closing') {
                      setPhase('closed');
                    }
                  }}
                >
                  {options.length === 0 && emptyText ? (
                    <li className="ca-pop-empty" role="presentation">
                      {emptyText}
                    </li>
                  ) : null}

                  {options.map((option, index) => {
                    const isSelected = option.value === value;
                    return (
                      <li
                        key={option.value}
                        ref={(node) => {
                          optionRefs.current[index] = node;
                        }}
                        id={optionId(index)}
                        role="option"
                        aria-selected={isSelected}
                        aria-disabled={option.disabled || undefined}
                        data-active={index === activeIndex ? 'true' : undefined}
                        className="ca-option"
                        onClick={() => commit(index)}
                        onPointerEnter={() => {
                          if (!option.disabled) {
                            setActiveIndex(index);
                          }
                        }}
                      >
                        <span className="ca-option-mark">
                          {isSelected ? <CheckIcon /> : null}
                        </span>
                        <span className="ca-option-label">{option.label}</span>
                        {option.hint ? (
                          <span className="ca-option-hint">{option.hint}</span>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>,
                document.body,
              )
            : null}
        </div>
      )}
    </Field>
  );
}

function ChevronIcon() {
  return (
    <svg
      className="ca-select-chevron"
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="m6 9.5 6 6 6-6" />
    </svg>
  );
}

/** The check, so "selected" is a shape and not only the accent colour. */
function CheckIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="m5 12.5 4.5 4.5L19 7.5" />
    </svg>
  );
}

export default Select;
