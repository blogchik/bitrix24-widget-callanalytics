'use client';

/**
 * A select that takes several answers.
 *
 * It is {@link Select} with one thing changed and two consequences. The change: a click
 * toggles instead of committing, so the panel stays open while a person picks a team. The
 * consequences:
 *
 *  * **The empty selection means "everyone".** This is not a shortcut, it is the app's
 *    existing convention - `''` has always meant "no predicate" in `DashboardFilters`, and
 *    the server has always read a missing `employee` parameter as the whole portal. So the
 *    "All" row is not a button bolted onto a listbox: it is a real option, selected exactly
 *    when nothing else is, and choosing it clears the rest. That keeps the control inside
 *    `role="listbox"` - a listbox may contain options and nothing else, and a "select all"
 *    button placed among them is markup a screen reader has no rule for.
 *  * **The trigger has to summarise.** One name fits; four do not. It says the name while
 *    there is one, and a count past that, because a truncated list of names reads as a bug
 *    rather than as a summary.
 *
 * Everything about where the panel opens, when it flips and how it dismisses lives in
 * {@link usePopover}, shared with `Select`. It is not generic popup logic - every branch
 * of it is a measurement against the short Bitrix24 slider - which is precisely why there
 * is one copy.
 */

import {
  useCallback,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { createPortal } from 'react-dom';

import { Field, type FieldControl } from './Field';
import type { SelectOption } from './Select';
import { PANEL_MAX_H, usePopover } from './usePopover';

/** A typed run is one word: this long a pause starts a new one. */
const TYPEAHEAD_RESET_MS = 700;

export interface MultiSelectProps {
  label: string;
  /** The selected values. Empty means "all", which is what the server reads too. */
  values: readonly string[];
  onChange: (next: string[]) => void;
  options: readonly SelectOption[];
  /** The first row, and what the trigger says when nothing is selected ("All"). */
  allLabel: string;
  /** The trigger past one selection, already translated ("3 selected"). */
  summaryLabel: (count: number) => string;
  hint?: string;
  error?: string;
  disabled?: boolean;
  /** Shown in place of the rows when there are none, already translated. */
  emptyText?: string;
  className?: string;
  onOpenChange?: (open: boolean) => void;
}

export function MultiSelect({
  label,
  values,
  onChange,
  options,
  allLabel,
  summaryLabel,
  hint,
  error,
  disabled = false,
  emptyText,
  className,
  onOpenChange,
}: MultiSelectProps) {
  const base = useId();
  const listId = `${base}list`;
  const optionId = (index: number) => `${base}o${index}`;

  const [activeIndex, setActiveIndex] = useState(-1);
  const optionRefs = useRef<Array<HTMLLIElement | null>>([]);
  const typeahead = useRef<{ buffer: string; at: number }>({ buffer: '', at: 0 });

  const {
    phase,
    open,
    position,
    triggerRef,
    panelRef,
    openPanel: openPopover,
    closePanel,
    finishExit,
    revealRow,
  } = usePopover<HTMLButtonElement, HTMLUListElement>({
    disabled,
    onOpenChange,
    measureKey: options.length,
  });

  const selected = useMemo(() => new Set(values), [values]);

  /**
   * The rows, with "All" first.
   *
   * Index 0 is the sentinel: it is a real `role="option"`, it is selected exactly when
   * nothing else is, and choosing it clears the selection. Keyboard, typeahead and
   * `aria-activedescendant` then work on it without a single special case.
   */
  const rows = useMemo(
    (): readonly SelectOption[] => [{ value: '', label: allLabel }, ...options],
    [allLabel, options],
  );

  const triggerText = useMemo(() => {
    if (selected.size === 0) {
      return allLabel;
    }
    if (selected.size === 1) {
      const only = options.find((option) => selected.has(option.value));
      // A selected value with no option behind it is a stale id - the employee list was
      // refetched and someone left. The count is still true, so say that rather than an
      // empty trigger.
      return only ? only.label : summaryLabel(selected.size);
    }
    return summaryLabel(selected.size);
  }, [allLabel, options, selected, summaryLabel]);

  const openPanel = useCallback(
    (start: 'first' | 'last') => {
      if (disabled) {
        return;
      }
      const enabled = rows
        .map((row, index) => (row.disabled ? -1 : index))
        .filter((index) => index >= 0);
      const fallback = start === 'last' ? enabled[enabled.length - 1] : enabled[0];
      setActiveIndex(fallback ?? -1);
      openPopover();
    },
    [disabled, openPopover, rows],
  );

  const toggle = useCallback(
    (index: number) => {
      const row = rows[index];
      if (!row || row.disabled) {
        return;
      }
      // The "All" row is not a value, it is the absence of them.
      if (index === 0) {
        onChange([]);
        return;
      }
      const next = new Set(selected);
      if (next.has(row.value)) {
        next.delete(row.value);
      } else {
        next.add(row.value);
      }
      // Ordered by the option list rather than by click order, so the query string this
      // feeds is stable and two identical selections produce one cache key.
      onChange(options.filter((option) => next.has(option.value)).map((option) => option.value));
    },
    [onChange, options, rows, selected],
  );

  useLayoutEffect(() => {
    if (open && activeIndex >= 0) {
      revealRow(optionRefs.current[activeIndex] ?? null);
    }
  }, [activeIndex, open, position, revealRow]);

  // --- keyboard -------------------------------------------------------------------------

  const step = useCallback(
    (from: number, delta: number): number => {
      if (rows.length === 0) {
        return -1;
      }
      let index = from;
      for (let guard = 0; guard < rows.length; guard += 1) {
        index += delta;
        if (index < 0) {
          index = 0;
        }
        if (index > rows.length - 1) {
          index = rows.length - 1;
        }
        if (!rows[index]?.disabled) {
          return index;
        }
        if ((delta < 0 && index === 0) || (delta > 0 && index === rows.length - 1)) {
          break;
        }
      }
      return from;
    },
    [rows],
  );

  const edge = useCallback(
    (which: 'first' | 'last'): number => {
      if (which === 'first') {
        return rows.findIndex((row) => !row.disabled);
      }
      for (let index = rows.length - 1; index >= 0; index -= 1) {
        if (!rows[index]?.disabled) {
          return index;
        }
      }
      return -1;
    },
    [rows],
  );

  /** The next row matching what has been typed so far, or -1. */
  const matchTyped = useCallback(
    (character: string, from: number): number => {
      const now = Date.now();
      const state = typeahead.current;
      const buffer = now - state.at > TYPEAHEAD_RESET_MS ? character : state.buffer + character;
      typeahead.current = { buffer, at: now };

      const needle = buffer.toLocaleLowerCase();
      const repeated = buffer.length > 1 && [...buffer].every((c) => c === buffer[0]);
      const query = repeated ? needle.slice(0, 1) : needle;
      const start = repeated || buffer.length === 1 ? from + 1 : from;

      for (let offset = 0; offset < rows.length; offset += 1) {
        const index = (start + offset + rows.length) % rows.length;
        const row = rows[index];
        if (!row || row.disabled) {
          continue;
        }
        if (row.label.toLocaleLowerCase().startsWith(query)) {
          return index;
        }
      }
      return -1;
    },
    [rows],
  );

  const onTriggerKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (disabled) {
      return;
    }
    const { key } = event;
    const printable = key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey;

    if (!open) {
      if (key === 'ArrowDown' || key === 'ArrowUp' || key === 'Enter' || key === ' ') {
        // `preventDefault` also cancels the click the browser would synthesise from
        // Enter/Space on a <button>, so the panel is not opened and closed in one stroke.
        event.preventDefault();
        openPanel(key === 'ArrowUp' ? 'last' : 'first');
      }
      // Unlike a single select, a closed multi-select does NOT change the selection on a
      // typed letter: there is no "the next one" to move to when several are already
      // chosen, and silently adding one would be a selection nobody saw being made.
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
      case ' ':
        if (
          key === ' ' &&
          typeahead.current.buffer &&
          Date.now() - typeahead.current.at <= TYPEAHEAD_RESET_MS
        ) {
          break; // a space inside a typed phrase
        }
        event.preventDefault();
        if (activeIndex >= 0) {
          // Toggles and stays open: picking four people should not cost four reopens.
          toggle(activeIndex);
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
  // `aria-activedescendant` requires: the panel is never focused, so it never has to hand
  // focus back.
  const keepFocus = (event: ReactPointerEvent<HTMLElement>) => {
    event.preventDefault();
  };

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
            onClick={() => (open ? closePanel(true) : openPanel('first'))}
            onKeyDown={onTriggerKeyDown}
          >
            <span className="ca-select-value">{triggerText}</span>
            <ChevronIcon />
          </button>

          {phase !== 'closed' && typeof document !== 'undefined'
            ? createPortal(
                <ul
                  ref={panelRef}
                  id={listId}
                  role="listbox"
                  aria-multiselectable="true"
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
                      finishExit();
                    }
                  }}
                >
                  {options.length === 0 && emptyText ? (
                    <li className="ca-pop-empty" role="presentation">
                      {emptyText}
                    </li>
                  ) : null}

                  {rows.map((row, index) => {
                    const isSelected = index === 0 ? selected.size === 0 : selected.has(row.value);
                    return (
                      <li
                        key={row.value || ' all'}
                        ref={(node) => {
                          optionRefs.current[index] = node;
                        }}
                        id={optionId(index)}
                        role="option"
                        aria-selected={isSelected}
                        aria-disabled={row.disabled || undefined}
                        data-active={index === activeIndex ? 'true' : undefined}
                        className="ca-option"
                        onClick={() => toggle(index)}
                        onPointerEnter={() => {
                          if (!row.disabled) {
                            setActiveIndex(index);
                          }
                        }}
                      >
                        <span className="ca-option-mark">{isSelected ? <CheckIcon /> : null}</span>
                        <span className="ca-option-label">{row.label}</span>
                        {row.hint ? <span className="ca-option-hint">{row.hint}</span> : null}
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
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="ca-select-chevron"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M4 6.5L8 10.5l4-4" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M3.5 8.5l3 3 6-6.5" />
    </svg>
  );
}

export default MultiSelect;
