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
 */
import { useLocale } from 'next-intl';
import { Fragment, useCallback, useMemo, useState, type ReactNode } from 'react';

import { ErrorState } from '@/components/AppFrame';
import InlinePlayer from '@/components/InlinePlayer';
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

const CSS = `
.ca-calls {
  padding: 0;
  overflow: hidden;
}
.ca-calls-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px 12px;
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
  padding: 9px 12px;
  border-bottom: 1px solid var(--ca-border-soft);
  vertical-align: top;
}
.ca-tbl th:first-child,
.ca-tbl td:first-child {
  padding-left: 18px;
}
.ca-tbl th:last-child,
.ca-tbl td:last-child {
  padding-right: 18px;
}
.ca-tbl tbody tr.ca-row:hover td {
  background: var(--ca-surface-soft);
}
.ca-th-time {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.ca-right {
  text-align: right;
}
.ca-sub {
  display: block;
  margin-top: 2px;
  font-size: 12px;
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
.ca-linkbtn {
  padding: 0;
  border: 0;
  background: none;
  font: inherit;
  color: var(--ca-accent);
  text-align: left;
  cursor: pointer;
}
.ca-linkbtn:hover {
  text-decoration: underline;
}
.ca-playbtn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 999px;
  border: 1px solid var(--ca-border);
  background: var(--ca-surface);
  color: var(--ca-accent);
  cursor: pointer;
}
.ca-playbtn[aria-expanded='true'] {
  background: var(--ca-accent-soft);
}
.ca-playercell {
  background: var(--ca-page);
  padding: 4px 18px 14px;
}
.ca-calls-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 18px;
}
.ca-calls-note {
  margin: 0;
  font-size: 13px;
  color: var(--ca-muted);
}
.ca-empty {
  padding: 26px 18px 30px;
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
   * Page-level takeover of a failed first page.
   *
   * The CRM tab uses it to render §4.11's `crm_no_access` state instead of an empty
   * table; everything else falls through to the shared `ErrorState`.
   */
  renderError?: (error: unknown, retry: () => void) => ReactNode;
}

/** The table. The page owns the period and the filters; this owns the paging. */
export function CallsTable({
  query,
  timezone = null,
  title,
  showEmployee = true,
  importing = false,
  enabled = true,
  renderError,
}: CallsTableProps) {
  const c = useCopy();
  const locale = useLocale();

  const [expanded, setExpanded] = useState<number | null>(null);

  const { items, error, loading, loadingMore, hasMore, recordingMode, loadMore, reload } =
    useCalls(query, enabled);

  const toggleRow = useCallback((id: number) => {
    // One player at a time: two recordings playing over each other is never what the
    // reader meant, and unmounting the other player is what actually stops its audio.
    setExpanded((current) => (current === id ? null : id));
  }, []);

  const columns = useMemo(() => (showEmployee ? 8 : 7), [showEmployee]);

  if (error && items.length === 0) {
    return renderError ? (
      <>{renderError(error, reload)}</>
    ) : (
      <ErrorState error={error} onRetry={reload} />
    );
  }

  return (
    <section className="ca-card ca-calls">
      <style
        href={VIZ_STYLE_ID}
        precedence="default"
        dangerouslySetInnerHTML={{ __html: VIZ_CSS }}
      />
      <style href={STYLE_ID} precedence="default" dangerouslySetInnerHTML={{ __html: CSS }} />

      <div className="ca-calls-head">
        <h2 className="ca-calls-title">{title ?? c('app.calls.table.title')}</h2>
        <span className="ca-calls-count">
          {c('app.calls.table.shown', { shown: formatCount(items.length, locale) })}
        </span>
      </div>

      <div className="ca-calls-scroll">
        <table className="ca-tbl">
          <caption className="sr-only">{c('app.calls.table.caption')}</caption>
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
              <th scope="col">{c('app.calls.table.crm')}</th>
              <th scope="col" className="ca-right">
                {c('app.calls.table.duration')}
              </th>
              <th scope="col">{c('app.calls.table.result')}</th>
              <th scope="col">{c('app.calls.table.recording')}</th>
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

      {!loading && items.length === 0 ? (
        <p className="ca-empty">
          {importing ? c('app.calls.table.emptyImporting') : c('app.calls.table.empty')}
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

interface CallRowViewProps {
  call: CallRow;
  copy: Copy;
  locale: string;
  timezone: string | null;
  showEmployee: boolean;
  expanded: boolean;
  onToggle: (id: number) => void;
}

function CallRowView({
  call,
  copy,
  locale,
  timezone,
  showEmployee,
  expanded,
  onToggle,
}: CallRowViewProps) {
  const employee = describeEmployee(call, copy);
  const direction = describeDirection(call, copy);
  const result = describeResult(call, copy);
  const group = resultGroupOf(call);
  const crmType = call.crm?.type ?? null;
  const crmId = call.crm?.id ?? null;
  const crmPath = crmEntityPath(crmType, crmId);
  const crmLabel =
    crmId === null || crmId === undefined ? null : `${copy(crmEntityKey(crmType))} #${crmId}`;
  const line = lineLabel(call);

  return (
    <tr className="ca-row">
      <td className="ca-viz-num">{formatDateTime(call.call_start_date, locale, timezone)}</td>

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
        <span className="ca-viz-num">{formatPhone(call.phone_number)}</span>
        {call.portal_number ? (
          <span className="ca-sub ca-viz-num" title={line ?? undefined}>
            {formatPhone(call.portal_number)}
          </span>
        ) : line ? (
          <span className="ca-sub">{line}</span>
        ) : null}
      </td>

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

      <td className="ca-viz-num ca-right">{formatDuration(call.duration)}</td>

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
    </tr>
  );
}

/** Content-shaped placeholder while the first page is in flight. */
function SkeletonRows({ columns }: { columns: number }) {
  return (
    <>
      {[0, 1, 2, 3, 4].map((row) => (
        <tr key={row}>
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
