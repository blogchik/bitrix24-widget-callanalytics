/**
 * The control kit.
 *
 * One import site for every control that has to line up in a row: they share a height, a
 * radius, a border, a focus ring and a motion vocabulary, all of it expressed in the
 * `--ca-*` tokens from `globals.css`, and they all wear the same label / hint / error frame.
 *
 * `Select` and `MultiSelect` share `usePopover`: where the panel opens, when it flips and
 * how it dismisses are measurements against the short Bitrix24 slider, not generic popup
 * logic, so they exist once rather than once per control.
 *
 * `Field` is exported for controls this kit does not own yet - a native date input, a
 * checkbox - so that a one-off control still gets a real `<label>`, a visible hint and an
 * announced error rather than a hand-rolled approximation of them.
 *
 * Note for callers: `AppFrame` also exports a `Field`, which is an unrelated label/value
 * row for a definition list. Import this one from `@/components/ui`.
 */

export { Field, UiStyles, type FieldControl, type FieldProps } from './Field';
export { Select, type SelectOption, type SelectProps } from './Select';
export { MultiSelect, type MultiSelectProps } from './MultiSelect';
export { Input, type InputProps } from './Input';
export {
  SegmentedControl,
  type SegmentedOption,
  type SegmentedControlProps,
} from './SegmentedControl';
export {
  DateRange,
  DEFAULT_MAX_SPAN_DAYS,
  CALENDAR_FLOOR,
  type DateRangeValue,
  type DateRangeLabels,
  type DateRangeProps,
} from './DateRange';
