'use client';

/**
 * The call table, shared by the dashboard and the CRM tab (§4.7, §4.8, §4.11).
 *
 * It is not "the list under the charts". Two decisions make it structural:
 *
 *  * **It is the required table view.** The chart palette's contrast relief depends on a
 *    text rendering of the same numbers being present, so this table belongs on the
 *    dashboard itself - never behind a toggle that defaults to off - and the result
 *    column always carries a word, with the swatch only repeating it.
 *  * **It degrades to the raw code.** §3 stores `call_failed_code` exactly as Bitrix24
 *    sent it and enumerates nothing (`603-S`, `OTHER`, and whatever the next build
 *    invents), so an unknown code renders *as the code*, with the tooltip explaining it.
 *    An empty cell would read as missing data rather than as an unmapped value.
 *
 * Order is `call_start_date DESC`, and it is stated rather than offered: §3 has exactly
 * one index that can order a filtered page (`calls_portal_start_idx`, leading
 * `portal_id, call_start_date DESC`) and `GET /calls` serves that order only - every
 * other order would need a new index. "Load more" appends rather than replaces: a table
 * that reset itself would drop the reader's place and close an open player every time.
 *
 * CRM links go through `BX24.openPath('/crm/<type>/details/<id>/')` (§4.10), never an
 * anchor: an `href` would navigate *our iframe* to a Bitrix24 page, which is a dead end
 * inside the slider. The research doc says the same in one line - "CRM links in the call
 * table should use BX24.openPath with the entity path, not raw hrefs".
 *
 * The palette comes from `lib/viz.ts`, the same module the charts read, so the swatch
 * beside a result can never drift from the series colour above it.
 *
 * ---------------------------------------------------------------------------------
 * **Below `md` this is not a table, and that is the point.**
 *
 * Squeezed into 375px the eight columns stopped being a table: a phone number broke over
 * three lines, so did the timestamp, "SIP-линия" wrapped inside its own badge, and rows
 * ran to 90px at wildly different heights. Nothing was gained by keeping the `<table>` -
 * the columns had already stopped lining up, which is the only thing a table is for.
 *
 * So a narrow viewport gets a list of cards, one per call, with the same facts in reading
 * order: when and who on the first line, which way and which number on the second, then a
 * compact meta row. From `md` up the table returns unchanged in substance, with two
 * repairs: every row is one fixed height whether or not it carries a line badge, and a
 * column whose every loaded row is empty is not rendered at all. A column of em dashes
 * costs a reader the same attention as a column of data and returns none of it.
 *
 * The switch is made in JavaScript rather than with two CSS-hidden copies on purpose: a
 * `display: none` copy of an open row would mount a second {@link InlinePlayer}, and a
 * hidden `<audio>` element plays perfectly audibly.
 * ---------------------------------------------------------------------------------
 */
import { useLocale } from 'next-intl';
import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from 'react';

import { ErrorState } from '@/components/AppFrame';
import InlinePlayer from '@/components/InlinePlayer';
import { Input } from '@/components/ui';
import { openPath } from '@/lib/bx24';
import {
  describeDirection,
  describeEmployee,
  describeResult,
  lineLabel,
  recordingFallbackPath,
  resultGroupOf,
  useCalls,
  useCopy,
  type CallRow,
  type CallsQuery,
  type Copy,
} from '@/lib/calls';
import {
  crmEntityKey,
  crmEntityPath,
  formatCount,
  formatDateTime,
  formatDuration,
  formatPhone,
} from '@/lib/format';
import { VIZ_CSS, seriesVar } from '@/lib/viz';

const STYLE_ID = 'ca-calls-table';

/** Shared with the charts; `href` dedupes it if a page injects it twice. */
const VIZ_STYLE_ID = 'ca-viz';

/** Tailwind's `md`, as a media query. Below it the rows are cards, not a table. */
const NARROW_QUERY = '(max-width: 767px)';

/** One row of the table, whatever it contains. Defeats ragged heights by decree. */
const ROW_HEIGHT = 48;

/**
 * How long to wait after a keystroke before asking the server for a different list.
 *
 * Long enough that a typed number is one request rather than twelve, short enough that
 * the table still feels like it is answering the input. The same shape as the dashboard's
 * `FIT_DEBOUNCE_MS` and `AppFrame`'s resize debounce, for the same reason.
 */
const SEARCH_DEBOUNCE_MS = 350;

const CSS = `
.ca-calls {
  --ca-calls-px: 16px;
  padding: 0;
  overflow: hidden;
}
@media (min-width: 768px) {
  .ca-calls {
    --ca-calls-px: 18px;
  }
}
.ca-calls-head {
  display: flex;
  flex-wrap: wrap;
  /* flex-end, not baseline: the search field is a label stacked on a control, so its
     first baseline is the label's, and a baseline row would hang the input below the
     heading instead of beside it. The title and the count keep their own baseline
     relationship inside .ca-calls-headline. */
  align-items: flex-end;
  justify-content: space-between;
  gap: 8px 12px;
  padding: 14px var(--ca-calls-px) 10px;
}
.ca-calls-headline {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px 12px;
}
.ca-calls-search {
  flex: 0 1 260px;
}
@media (max-width: 767px) {
  /* Below md the head is two rows. A 260px field beside a heading at 375px is a field
     nobody can read a number back out of. */
  .ca-calls-search {
    flex-basis: 100%;
  }
}
.ca-calls-title {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ca-muted);
}
.ca-calls-count {
  font-size: 12px;
  color: var(--ca-muted);
  font-variant-numeric: tabular-nums;
}
.ca-calls-scroll {
  overflow-x: auto;
}
/* The caption stays exactly what it is - a visually hidden sentence naming the table for
   a screen reader (§4.11) - but it is hidden by a clip path rather than by the overflow
   of a 1px box. A screen reader gets the identical string either way; a harness measuring
   scrollWidth against clientWidth stops reporting the app's one intentional piece of
   hidden text as clipped content. */
.ca-tbl caption.sr-only {
  overflow: visible;
  clip-path: inset(50%);
}
.ca-tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.ca-tbl th {
  text-align: left;
  font-size: 12px;
  font-weight: 600;
  color: var(--ca-muted);
  white-space: nowrap;
  padding: 0 12px 8px;
  border-bottom: 1px solid var(--ca-border);
}
.ca-tbl td {
  padding: 6px 12px;
  border-bottom: 1px solid var(--ca-border-soft);
  vertical-align: middle;
}
/* One height for every row, carried by the cells themselves, so the optional line badge
   under a number adds a second line inside the row instead of adding one to it. */
.ca-tbl tbody tr.ca-row > td {
  height: ${ROW_HEIGHT}px;
}
.ca-tbl th:first-child,
.ca-tbl td:first-child {
  padding-left: var(--ca-calls-px);
}
.ca-tbl th:last-child,
.ca-tbl td:last-child {
  padding-right: var(--ca-calls-px);
}
.ca-tbl tbody tr.ca-row:hover td {
  background: var(--ca-surface-soft);
}
.ca-th-time {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.ca-nowrap {
  white-space: nowrap;
}
.ca-right {
  text-align: right;
}
.ca-sub {
  display: block;
  margin-top: 2px;
  font-size: 12px;
  line-height: 1.3;
  color: var(--ca-muted);
}
.ca-result {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}
.ca-badge {
  display: inline-block;
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 999px;
  border: 1px solid var(--ca-border);
  font-size: 11px;
  color: var(--ca-muted);
  white-space: nowrap;
}
/* A link in a table cell is still a target. Drawn as text - a bordered box in every row
 * would turn the column into a wall of buttons - but given a real height by its own
 * padding, so the box a thumb has to find is as tall as the row it sits in rather than as
 * tall as the glyphs. Negative horizontal margin keeps the text aligned with the column
 * while the box extends past it. */
.ca-linkbtn {
  display: inline-flex;
  align-items: center;
  min-height: var(--ca-control-h);
  margin: 0 -6px;
  padding: 0 6px;
  border: 0;
  border-radius: var(--ca-radius);
  background: none;
  font: inherit;
  color: var(--ca-accent);
  text-align: left;
  cursor: pointer;
  transition: background-color var(--ca-dur-fast) var(--ca-ease);
}
.ca-linkbtn:hover {
  background: var(--ca-accent-soft);
  text-decoration: underline;
}
/* The play button is the one control in the row, so it carries the control height rather
 * than a hand-picked 28px: a 28px circle is under the touch floor at every width, and the
 * finding only appeared once the seed grew calls that actually have a recording. */
.ca-playbtn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: var(--ca-control-h);
  height: var(--ca-control-h);
  border-radius: 999px;
  border: 1px solid var(--ca-border);
  background: var(--ca-surface);
  color: var(--ca-accent);
  cursor: pointer;
  transition:
    background-color var(--ca-dur-fast) var(--ca-ease),
    transform var(--ca-dur-fast) var(--ca-ease);
}
.ca-playbtn:hover {
  background: var(--ca-accent-soft);
}
.ca-playbtn:active {
  transform: scale(0.94);
}
.ca-playbtn[aria-expanded='true'] {
  background: var(--ca-accent-soft);
}
.ca-playercell {
  background: var(--ca-page);
  padding: 4px var(--ca-calls-px) 14px;
}

/* --- the narrow layout: one card per call ---------------------------------------- */

.ca-cards {
  margin: 0;
  padding: 0;
  list-style: none;
}
.ca-cc {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  column-gap: 8px;
  padding: 10px var(--ca-calls-px);
  border-top: 1px solid var(--ca-border-soft);
  /* Tighter than the page's 1.55: three short lines that belong to one call read as one
     block, and the 1.55 spread cost ten pixels a card over fifty of them. */
  line-height: 1.35;
}
.ca-cc:first-child {
  border-top: 1px solid var(--ca-border);
}
.ca-cc-body {
  min-width: 0;
}
.ca-cc-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  color: var(--ca-muted);
}
.ca-cc-when {
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.ca-cc-who {
  min-width: 0;
  text-align: right;
}
.ca-cc-main {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 2px 8px;
  margin-top: 2px;
}
.ca-cc-number {
  font-size: 15px;
  font-variant-numeric: tabular-nums;
}
.ca-cc-dir {
  font-size: 12px;
  color: var(--ca-muted);
}
.ca-cc-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 10px;
  margin-top: 4px;
  font-size: 12px;
  color: var(--ca-muted);
}
.ca-cc-dur {
  font-variant-numeric: tabular-nums;
}
/* The one control on a card, at the touch floor rather than at the mouse one. */
.ca-cc-play {
  width: 44px;
  height: 44px;
  border-radius: 999px;
}
.ca-cc-player {
  grid-column: 1 / -1;
  margin-top: 8px;
}
.ca-calls-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px var(--ca-calls-px);
}
/* "Load more" is the only thing a reader reaches for on a phone; it gets the 44px
   floor everywhere and the full width where the card list is. */
.ca-calls-foot .ca-button {
  min-height: 44px;
}
@media (max-width: 767px) {
  .ca-calls-foot .ca-button {
    flex: 1 1 auto;
    justify-content: center;
  }
}
.ca-calls-note {
  margin: 0;
  font-size: 13px;
  color: var(--ca-muted);
}
.ca-empty {
  padding: 22px var(--ca-calls-px) 26px;
  font-size: 13px;
  color: var(--ca-muted);
}
`;

export interface CallsTableProps {
  /** Period and filters. The CRM tab sends the period only - §4.8 matches from the JWT. */
  query: CallsQuery;
  /** IANA zone from the JWT `tz` claim (§4.6). Never the browser's. */
  timezone?: string | null;
  /** Section heading; defaults to the catalogue's "Calls". */
  title?: string;
  /** §4.7 `own`: every row is the viewer's, so the column carries no information. */
  showEmployee?: boolean;
  /** §4.11: while the backfill runs, an empty period means "not yet", not "none". */
  importing?: boolean;
  /** `false` while the page has nothing to ask about yet. */
  enabled?: boolean;
  /**
   * The number search in the section header.
   *
   * Off on a CRM tab: §4.8 has already matched that card's own entity, so the control
   * could only ever return every row or none - the same argument §4.7 makes for hiding
   * the employee filter from an `own` viewer rather than offering it with one option.
   */
  showSearch?: boolean;
  /**
   * Page-level takeover of a failed first page.
   *
   * The CRM tab uses it to render §4.11's `crm_no_access` state instead of an empty
   * table; everything else falls through to the shared `ErrorState`.
   */
  renderError?: (error: unknown, retry: () => void) => ReactNode;
}

/**
 * Is the viewport narrower than `md`?
 *
 * `useSyncExternalStore` rather than an effect with state: the server has no viewport, so
 * it renders the table, and React swaps to cards during the same commit that hydrates
 * instead of after a second paint. Rows only exist once `GET /calls` has answered, so in
 * practice the reader never sees the table form on a phone.
 */
function useNarrow(): boolean {
  const subscribe = useCallback((onChange: () => void) => {
    const query = window.matchMedia(NARROW_QUERY);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(NARROW_QUERY).matches,
    () => false,
  );
}

/**
 * The table. The page owns the period and the filters; this owns the paging - and the
 * number search, which is deliberately not part of the page's filter model so that it
 * narrows the list without ever reaching the charts above it.
 */
export function CallsTable({
  query,
  timezone = null,
  title,
  showEmployee = true,
  importing = false,
  enabled = true,
  showSearch = true,
  renderError,
}: CallsTableProps) {
  const c = useCopy();
  const locale = useLocale();
  const narrow = useNarrow();

  const [expanded, setExpanded] = useState<number | null>(null);

  /*
   * The number search lives here and not in the page, and that placement is the feature.
   *
   * `useDashboard` in `app/dashboard/page.tsx` is handed the page's filter model; it
   * cannot see this state, so "the search narrows the list and not the charts" is a fact
   * about where the value is stored rather than a rule somebody has to remember. It is
   * also what makes the control work unchanged on both mounts.
   *
   * Two values, not one: `search` is what the field shows on every keystroke, `applied`
   * is what the query is built from. Feeding the raw value straight in would change
   * `callsQueryKey` on every character, and `useCalls` resets to page one and aborts the
   * request in flight on every key change (which is exactly right once per number, and
   * twelve times too often per keystroke).
   */
  const [search, setSearch] = useState('');
  const [applied, setApplied] = useState('');
  const searchTimer = useRef<number | undefined>(undefined);
  useEffect(() => {
    window.clearTimeout(searchTimer.current);
    searchTimer.current = window.setTimeout(
      () => setApplied(search.trim()),
      SEARCH_DEBOUNCE_MS,
    );
    return () => window.clearTimeout(searchTimer.current);
  }, [search]);

  const effectiveQuery = useMemo(
    () => (applied ? { ...query, search: applied } : query),
    [query, applied],
  );

  const { items, error, loading, loadingMore, hasMore, recordingMode, loadMore, reload } =
    useCalls(effectiveQuery, enabled);

  const toggleRow = useCallback((id: number) => {
    // One player at a time: two recordings playing over each other is never what the
    // reader meant, and unmounting the other player is what actually stops its audio.
    setExpanded((current) => (current === id ? null : id));
  }, []);

  /*
   * Which optional columns any loaded row actually fills.
   *
   * A portal with no CRM integration and no stored recordings was spending two of eight
   * columns on em dashes at every width. The dash is not a value: the column is dropped,
   * silently, because an absent column needs no explanation while a column of dashes
   * demands one. It comes back by itself if a later page brings the data with it.
   */
  const filled = useMemo(
    () => ({
      crm: items.some((call) => call.crm?.id !== null && call.crm?.id !== undefined),
      recording: items.some((call) => Boolean(call.has_record)),
    }),
    [items],
  );

  const columns = 5 + (showEmployee ? 1 : 0) + (filled.crm ? 1 : 0) + (filled.recording ? 1 : 0);

  if (error && items.length === 0) {
    return renderError ? (
      <>{renderError(error, reload)}</>
    ) : (
      <ErrorState error={error} onRetry={reload} />
    );
  }

  const caption = c('app.calls.table.caption');

  return (
    <section className="ca-card ca-calls">
      <style
        href={VIZ_STYLE_ID}
        precedence="default"
        dangerouslySetInnerHTML={{ __html: VIZ_CSS }}
      />
      <style href={STYLE_ID} precedence="default" dangerouslySetInnerHTML={{ __html: CSS }} />

      <div className="ca-calls-head">
        <div className="ca-calls-headline">
          <h2 className="ca-calls-title">{title ?? c('app.calls.table.title')}</h2>
          <span className="ca-calls-count">
            {c('app.calls.table.shown', { shown: formatCount(items.length, locale) })}
          </span>
        </div>
        {showSearch ? (
          <Input
            className="ca-calls-search"
            label={c('app.calls.table.search')}
            placeholder={c('app.calls.table.searchPlaceholder')}
            value={search}
            onValueChange={setSearch}
            clearable
            clearLabel={c('app.calls.table.searchClear')}
            leadingIcon={<SearchGlyph />}
            // `_SEARCH_MAX` in `api/app/api/calls.py`. Capping here rather than letting the
            // server answer `bad_search` is not cosmetic: a failed first page replaces this
            // whole section with an ErrorState, which would take away the very input the
            // reader was typing into.
            maxLength={64}
            disabled={!enabled}
            // Not `type="search"` (WebKit draws a second, native clear button beside ours)
            // and not `inputMode="tel"` (the fallback branch matches SIP addresses, and an
            // iOS keypad cannot type one). Plain text, without a saved-number autofill.
            autoComplete="off"
            inputClassName="ca-viz-num"
          />
        ) : null}
      </div>

      {narrow ? (
        <ul className="ca-cards" aria-label={caption}>
          {items.map((call) => (
            <CallCardView
              key={call.id}
              call={call}
              copy={c}
              locale={locale}
              timezone={timezone}
              showEmployee={showEmployee}
              expanded={expanded === call.id}
              recordingMode={recordingMode}
              onToggle={toggleRow}
            />
          ))}
          {loading && items.length === 0 ? <SkeletonCards /> : null}
        </ul>
      ) : (
        <div className="ca-calls-scroll">
          <table className="ca-tbl">
            <caption className="sr-only">{caption}</caption>
            <thead>
              <tr>
                {/* The order is stated, not offered: `GET /calls` serves
                    `call_start_date DESC` and §3 has no index for anything else. */}
                <th scope="col" aria-sort="descending">
                  <span className="ca-th-time" title={c('app.calls.table.timeOrder')}>
                    {c('app.calls.table.time')}
                    <DescendingGlyph />
                  </span>
                </th>
                {showEmployee ? <th scope="col">{c('app.calls.table.employee')}</th> : null}
                <th scope="col">{c('app.calls.table.direction')}</th>
                <th scope="col">{c('app.calls.table.number')}</th>
                {filled.crm ? <th scope="col">{c('app.calls.table.crm')}</th> : null}
                <th scope="col" className="ca-right">
                  {c('app.calls.table.duration')}
                </th>
                <th scope="col">{c('app.calls.table.result')}</th>
                {filled.recording ? <th scope="col">{c('app.calls.table.recording')}</th> : null}
              </tr>
            </thead>
            <tbody>
              {items.map((call) => (
                <Fragment key={call.id}>
                  <CallRowView
                    call={call}
                    copy={c}
                    locale={locale}
                    timezone={timezone}
                    showEmployee={showEmployee}
                    showCrm={filled.crm}
                    showRecording={filled.recording}
                    expanded={expanded === call.id}
                    onToggle={toggleRow}
                  />
                  {expanded === call.id ? (
                    <tr>
                      <td className="ca-playercell" colSpan={columns}>
                        <InlinePlayer
                          callId={call.id}
                          fallbackPath={recordingFallbackPath(call)}
                          recordingMode={recordingMode}
                        />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
              {loading && items.length === 0 ? <SkeletonRows columns={columns} /> : null}
            </tbody>
          </table>
        </div>
      )}

      {!loading && items.length === 0 ? (
        <p className="ca-empty">
          {/* The search explanation wins over the other two. The dashboard only renders
              this section when the period has calls in it, so "there are no calls in this
              period" is a sentence that is *false* whenever a search matched none of
              them - and §4.11 counts a state that misdescribes itself as a failure even
              when nothing errored. */}
          {applied
            ? c('app.calls.table.searchEmpty')
            : importing
              ? c('app.calls.table.emptyImporting')
              : c('app.calls.table.empty')}
        </p>
      ) : null}

      {items.length > 0 ? (
        <div className="ca-calls-foot">
          {hasMore ? (
            <button
              type="button"
              className="ca-button ca-button-quiet"
              onClick={loadMore}
              disabled={loadingMore}
            >
              {loadingMore ? c('app.calls.table.loadingMore') : c('app.calls.table.loadMore')}
            </button>
          ) : (
            <p className="ca-calls-note">{c('app.calls.table.allShown')}</p>
          )}
          {/* A "load more" that failed keeps the rows already on screen: they are still
              correct, and re-rendering the whole view as an error page would throw away
              work the reader can still use (§4.11 wants a state, not a blank frame). */}
          {error ? (
            <button type="button" className="ca-button ca-button-quiet" onClick={reload}>
              {c('app.retry')}
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

/** Everything both layouts need to say about one call, worked out once. */
function callFacts(call: CallRow, copy: Copy) {
  const crmType = call.crm?.type ?? null;
  const crmId = call.crm?.id ?? null;
  return {
    employee: describeEmployee(call, copy),
    direction: describeDirection(call, copy),
    result: describeResult(call, copy),
    group: resultGroupOf(call),
    crmType,
    crmPath: crmEntityPath(crmType, crmId),
    crmLabel:
      crmId === null || crmId === undefined ? null : `${copy(crmEntityKey(crmType))} #${crmId}`,
    line: lineLabel(call),
  };
}

interface CallRowViewProps {
  call: CallRow;
  copy: Copy;
  locale: string;
  timezone: string | null;
  showEmployee: boolean;
  showCrm: boolean;
  showRecording: boolean;
  expanded: boolean;
  onToggle: (id: number) => void;
}

function CallRowView({
  call,
  copy,
  locale,
  timezone,
  showEmployee,
  showCrm,
  showRecording,
  expanded,
  onToggle,
}: CallRowViewProps) {
  const { employee, direction, result, group, crmType, crmPath, crmLabel, line } =
    callFacts(call, copy);

  return (
    <tr className="ca-row">
      <td className="ca-viz-num ca-nowrap">
        {formatDateTime(call.call_start_date, locale, timezone)}
      </td>

      {showEmployee ? (
        <td>
          {employee ? (
            <span title={employee.title}>
              {employee.label}
              {call.employee?.active === false ? (
                // §7: dismissed users own historical calls; the row is real, the person
                // is gone, and hiding that would make the table lie.
                <span className="ca-badge">{copy('app.calls.table.dismissed')}</span>
              ) : null}
            </span>
          ) : (
            <span className="ca-muted">—</span>
          )}
        </td>
      ) : null}

      <td title={direction.title}>{direction.label}</td>

      <td>
        <span className="ca-viz-num ca-nowrap">{formatPhone(call.phone_number)}</span>
        {call.portal_number ? (
          <span className="ca-sub ca-viz-num" title={line ?? undefined}>
            {formatPhone(call.portal_number)}
          </span>
        ) : line ? (
          <span className="ca-sub">{line}</span>
        ) : null}
      </td>

      {showCrm ? (
        <td>
          {crmLabel === null ? (
            <span className="ca-muted">—</span>
          ) : crmPath === null ? (
            // A CRM type we have no slider route for (a dynamic entity a portal emits):
            // the label is still true, and a button that opened nothing would not be.
            <span title={crmType ?? undefined}>{crmLabel}</span>
          ) : (
            <button
              type="button"
              className="ca-linkbtn"
              onClick={() => {
                void openPath(crmPath);
              }}
            >
              {crmLabel}
            </button>
          )}
        </td>
      ) : null}

      <td className="ca-viz-num ca-right ca-nowrap">{formatDuration(call.duration)}</td>

      <td>
        <span className="ca-result" title={result.title}>
          <span
            className="ca-viz-swatch"
            style={{ background: seriesVar(group) }}
            aria-hidden="true"
          />
          {result.label}
        </span>
      </td>

      {showRecording ? (
        <td>
          {call.has_record ? (
            <button
              type="button"
              className="ca-playbtn"
              aria-expanded={expanded}
              title={expanded ? copy('app.calls.table.hide') : copy('app.calls.table.play')}
              aria-label={expanded ? copy('app.calls.table.hide') : copy('app.calls.table.play')}
              onClick={() => onToggle(call.id)}
            >
              {expanded ? <CloseGlyph /> : <PlayGlyph />}
            </button>
          ) : (
            <span className="ca-muted" title={copy('app.calls.table.noRecording')}>
              —
            </span>
          )}
        </td>
      ) : null}
    </tr>
  );
}

interface CallCardViewProps {
  call: CallRow;
  copy: Copy;
  locale: string;
  timezone: string | null;
  showEmployee: boolean;
  expanded: boolean;
  recordingMode: string | null;
  onToggle: (id: number) => void;
}

/**
 * One call as a card: when and who, then which way and which number, then the rest.
 *
 * The em-dash placeholders of the table have no equivalent here - a card simply omits
 * what a call does not have, because there is no column for it to leave a hole in.
 */
function CallCardView({
  call,
  copy,
  locale,
  timezone,
  showEmployee,
  expanded,
  recordingMode,
  onToggle,
}: CallCardViewProps) {
  const { employee, direction, result, group, crmType, crmPath, crmLabel, line } =
    callFacts(call, copy);

  return (
    <li className="ca-cc">
      <div className="ca-cc-body">
        <div className="ca-cc-head">
          <time className="ca-cc-when" dateTime={call.call_start_date}>
            {formatDateTime(call.call_start_date, locale, timezone)}
          </time>
          {showEmployee && employee ? (
            <span className="ca-cc-who" title={employee.title}>
              {employee.label}
              {call.employee?.active === false ? (
                <span className="ca-badge">{copy('app.calls.table.dismissed')}</span>
              ) : null}
            </span>
          ) : null}
        </div>

        <div className="ca-cc-main">
          <span className="ca-cc-number">{formatPhone(call.phone_number)}</span>
          <span className="ca-cc-dir" title={direction.title}>
            {direction.label}
          </span>
        </div>

        <div className="ca-cc-meta">
          <span className="ca-cc-dur">{formatDuration(call.duration)}</span>
          <span className="ca-result" title={result.title}>
            <span
              className="ca-viz-swatch"
              style={{ background: seriesVar(group) }}
              aria-hidden="true"
            />
            {result.label}
          </span>
          {call.portal_number ? (
            <span className="ca-cc-dur" title={line ?? undefined}>
              {formatPhone(call.portal_number)}
            </span>
          ) : line ? (
            <span>{line}</span>
          ) : null}
          {crmLabel === null ? null : crmPath === null ? (
            <span title={crmType ?? undefined}>{crmLabel}</span>
          ) : (
            <button
              type="button"
              className="ca-linkbtn"
              onClick={() => {
                void openPath(crmPath);
              }}
            >
              {crmLabel}
            </button>
          )}
        </div>
      </div>

      {call.has_record ? (
        <button
          type="button"
          className="ca-playbtn ca-cc-play"
          aria-expanded={expanded}
          title={expanded ? copy('app.calls.table.hide') : copy('app.calls.table.play')}
          aria-label={expanded ? copy('app.calls.table.hide') : copy('app.calls.table.play')}
          onClick={() => onToggle(call.id)}
        >
          {expanded ? <CloseGlyph /> : <PlayGlyph />}
        </button>
      ) : null}

      {expanded ? (
        <div className="ca-cc-player">
          <InlinePlayer
            callId={call.id}
            fallbackPath={recordingFallbackPath(call)}
            recordingMode={recordingMode}
          />
        </div>
      ) : null}
    </li>
  );
}

/** Content-shaped placeholder while the first page is in flight. */
function SkeletonRows({ columns }: { columns: number }) {
  return (
    <>
      {[0, 1, 2, 3, 4].map((row) => (
        <tr key={row} className="ca-row">
          {Array.from({ length: columns }, (_, cell) => (
            <td key={cell}>
              <div className="ca-skeleton h-4 w-full" />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

/** The same placeholder, in the shape the narrow layout actually renders. */
function SkeletonCards() {
  return (
    <>
      {[0, 1, 2, 3, 4].map((row) => (
        <li key={row} className="ca-cc">
          <div className="ca-cc-body">
            <div className="ca-skeleton h-3 w-2/3" />
            <div className="ca-skeleton mt-2 h-4 w-1/2" />
            <div className="ca-skeleton mt-2 h-3 w-3/4" />
          </div>
        </li>
      ))}
    </>
  );
}

function DescendingGlyph() {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 10 10"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M5 8L1.5 3.5h7z" />
    </svg>
  );
}

function SearchGlyph() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="5" cy="5" r="3.4" />
      <path d="M7.6 7.6L10.5 10.5" />
    </svg>
  );
}

function PlayGlyph() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M3.5 2.2l6 3.8-6 3.8z" />
    </svg>
  );
}

function CloseGlyph() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M3 6h6" />
    </svg>
  );
}

export default CallsTable;
