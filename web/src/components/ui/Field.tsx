'use client';

/**
 * The label / helper / error wrapper that every control in this kit shares, plus the one
 * stylesheet they all draw from.
 *
 * Three decisions are load-bearing:
 *
 *  * **The label is a real `<label>` bound to a real control id.** A placeholder is not a
 *    label: it disappears the moment someone types, it is not announced as a name by every
 *    screen reader, and it cannot be clicked to reach the control. `Field` mints the ids
 *    and hands them to the control through a render prop, so a caller cannot forget the
 *    binding.
 *  * **Helper text is always visible, and the error sits below the control.** A hint that
 *    only appears once the input is already wrong is a hint nobody read in time; and an
 *    error rendered above the control pushes the control down as it appears, which moves
 *    the thing the pointer was aiming at.
 *  * **Colour is never the only signal.** The error line carries an icon and `role="alert"`;
 *    an invalid control carries `aria-invalid`, not just a red border.
 *
 * The stylesheet lives in this module rather than in `globals.css` because these rules are
 * the kit's own and must travel with it. It is emitted through React 19's
 * `<style href precedence>` hoisting, which de-duplicates it however many controls render -
 * the same mechanism `InlinePlayer` and the chart layer already use.
 *
 * Every colour and every duration below is a `--ca-*` custom property from `globals.css`.
 * Nothing hardcodes a duration, which is what makes `prefers-reduced-motion` a solved
 * problem here: the tokens collapse to 1ms and the whole kit stops moving.
 */

import { useId, type ReactNode } from 'react';

/** React 19 de-duplicates a hoisted `<style>` by this href, so every control may render it. */
const UI_STYLE_ID = 'ca-ui-kit';

const UI_CSS = `
/* A danger colour the token layer does not define yet. It defers to \`--ca-danger\` the
 * moment \`globals.css\` grows one, so this is a fallback and not a second source of truth. */
:root{--ca-ui-danger:var(--ca-danger,#b42318);}
@media (prefers-color-scheme:dark){:root{--ca-ui-danger:var(--ca-danger,#ef7b72);}}

.ca-field{display:flex;flex-direction:column;min-width:0;}
.ca-field-label{display:block;margin:0 0 6px;color:var(--ca-text);font-size:13px;font-weight:600;line-height:1.35;}
.ca-field-optional{margin-left:6px;color:var(--ca-muted);font-weight:400;}
.ca-field-hint{margin:6px 0 0;color:var(--ca-muted);font-size:12px;line-height:1.45;}
.ca-field-error{display:flex;align-items:flex-start;gap:5px;margin:6px 0 0;color:var(--ca-ui-danger);font-size:12px;line-height:1.45;}
.ca-field-error svg{flex:none;margin-top:2px;}

/* ---- the shared control box ------------------------------------------------------
 * One height, one radius, one border for select, input and button, so a filter row is a
 * row and not a staircase. Width is 100% by default: the grid that lays the row out
 * decides how wide a control is, never the length of its longest option. */
.ca-control{box-sizing:border-box;display:flex;align-items:center;gap:8px;width:100%;min-width:0;min-height:var(--ca-control-h);padding:0 var(--ca-control-px);margin:0;border:1px solid var(--ca-border);border-radius:var(--ca-radius);background:var(--ca-surface);color:var(--ca-text);font:inherit;font-size:14px;line-height:1.35;text-align:left;transition:border-color var(--ca-dur-fast) var(--ca-ease),background-color var(--ca-dur-fast) var(--ca-ease),box-shadow var(--ca-dur-fast) var(--ca-ease);}
.ca-control:hover:not(:disabled){border-color:var(--ca-muted);}
/* The pressed state is colour and an inset shadow: no padding, border or margin change, so
 * nothing under the pointer moves at the moment of the click. */
.ca-control:active:not(:disabled){background:var(--ca-surface-soft);box-shadow:inset 0 1px 2px rgb(16 24 40 / .10);}
.ca-control:disabled{opacity:.55;cursor:not-allowed;}
.ca-control[aria-invalid='true']{border-color:var(--ca-ui-danger);}

/* ---- Select ---------------------------------------------------------------------- */
.ca-select{position:relative;width:100%;min-width:0;}
.ca-select-trigger{justify-content:space-between;cursor:pointer;}
.ca-select-value{flex:1 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}
.ca-select-value[data-placeholder='true']{color:var(--ca-muted);}
.ca-select-chevron{flex:none;color:var(--ca-muted);transition:transform var(--ca-dur) var(--ca-ease),color var(--ca-dur-fast) var(--ca-ease);}
.ca-select-trigger[aria-expanded='true']{border-color:var(--ca-accent);}
.ca-select-trigger[aria-expanded='true'] .ca-select-chevron{transform:rotate(180deg);color:var(--ca-accent);}

/* The panel is portalled to <body> and fixed-positioned: no ancestor's overflow can clip
 * it, and the flip decision is made against the iframe viewport, which in a Bitrix24
 * slider is short. */
.ca-pop{position:fixed;z-index:var(--ca-z-pop);box-sizing:border-box;margin:0;padding:4px;list-style:none;overflow-y:auto;overscroll-behavior:contain;background:var(--ca-surface);border:1px solid var(--ca-border);border-radius:var(--ca-radius-lg);box-shadow:var(--ca-shadow-pop);transform-origin:top center;animation:ca-pop-in var(--ca-dur-panel) var(--ca-ease-out) both;}
.ca-pop[data-placement='top']{transform-origin:bottom center;animation-name:ca-pop-in-up;}
.ca-pop[data-phase='closing']{pointer-events:none;animation:ca-pop-out var(--ca-dur-exit) var(--ca-ease) both;}
.ca-pop[data-phase='closing'][data-placement='top']{animation-name:ca-pop-out-up;}
@keyframes ca-pop-in{from{opacity:0;transform:translateY(-6px) scale(.98);}to{opacity:1;transform:none;}}
@keyframes ca-pop-in-up{from{opacity:0;transform:translateY(6px) scale(.98);}to{opacity:1;transform:none;}}
@keyframes ca-pop-out{from{opacity:1;transform:none;}to{opacity:0;transform:translateY(-4px) scale(.98);}}
@keyframes ca-pop-out-up{from{opacity:1;transform:none;}to{opacity:0;transform:translateY(4px) scale(.98);}}

.ca-option{display:flex;align-items:center;gap:8px;box-sizing:border-box;min-height:calc(var(--ca-control-h) - 6px);padding:6px 8px;border-radius:calc(var(--ca-radius) - 2px);color:var(--ca-text);font-size:14px;line-height:1.35;cursor:pointer;user-select:none;transition:background-color var(--ca-dur-fast) var(--ca-ease),color var(--ca-dur-fast) var(--ca-ease);}
.ca-option[data-active='true']{background:var(--ca-surface-soft);}
.ca-option:active{background:var(--ca-accent-soft);}
.ca-option[aria-selected='true']{color:var(--ca-accent);font-weight:500;}
.ca-option[aria-selected='true'][data-active='true']{background:var(--ca-accent-soft);}
.ca-option[aria-disabled='true']{opacity:.5;cursor:not-allowed;}
.ca-option-mark{flex:none;display:flex;width:16px;height:16px;color:var(--ca-accent);}
.ca-option-label{flex:1 1 auto;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}
.ca-option-hint{flex:none;color:var(--ca-muted);font-size:12px;font-weight:400;white-space:nowrap;}
.ca-pop-empty{padding:10px 8px;color:var(--ca-muted);font-size:13px;}

/* ---- Input ----------------------------------------------------------------------- */
.ca-input-shell{position:relative;display:block;width:100%;min-width:0;}
/* The bordered box is the <input> itself and not a wrapper, which keeps the base layer's
 * :focus-visible ring on the element the keyboard actually lands on. */
.ca-input{display:block;height:var(--ca-control-h);cursor:text;}
/* The room the leading icon and the trailing clear button need. Both overlay the box, so
 * the text has to be told to start and stop clear of them; the trailing figure tracks the
 * button's size, which changes with the pointer. */
.ca-input[data-leading]{padding-left:calc(var(--ca-control-px) + 24px);}
.ca-input[data-trailing]{padding-right:calc(var(--ca-control-px) + 22px);}
@media (pointer:coarse){.ca-input[data-trailing]{padding-right:44px;}}
.ca-input::placeholder{color:var(--ca-muted);opacity:1;}
.ca-input-icon{position:absolute;left:var(--ca-control-px);top:50%;display:flex;transform:translateY(-50%);color:var(--ca-muted);pointer-events:none;}
.ca-input-clear{position:absolute;right:calc(var(--ca-control-px) - 4px);top:50%;display:flex;align-items:center;justify-content:center;width:24px;height:24px;padding:0;transform:translateY(-50%);border:0;border-radius:999px;background:transparent;color:var(--ca-muted);cursor:pointer;transition:background-color var(--ca-dur-fast) var(--ca-ease),color var(--ca-dur-fast) var(--ca-ease);}
.ca-input-clear:hover{background:var(--ca-surface-soft);color:var(--ca-text);}
.ca-input-clear:active{background:var(--ca-border-soft);}
/* A 24px glyph is the right size to look at and the wrong size to hit. On a finger the
 * BUTTON grows to the 44px floor and the glyph stays 24px, centred inside it - rather than
 * an invisible pseudo-element, which enlarges the hit area for a real thumb but leaves the
 * element's own box at 24px, so every automated check still reads it as undersized and a
 * human reviewer has to be told why the failure is not a failure. Real box, real number. */
@media (pointer:coarse){
  .ca-input-clear{width:44px;height:44px;right:calc(var(--ca-control-px) - 12px);}
  .ca-input-clear svg{flex:none;}
}

/* ---- SegmentedControl ------------------------------------------------------------ */
.ca-seg{position:relative;box-sizing:border-box;display:flex;align-items:center;gap:2px;min-height:var(--ca-control-h);max-width:100%;padding:3px;border:1px solid var(--ca-border);border-radius:var(--ca-radius);background:var(--ca-surface-soft);overflow-x:auto;overflow-y:hidden;scrollbar-width:none;-ms-overflow-style:none;}
.ca-seg::-webkit-scrollbar{display:none;}
/* A scroller with a segment sliced flush against its border reads as a clipping bug. The
 * fade on the overflowing side says "there is more this way" without spending any width on
 * an arrow, and it is only applied on the side that actually overflows. */
.ca-seg[data-overflow='end']{-webkit-mask-image:linear-gradient(to right,#000 calc(100% - 28px),transparent);mask-image:linear-gradient(to right,#000 calc(100% - 28px),transparent);}
.ca-seg[data-overflow='start']{-webkit-mask-image:linear-gradient(to right,transparent,#000 28px);mask-image:linear-gradient(to right,transparent,#000 28px);}
.ca-seg[data-overflow='both']{-webkit-mask-image:linear-gradient(to right,transparent,#000 28px,#000 calc(100% - 28px),transparent);mask-image:linear-gradient(to right,transparent,#000 28px,#000 calc(100% - 28px),transparent);}
/* The bug this control exists to fix: a label must never break across two lines. When the
 * viewport is too narrow the track scrolls sideways instead. */
.ca-seg-item{position:relative;z-index:1;flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;min-height:calc(var(--ca-control-h) - 8px);padding:0 14px;border:0;border-radius:calc(var(--ca-radius) - 3px);background:transparent;color:var(--ca-muted);font:inherit;font-size:13px;font-weight:500;white-space:nowrap;cursor:pointer;transition:color var(--ca-dur-fast) var(--ca-ease),transform var(--ca-dur-fast) var(--ca-ease);}
.ca-seg-item:hover:not(:disabled){color:var(--ca-text);}
.ca-seg-item:active:not(:disabled){transform:scale(.97);}
.ca-seg-item[aria-checked='true']{color:var(--ca-accent);}
.ca-seg-item:disabled{opacity:.5;cursor:not-allowed;}
/* Only \`transform\` is transitioned. The width is written straight onto the element,
 * because animating width or left is a layout animation on every frame. */
.ca-seg-indicator{position:absolute;z-index:0;top:3px;bottom:3px;left:0;opacity:0;border:1px solid var(--ca-border);border-radius:calc(var(--ca-radius) - 3px);background:var(--ca-surface);box-shadow:0 1px 2px rgb(16 24 40 / .06);transition:transform var(--ca-dur) var(--ca-ease);will-change:transform;pointer-events:none;}
/* On a finger the SEGMENT is the target, not the track, so the segment - not the track -
 * is what has to clear 44px. The track grows by its own padding to keep the sliding
 * indicator inset the way it is with a mouse. */
@media (pointer:coarse){
  .ca-seg{min-height:calc(var(--ca-control-h) + 8px);}
  .ca-seg-item{min-height:var(--ca-control-h);padding:0 16px;}
}
.ca-seg[data-placed='true'] .ca-seg-indicator{opacity:1;}
.ca-seg[data-animate='false'] .ca-seg-indicator{transition:none;}
`;

/** Emits the kit stylesheet once per document, whichever control renders first. */
export function UiStyles() {
  return (
    <style
      href={UI_STYLE_ID}
      precedence="default"
      dangerouslySetInnerHTML={{ __html: UI_CSS }}
    />
  );
}

/**
 * The wiring a control needs in order to be named and described by its own field.
 *
 * It exists so the ids are minted in exactly one place: a caller cannot bind the label to a
 * control that is not there, nor forget the `aria-describedby` that makes the hint audible.
 */
export interface FieldControl {
  /** Put this on the control; `<label htmlFor>` already points at it. */
  controlId: string;
  /** The label element's own id, for a control that takes `aria-labelledby` (a group). */
  labelId: string;
  /** Ready-made `aria-describedby`: hint then error, or `undefined` when there is neither. */
  describedBy: string | undefined;
  /** Mirrors "an error is showing", so the control can set `aria-invalid` from one source. */
  invalid: boolean;
}

export interface FieldProps {
  /** The visible name of the control; a placeholder is not a label and never replaces it. */
  label: string;
  /**
   * Standing helper text under the control. Always rendered, never only on error - guidance
   * that appears after the mistake has been made arrived too late to prevent it.
   */
  hint?: ReactNode;
  /** The problem, in the viewer's language. Its presence is what makes the control invalid. */
  error?: ReactNode;
  /**
   * Renders the label as a `<span>` for a control with no single labelable element - a
   * segmented control, a radio set - which points at it with `aria-labelledby` instead.
   */
  group?: boolean;
  /** Appended to the label ("(optional)"), so optionality is stated rather than guessed. */
  optionalText?: string;
  /** Extra classes on the field wrapper, so the caller's grid can size and place it. */
  className?: string;
  /** The control. In the render-prop form it is handed the ids it has to carry. */
  children: ReactNode | ((control: FieldControl) => ReactNode);
}

/** The label / hint / error frame around one control. */
export function Field({
  label,
  hint,
  error,
  group = false,
  optionalText,
  className,
  children,
}: FieldProps) {
  const base = useId();
  const controlId = `${base}c`;
  const labelId = `${base}l`;
  const hintId = `${base}h`;
  const errorId = `${base}e`;

  const described: string[] = [];
  if (hint) {
    described.push(hintId);
  }
  if (error) {
    described.push(errorId);
  }

  const control: FieldControl = {
    controlId,
    labelId,
    describedBy: described.length > 0 ? described.join(' ') : undefined,
    invalid: Boolean(error),
  };

  const labelContent = (
    <>
      {label}
      {optionalText ? <span className="ca-field-optional">{optionalText}</span> : null}
    </>
  );

  return (
    <div className={className ? `ca-field ${className}` : 'ca-field'}>
      <UiStyles />
      {group ? (
        <span className="ca-field-label" id={labelId}>
          {labelContent}
        </span>
      ) : (
        <label className="ca-field-label" id={labelId} htmlFor={controlId}>
          {labelContent}
        </label>
      )}

      {typeof children === 'function' ? children(control) : children}

      {hint ? (
        <p className="ca-field-hint" id={hintId}>
          {hint}
        </p>
      ) : null}

      {/* Below the control, so its arrival never pushes the control out from under the
          pointer, and `role="alert"` so it is announced the moment it appears. */}
      {error ? (
        <p className="ca-field-error" id={errorId} role="alert">
          <AlertIcon />
          <span>{error}</span>
        </p>
      ) : null}
    </div>
  );
}

/** The error line's icon: the second signal, so the message is not red text alone. */
function AlertIcon() {
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
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7.5v5" />
      <path d="M12 16.2h.01" />
    </svg>
  );
}

export default Field;
