'use client';

/**
 * The period switcher: one row of mutually exclusive choices with a sliding indicator.
 *
 * The bug it exists to fix is narrow and measured: at 375px the old control let "7 дней"
 * break across two lines, which made the row taller than every other control beside it and
 * left half a word hanging. So a segment's label is `white-space: nowrap` and the track
 * scrolls sideways when it has to. A control that scrolls is legible; a control that
 * hyphenates a two-word label is not.
 *
 * Two further decisions:
 *
 *  * **`radiogroup` / `radio`, not `tablist` / `tab`.** These segments select a value, they
 *    do not reveal panels, and a screen reader that announces "2 of 4, selected" is telling
 *    the truth about what the control does. The keyboard contract is the radio group's:
 *    arrows move *and* select, Home and End jump, and only the checked segment is in the
 *    tab order, so Tab passes the whole control in one press.
 *  * **The indicator animates `transform` and nothing else.** Its width is written straight
 *    onto the element without a transition; animating width or left would relayout the row
 *    on every frame, and the row contains the rest of the filter bar.
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';

import { Field, type FieldControl } from './Field';

/** One segment. */
export interface SegmentedOption<V extends string> {
  /** The value handed back to `onChange`, and the React key. */
  value: V;
  /** The segment's text, already translated - short, because it must never wrap. */
  label: string;
  /** Renders the segment unselectable while leaving it visible and announced. */
  disabled?: boolean;
}

export interface SegmentedControlProps<V extends string> {
  /** The group's visible name; `Field` renders it as a `<span>` the group points at. */
  label: string;
  /** The selected value. Controlled: the page owns the period, not this control. */
  value: V;
  /** Called with the chosen value, including when an arrow key moves the selection. */
  onChange: (value: V) => void;
  /** The segments, in display order. Two to five: past that it is a Select. */
  options: readonly SegmentedOption<V>[];
  /** Standing helper text under the control, shown whether or not there is an error. */
  hint?: string;
  /** The problem with the current choice; renders below the control with `role="alert"`. */
  error?: string;
  /** Blocks the whole group, e.g. while the page it drives is loading its first data. */
  disabled?: boolean;
  /** Extra classes on the field wrapper, so the caller's grid can size and place it. */
  className?: string;
}

/** Where the indicator sits, in track coordinates. */
interface Metrics {
  left: number;
  width: number;
}

/** Keep-away when scrolling a segment into view, in px. */
const SCROLL_EDGE = 3;

/** Which side of the track has content beyond the edge, for the scroll-affordance fade. */
type Overflow = 'none' | 'start' | 'end' | 'both';

export function SegmentedControl<V extends string>({
  label,
  value,
  onChange,
  options,
  hint,
  error,
  disabled = false,
  className,
}: SegmentedControlProps<V>) {
  const trackRef = useRef<HTMLDivElement | null>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [animate, setAnimate] = useState(false);
  const [overflow, setOverflow] = useState<Overflow>('none');

  const selectedIndex = options.findIndex((option) => option.value === value);
  // Nothing selected still needs one segment in the tab order, or the control is
  // unreachable by keyboard until it has been clicked.
  const focusIndex =
    selectedIndex >= 0 ? selectedIndex : options.findIndex((option) => !option.disabled);

  // The indicator is measured rather than computed: label widths depend on the locale and
  // on whether the system font has finished loading, and neither is knowable up front.
  useLayoutEffect(() => {
    const track = trackRef.current;
    const node = selectedIndex >= 0 ? itemRefs.current[selectedIndex] : null;
    if (!track || !node) {
      setMetrics(null);
      return;
    }
    const measure = () => {
      const next = { left: node.offsetLeft, width: node.offsetWidth };
      setMetrics((current) =>
        current && current.left === next.left && current.width === next.width ? current : next,
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(track);
    observer.observe(node);
    return () => observer.disconnect();
  }, [options.length, selectedIndex, value]);

  // Which edge is hiding a segment, so the fade is drawn only where there is more to see.
  useEffect(() => {
    const track = trackRef.current;
    if (!track) {
      return;
    }
    const sync = () => {
      const room = track.scrollWidth - track.clientWidth;
      const atStart = track.scrollLeft > 1;
      const atEnd = track.scrollLeft < room - 1;
      setOverflow(atStart && atEnd ? 'both' : atStart ? 'start' : atEnd ? 'end' : 'none');
    };
    sync();
    track.addEventListener('scroll', sync, { passive: true });
    const observer = new ResizeObserver(sync);
    observer.observe(track);
    return () => {
      track.removeEventListener('scroll', sync);
      observer.disconnect();
    };
  }, [options.length]);

  // The first placement is a jump, not a slide: an indicator that slides in from the left
  // edge on mount reads as a loading animation nobody asked for.
  useEffect(() => {
    if (!metrics || animate) {
      return;
    }
    const frame = window.requestAnimationFrame(() => setAnimate(true));
    return () => window.cancelAnimationFrame(frame);
  }, [animate, metrics]);

  // On a narrow viewport the track scrolls, so the chosen segment has to be brought back
  // into view when it is chosen from the keyboard.
  useEffect(() => {
    const track = trackRef.current;
    const node = selectedIndex >= 0 ? itemRefs.current[selectedIndex] : null;
    if (!track || !node) {
      return;
    }
    const left = node.offsetLeft;
    const right = left + node.offsetWidth;
    if (left < track.scrollLeft) {
      track.scrollLeft = Math.max(0, left - SCROLL_EDGE);
    } else if (right > track.scrollLeft + track.clientWidth) {
      track.scrollLeft = right - track.clientWidth + SCROLL_EDGE;
    }
  }, [selectedIndex]);

  const focusAndSelect = useCallback(
    (index: number) => {
      const option = options[index];
      if (!option || option.disabled) {
        return;
      }
      itemRefs.current[index]?.focus();
      if (option.value !== value) {
        onChange(option.value);
      }
    },
    [onChange, options, value],
  );

  /** The next enabled segment in `delta`'s direction, wrapping. */
  const step = useCallback(
    (from: number, delta: number): number => {
      if (options.length === 0) {
        return -1;
      }
      for (let offset = 1; offset <= options.length; offset += 1) {
        const index = (from + delta * offset + options.length * options.length) % options.length;
        if (!options[index]?.disabled) {
          return index;
        }
      }
      return from;
    },
    [options],
  );

  const onKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, index: number) => {
    if (disabled) {
      return;
    }
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        event.preventDefault();
        focusAndSelect(step(index, 1));
        return;
      case 'ArrowLeft':
      case 'ArrowUp':
        event.preventDefault();
        focusAndSelect(step(index, -1));
        return;
      case 'Home':
        event.preventDefault();
        focusAndSelect(step(-1, 1));
        return;
      case 'End':
        event.preventDefault();
        focusAndSelect(step(options.length, -1));
        return;
      default:
        return;
    }
  };

  return (
    <Field label={label} hint={hint} error={error} className={className} group>
      {(control: FieldControl) => (
        <div
          ref={trackRef}
          id={control.controlId}
          role="radiogroup"
          aria-labelledby={control.labelId}
          aria-describedby={control.describedBy}
          className="ca-seg"
          data-placed={metrics ? 'true' : 'false'}
          data-animate={animate ? 'true' : 'false'}
          data-overflow={overflow}
        >
          <span
            className="ca-seg-indicator"
            aria-hidden="true"
            style={
              metrics
                ? { width: metrics.width, transform: `translateX(${metrics.left}px)` }
                : undefined
            }
          />

          {options.map((option, index) => (
            <button
              key={option.value}
              ref={(node) => {
                itemRefs.current[index] = node;
              }}
              type="button"
              role="radio"
              aria-checked={option.value === value}
              tabIndex={index === focusIndex ? 0 : -1}
              disabled={disabled || option.disabled}
              className="ca-seg-item"
              onClick={() => focusAndSelect(index)}
              onKeyDown={(event) => onKeyDown(event, index)}
            >
              {option.label}
            </button>
          ))}
        </div>
      )}
    </Field>
  );
}

export default SegmentedControl;
