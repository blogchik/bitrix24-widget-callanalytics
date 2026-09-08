'use client';

/**
 * The CRM detail tab (`CRM_DEAL|LEAD|CONTACT|COMPANY_DETAIL_TAB`, §4.4, §4.8).
 *
 * One question, one answer: **the calls of this entity**. No charts, no employee or
 * result filters, nothing but the period - a deal card is not a place to explore a
 * portal's telephony, it is a place to see who called this customer and to listen to it.
 *
 * The entity is never taken from the query string. It comes from the JWT `ent` claim
 * that `GET /me` echoes, because §4.4 step 5 mints **no** entity JWT when a CRM command
 * failed: a user who cannot see the entity must not be able to name one. The client
 * therefore sends no entity parameter at all - §4.8 matches server-side from `ent` plus
 * the cached `crm_contexts` row, and `scope_filter` still applies on top.
 *
 * Two failure paths are specific to this page:
 *
 *  * **409 `context_missing`** (§4.8: the cached context is older than the session) is
 *    already cured by `lib/api.ts`, which runs the session exchange and retries the
 *    request exactly once. If it comes back a second time the context genuinely cannot
 *    be resolved for this viewer, and that is not an empty table - it is
 *    §4.11's "no access to this CRM item".
 *  * **`crm_no_access`** for the same reason, said explicitly by the backend.
 */
import { useTranslations } from 'next-intl';
import { useCallback, useMemo, useState } from 'react';

import { ErrorState, LoadingBlock, PageShell } from '@/components/AppFrame';
import CallsTable from '@/components/CallsTable';
import StateCard from '@/components/StateCard';
import { ApiError, deniedBodyKey, useMe } from '@/lib/api';
import { openPath } from '@/lib/bx24';
import { periodRange, useCopy, type CallsQuery, type PeriodId } from '@/lib/calls';
import { crmEntityKey, crmEntityPath } from '@/lib/format';

/** The CRM tab's only control. Ordered shortest first; §4.8 caps the longest at 366 d. */
const PERIODS: readonly PeriodId[] = ['30d', '90d', '12m'];

const STYLE_ID = 'ca-crm-page';

/** The one style this page owns: the entity link in the header. */
const CSS = `
/* Drawn as a link, sized as a target: the glyphs are 20px tall, which is under the touch
 * floor on the surface this page exists for - a CRM card opened on a phone. The height
 * comes from the control token and the negative margin keeps the text where the header
 * puts it, so the box grows without the layout moving. */
.ca-entity-link {
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
  cursor: pointer;
  transition: background-color var(--ca-dur-fast) var(--ca-ease);
}
.ca-entity-link:hover {
  background: var(--ca-accent-soft);
  text-decoration: underline;
}
`;

/** Codes that mean "this viewer cannot see this entity" rather than "no rows". */
const NO_ACCESS_CODES: ReadonlySet<string> = new Set(['crm_no_access', 'context_missing']);

export default function CrmPage() {
  const t = useTranslations();
  const c = useCopy();
  const { data, error, loading, reload } = useMe();

  // A card is opened to see its whole history, not this week's: the entity's call count
  // is small by construction, so the default is the longest period we are allowed.
  const [period, setPeriod] = useState<PeriodId>('12m');

  const entity = data?.entity ?? null;
  const entityPath = crmEntityPath(entity?.t, entity?.id);

  const openEntity = useCallback(() => {
    if (entityPath) {
      void openPath(entityPath);
    }
  }, [entityPath]);

  const range = useMemo(() => periodRange(period, data?.timezone), [period, data?.timezone]);
  // `period: 'custom'` explicitly: `parse_filters` reads `from`/`to` under that preset
  // only, and a range sent without it would silently be answered for seven days.
  const query = useMemo<CallsQuery>(
    () => ({ period: 'custom', from: range.from, to: range.to }),
    [range.from, range.to],
  );

  const renderTableError = useCallback(
    (cause: unknown, retry: () => void) => {
      const code = cause instanceof ApiError ? cause.code : '';
      if (NO_ACCESS_CODES.has(code)) {
        return (
          <StateCard
            kind="crm_no_access"
            title={t('state.crm_no_access.title')}
            body={t('state.crm_no_access.body')}
          />
        );
      }
      return <ErrorState error={cause} onRetry={retry} />;
    },
    [t],
  );

  if (loading) {
    return <LoadingBlock label={t('app.loading')} />;
  }
  if (error || !data) {
    return <ErrorState error={error} onRetry={reload} />;
  }

  // §4.7: `denied` is a state with mandated copy, not an error, and `GET /me` is the
  // one endpoint that still answers for such a user.
  if (data.access === 'denied') {
    return (
      <StateCard kind="denied" title={t('state.denied.title')} body={t(deniedBodyKey(data))} />
    );
  }

  // A CRM placement with no `ent` claim means the open-time batch never resolved an
  // entity for this viewer (§4.4 step 5). An empty table would read as "no calls".
  if (!entity) {
    return (
      <StateCard
        kind="crm_no_access"
        title={t('state.crm_no_access.title')}
        body={t('state.crm_no_access.body')}
      />
    );
  }

  const entityLabel = `${t(crmEntityKey(entity.t))} #${entity.id}`;

  return (
    <PageShell
      title={t('app.crm.title')}
      subtitle={
        <>
          <style href={STYLE_ID} precedence="default" dangerouslySetInnerHTML={{ __html: CSS }} />
          {entityPath ? (
            <button type="button" className="ca-entity-link" onClick={openEntity}>
              {entityLabel}
            </button>
          ) : (
            entityLabel
          )}
        </>
      }
      chips={
        <PeriodPicker
          value={period}
          onChange={setPeriod}
          label={c('app.period.label')}
          copy={c}
        />
      }
      banner={data.access === 'own' ? <span>{t('app.ownScopeBanner')}</span> : undefined}
    >
      <CallsTable
        query={query}
        timezone={data.timezone}
        // Every row here belongs to whoever handled the call, and on a customer card
        // that is exactly the column a salesperson looks for first.
        showEmployee
        importing={Boolean(data.sync?.importing)}
        renderError={renderTableError}
      />
    </PageShell>
  );
}

interface PeriodPickerProps {
  value: PeriodId;
  onChange: (value: PeriodId) => void;
  label: string;
  copy: (key: string) => string;
}

/**
 * The period control.
 *
 * A row of buttons rather than a `<select>`: three options, and inside a slider a
 * native dropdown opens against the parent window's edge.
 */
function PeriodPicker({ value, onChange, label, copy }: PeriodPickerProps) {
  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label={label}>
      {PERIODS.map((period) => (
        <button
          key={period}
          type="button"
          className={period === value ? 'ca-button' : 'ca-button ca-button-quiet'}
          aria-pressed={period === value}
          onClick={() => onChange(period)}
        >
          {copy(`app.period.${period}`)}
        </button>
      ))}
    </div>
  );
}
