/**
 * Chart tokens and scale helpers for the dashboard (§4.11).
 *
 * **Every hex in this file is a specification value, not a choice.** The series pair and
 * the sequential ramp were run through the data-viz palette validator against these
 * surfaces; re-deriving them by eye produces a chart a red-green colourblind viewer
 * cannot read (green/red for answered/missed measures deutan dE 4.1 - the app's single
 * most important distinction, gone). The rules that come with them:
 *
 *  * Answered / missed / not-connected are **blue / orange / teal**, never green / red.
 *    Status green and red exist only on the summary tiles, never in a chart, and never
 *    as the only cue: an arrow icon and a word carry the same meaning beside them.
 *  * The heatmap ramp is **one hue**, twelve steps, lightest at zero. Never a rainbow.
 *  * Colour follows the entity, not its rank: `SERIES` is keyed by `result_group`, so
 *    changing a filter repaints nothing.
 *  * One y-axis. Thin marks, 4px rounded data-ends anchored to the baseline, a 2px
 *    surface gap between stacked segments, >=8px hit targets.
 *
 * The values ship as CSS custom properties ({@link VIZ_CSS}) rather than as inline
 * fills, so light/dark lives in exactly one place and a theme change repaints without a
 * React render. Components ask for `var(--ca-viz-*)` through the helpers below.
 *
 * Two values are *derived* rather than specified, and both are marked where they occur:
 * the chrome ink tokens are specified for light only, so the dark set is their
 * counterpart against the dark surface of `globals.css`; and in dark mode the twelve
 * ramp hues are used **in reverse order**, because the invariant the specification
 * states ("lightest means near zero") is really "near zero sits closest to the surface" -
 * on a dark surface the lightest blue would make an empty hour the loudest cell.
 */

// --- series (call outcome) ------------------------------------------------------------

/**
 * The two call outcomes the whole UI shares (`services/stats.py::_RESULT_GROUPS`).
 *
 * §3 still generates three values into `calls.result_group`; the server collapses them
 * once, so nothing on this side has to know that "missed" and "not connected" were ever
 * separate series. The reason they are not is that they answered no question a call list
 * is read to answer, while costing every chart a third stacked band.
 */
export const RESULT_GROUPS = ['answered', 'no_answer'] as const;

export type ResultGroup = (typeof RESULT_GROUPS)[number];

/**
 * Bottom-to-top stacking order, and the fixed order categorical hues are assigned in.
 *
 * Fixed, never cycled: a series keeps its colour no matter which filter is applied.
 */
export const SERIES_ORDER: readonly ResultGroup[] = RESULT_GROUPS;

/** Validated light-mode series hues. */
/*
 * Both hexes are specification values carried over unchanged from the three-series
 * palette, where they were validated together against colour-vision deficiency and
 * against the light and dark surfaces of `globals.css`. `no_answer` inherits the hue that
 * was `missed`, which is the outcome it mostly describes. No new colour is introduced -
 * a palette entry is a measurement here, not a preference.
 */
export const SERIES_LIGHT: Readonly<Record<ResultGroup, string>> = {
  answered: '#2a78d6',
  no_answer: '#eb6834',
};

/** Validated dark-mode series hues (all checks pass in both modes). */
export const SERIES_DARK: Readonly<Record<ResultGroup, string>> = {
  answered: '#3987e5',
  no_answer: '#d95926',
};

/** The paint a mark of `group` wears. Theme-resolved by CSS, not by JavaScript. */
export function seriesVar(group: ResultGroup): string {
  // `no_answer` -> `noanswer`: a CSS custom property may carry an underscore, but every
  // other token in `globals.css` is unbroken lowercase and one exception would be the
  // one people typo.
  return `var(--ca-viz-${group === 'no_answer' ? 'noanswer' : group})`;
}

/** Catalogue key for a series label (§8). Legends are never the only identity cue. */
export function seriesLabelKey(group: ResultGroup): string {
  return `app.dashboard.series.${group}`;
}

// --- sequential ramp (hour x weekday heatmap) -----------------------------------------

/** One hue, light -> dark, twelve steps. Index 0 is "near zero". */
export const RAMP: readonly string[] = [
  '#cde2fb',
  '#b7d3f6',
  '#9ec5f4',
  '#86b6ef',
  '#6da7ec',
  '#5598e7',
  '#3987e5',
  '#2a78d6',
  '#256abf',
  '#1c5cab',
  '#184f95',
  '#104281',
];

/**
 * Which ramp step a count lands on.
 *
 * Linear and monotone, with one deliberate kink: any non-zero count is at least step 1,
 * so "one call happened here" never renders identically to "nothing happened here".
 */
export function rampIndex(value: number, max: number): number {
  if (!Number.isFinite(value) || value <= 0 || !Number.isFinite(max) || max <= 0) {
    return 0;
  }
  const step = Math.ceil((value / max) * (RAMP.length - 1));
  return Math.min(RAMP.length - 1, Math.max(1, step));
}

/** The paint of ramp step `index`, clamped into range. */
export function rampVar(index: number): string {
  const clamped = Math.min(RAMP.length - 1, Math.max(0, Math.trunc(index)));
  return `var(--ca-viz-ramp-${clamped})`;
}

// --- chrome ---------------------------------------------------------------------------

/** Specified light-mode chrome: axis, labels, gridlines, baseline. */
export const CHROME_LIGHT = {
  ink: '#0b0b0b',
  ink2: '#52514e',
  muted: '#898781',
  grid: '#e1e0d9',
  baseline: '#c3c2b7',
} as const;

/** Derived dark-mode counterparts against the `globals.css` dark surface. */
export const CHROME_DARK = {
  ink: '#f2f1ec',
  ink2: '#c8c6bf',
  muted: '#93918b',
  grid: '#34322e',
  baseline: '#4c4a45',
} as const;

/**
 * Status green / red for the summary tiles only.
 *
 * They are permitted there and forbidden in charts, and even on a tile they are the
 * *third* cue: the arrow icon and the word carry the direction, the numbers carry the
 * size, and the tinted pill only agrees with them.
 */
export const STATUS = {
  up: '#0ca30c',
  down: '#d03b3b',
} as const;

/** Single-hue mark for the per-employee comparison. One series needs no legend. */
export const BAR_LIGHT = '#2a78d6';
export const BAR_DARK = '#3987e5';

/** Gap painted between stacked segments, in px (specification). */
export const STACK_GAP_PX = 2;

/** Radius of a rounded data-end, in px (specification). */
export const END_RADIUS_PX = 4;

// --- the stylesheet -------------------------------------------------------------------

function rampBlock(order: readonly string[]): string {
  return order.map((hex, index) => `--ca-viz-ramp-${index}:${hex};`).join('');
}

/**
 * The tokens above as CSS, injected once by the dashboard page.
 *
 * It lives here rather than in `globals.css` because these values belong to the chart
 * specification and must not drift from the constants the marks are laid out with.
 */
export const VIZ_CSS: string = `
:root{
--ca-viz-answered:${SERIES_LIGHT.answered};
--ca-viz-noanswer:${SERIES_LIGHT.no_answer};
--ca-viz-bar:${BAR_LIGHT};
--ca-viz-ink:${CHROME_LIGHT.ink};
--ca-viz-ink-2:${CHROME_LIGHT.ink2};
--ca-viz-muted:${CHROME_LIGHT.muted};
--ca-viz-grid:${CHROME_LIGHT.grid};
--ca-viz-baseline:${CHROME_LIGHT.baseline};
--ca-viz-up:${STATUS.up};
--ca-viz-down:${STATUS.down};
--ca-viz-up-soft:#e9f5e9;
--ca-viz-down-soft:#fbecec;
${rampBlock(RAMP)}
}
@media (prefers-color-scheme: dark){
:root{
--ca-viz-answered:${SERIES_DARK.answered};
--ca-viz-noanswer:${SERIES_DARK.no_answer};
--ca-viz-bar:${BAR_DARK};
--ca-viz-ink:${CHROME_DARK.ink};
--ca-viz-ink-2:${CHROME_DARK.ink2};
--ca-viz-muted:${CHROME_DARK.muted};
--ca-viz-grid:${CHROME_DARK.grid};
--ca-viz-baseline:${CHROME_DARK.baseline};
--ca-viz-up:#4cc44c;
--ca-viz-down:#f0736f;
--ca-viz-up-soft:#16301a;
--ca-viz-down-soft:#341c1c;
${rampBlock([...RAMP].reverse())}
}
}
.ca-viz-num{font-variant-numeric:tabular-nums;font-feature-settings:"tnum" 1;}
.ca-viz-ink{color:var(--ca-viz-ink);}
.ca-viz-ink-2{color:var(--ca-viz-ink-2);}
.ca-viz-label{color:var(--ca-viz-muted);font-size:11px;letter-spacing:.01em;}
.ca-viz-plot{position:relative;}
.ca-viz-tooltip{position:absolute;z-index:5;pointer-events:none;max-width:16rem;padding:8px 10px;border-radius:8px;background:var(--ca-surface);border:1px solid var(--ca-border);box-shadow:0 4px 14px rgba(0,0,0,.10);font-size:12px;line-height:1.45;color:var(--ca-text);}
.ca-viz-swatch{display:inline-block;width:10px;height:10px;border-radius:2px;flex:none;}
.ca-viz-dim{opacity:.5;transition:opacity .18s ease;}
.ca-viz-cell{border-radius:2px;}
.ca-viz-cell:hover{outline:1px solid var(--ca-viz-ink-2);outline-offset:0;}
@media (prefers-reduced-motion: reduce){.ca-viz-dim{transition:none;}}
`;

// --- scales ---------------------------------------------------------------------------

export interface LinearScale {
  /** The rounded axis maximum. */
  max: number;
  /** Tick values from 0 to {@link max}, inclusive. */
  ticks: number[];
}

/**
 * A 1 / 2 / 5 x 10^k axis for counts, from zero.
 *
 * One y-axis, never two, so this is the only scale a chart in this app builds.
 */
export function niceScale(rawMax: number, targetTicks = 4): LinearScale {
  if (!Number.isFinite(rawMax) || rawMax <= 0) {
    return { max: 1, ticks: [0, 1] };
  }
  const rough = rawMax / Math.max(1, targetTicks);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  const step = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * magnitude;
  const max = Math.ceil(rawMax / step) * step;
  const ticks: number[] = [];
  for (let value = 0; value <= max + step / 2; value += step) {
    ticks.push(Math.round(value * 1e6) / 1e6);
  }
  return { max, ticks };
}

/**
 * An SVG path for a bar whose top corners are rounded and whose bottom sits flat on
 * whatever is below it (the baseline, or the next segment down).
 *
 * Only the topmost segment of a stack gets this; everything below is a plain rect, so
 * the rounded end reads as "this is where the data stops", not as decoration.
 */
export function topRoundedPath(x: number, y: number, width: number, height: number): string {
  const radius = Math.max(0, Math.min(END_RADIUS_PX, width / 2, height));
  if (radius <= 0.5) {
    return `M${x} ${y}h${width}v${height}h${-width}z`;
  }
  return [
    `M${x} ${y + height}`,
    `V${y + radius}`,
    `a${radius} ${radius} 0 0 1 ${radius} ${-radius}`,
    `h${width - radius * 2}`,
    `a${radius} ${radius} 0 0 1 ${radius} ${radius}`,
    `V${y + height}`,
    'Z',
  ].join('');
}

/**
 * An SVG path for a horizontal bar rounded at its right (data) end and square at the
 * left, where it is anchored to the baseline.
 */
export function endRoundedPath(x: number, y: number, width: number, height: number): string {
  const radius = Math.max(0, Math.min(END_RADIUS_PX, width, height / 2));
  if (radius <= 0.5) {
    return `M${x} ${y}h${width}v${height}h${-width}z`;
  }
  return [
    `M${x} ${y}`,
    `h${width - radius}`,
    `a${radius} ${radius} 0 0 1 ${radius} ${radius}`,
    `v${height - radius * 2}`,
    `a${radius} ${radius} 0 0 1 ${-radius} ${radius}`,
    `H${x}`,
    'Z',
  ].join('');
}
