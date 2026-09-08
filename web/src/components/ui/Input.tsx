'use client';

/**
 * A text input at the one shared control height.
 *
 * Three things it does that a bare `<input className="...">` in a page does not:
 *
 *  * **It is the bordered box itself.** No wrapper draws the border, so the base layer's
 *    `:focus-visible` ring lands on exactly the element the keyboard focuses. The leading
 *    icon and the clear button are positioned over the box and the padding is opened up to
 *    make room for them, which is why neither can push the text or resize the control.
 *  * **The clear button is a 44px target on a finger.** Its box really is 44px under
 *    `pointer: coarse` and 24px under a mouse, where a 44px circle inside a 40px field would
 *    read as a second control. Growing the button rather than hanging an invisible
 *    pseudo-element off it keeps the drawn size and the measured size the same number, so a
 *    touch-target check reports what a thumb would actually find.
 *  * **`type` and `inputMode` pass straight through**, along with every other input
 *    attribute, so a phone number field gets a phone keypad and a date field gets the
 *    native picker instead of a text box someone has to type ISO into.
 */

import { useRef, type InputHTMLAttributes, type ReactNode } from 'react';

import { Field, type FieldControl } from './Field';

type NativeInputProps = Omit<
  InputHTMLAttributes<HTMLInputElement>,
  'id' | 'className' | 'value' | 'aria-describedby' | 'aria-invalid'
>;

interface InputOwnProps {
  /** The field's visible name; `Field` binds a real `<label>` to this input. */
  label: string;
  /** The current text. Controlled, because the page owns the model the input edits. */
  value: string;
  /** The new text on every keystroke - the common case, without unwrapping an event. */
  onValueChange?: (value: string) => void;
  /** Standing helper text under the control, shown whether or not there is an error. */
  hint?: string;
  /** The problem with the current text; renders below the control with `role="alert"`. */
  error?: string;
  /** A small glyph inside the left edge - a magnifier, a calendar. Decorative, never a control. */
  leadingIcon?: ReactNode;
  /** Extra classes on the field wrapper, so the caller's grid can size and place it. */
  className?: string;
  /** Extra classes on the `<input>` itself, e.g. `ca-viz-num` for a column of dates. */
  inputClassName?: string;
}

/**
 * The clear button forces its own accessible name into the type: an icon-only control with
 * no name is unreachable by voice and unreadable by a screen reader, and this component has
 * no catalogue of its own to fall back on.
 */
type ClearProps =
  | { clearable?: false; clearLabel?: never; onClear?: never }
  | {
      /** Shows a clear button whenever there is text to clear. */
      clearable: true;
      /** The button's accessible name, already translated ("Очистить"). */
      clearLabel: string;
      /** Extra work after clearing - refocusing happens either way. */
      onClear?: () => void;
    };

export type InputProps = InputOwnProps & ClearProps & NativeInputProps;

export function Input({
  label,
  value,
  onValueChange,
  hint,
  error,
  leadingIcon,
  className,
  inputClassName,
  clearable,
  clearLabel,
  onClear,
  onChange,
  disabled,
  style,
  ...rest
}: InputProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const showClear = Boolean(clearable) && value.length > 0 && !disabled;

  const classes = inputClassName
    ? `ca-control ca-input ${inputClassName}`
    : 'ca-control ca-input';

  return (
    <Field label={label} hint={hint} error={error} className={className}>
      {(control: FieldControl) => (
        <span className="ca-input-shell">
          {leadingIcon ? (
            <span className="ca-input-icon" aria-hidden="true">
              {leadingIcon}
            </span>
          ) : null}

          <input
            {...rest}
            ref={inputRef}
            id={control.controlId}
            className={classes}
            value={value}
            disabled={disabled}
            aria-describedby={control.describedBy}
            aria-invalid={control.invalid || undefined}
            onChange={(event) => {
              onValueChange?.(event.target.value);
              onChange?.(event);
            }}
            // Padding, not margin or a narrower box: the control keeps its full width and
            // the text simply starts after the icon. It is expressed as data attributes
            // rather than an inline style because the clear button is 24px under a mouse
            // and 44px under a finger, and an inline style cannot carry a media query - so
            // an inline padding would let the value slide underneath the button on a phone.
            data-leading={leadingIcon ? '' : undefined}
            data-trailing={showClear ? '' : undefined}
            style={style}
          />

          {showClear ? (
            <button
              type="button"
              className="ca-input-clear"
              aria-label={clearLabel}
              onClick={() => {
                onValueChange?.('');
                onClear?.();
                inputRef.current?.focus();
              }}
            >
              <ClearIcon />
            </button>
          ) : null}
        </span>
      )}
    </Field>
  );
}

function ClearIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

export default Input;
