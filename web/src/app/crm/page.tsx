'use client';

import { useLocale, useTranslations } from 'next-intl';
import { useCallback } from 'react';

import {
  ErrorState,
  Field,
  FieldList,
  LoadingBlock,
  PageShell,
  Section,
} from '@/components/AppFrame';
import StateCard from '@/components/StateCard';
import { deniedBodyKey, useMe } from '@/lib/api';
import { openPath } from '@/lib/bx24';
import { crmEntityKey, crmEntityPath } from '@/lib/format';

/**
 * The CRM detail tab (`CRM_DEAL|LEAD|CONTACT|COMPANY_DETAIL_TAB`, §4.4).
 *
 * **Placeholder for milestone 3.** Milestone 5 fills it with the calls table and the
 * inline player, read through `crm_contexts` (§4.8). What is real here:
 *
 *  * the entity comes from the JWT `ent` claim as `GET /me` echoes it - never from the
 *    query string, because a user who cannot see the entity must not be able to name
 *    one (§4.4 step 5 mints no entity JWT after a CRM command error),
 *  * a stale context is a 409 `context_missing`, which `lib/api.ts` cures with one
 *    session exchange and one retry (§4.8),
 *  * the link back to Bitrix24 goes through `BX24.openPath()` (§4.10) and degrades to
 *    plain text when the SDK is unavailable.
 */
export default function CrmPage() {
  const t = useTranslations();
  const locale = useLocale();
  const { data, error, loading, reload } = useMe();

  const entity = data?.entity ?? null;
  const path = crmEntityPath(entity?.t, entity?.id);

  const open = useCallback(() => {
    if (path) {
      void openPath(path);
    }
  }, [path]);

  if (loading) {
    return <LoadingBlock label={t('app.loading')} />;
  }
  if (error || !data) {
    return <ErrorState error={error} onRetry={reload} />;
  }

  if (data.access === 'denied') {
    return (
      <StateCard kind="denied" title={t('state.denied.title')} body={t(deniedBodyKey(data))} />
    );
  }

  const entityLabel = entity
    ? `${t(crmEntityKey(entity.t))} #${entity.id}`
    : t('app.crm.noEntity');

  return (
    <PageShell
      title={t('app.crm.title')}
      subtitle={entityLabel}
      chips={
        <>
          <span className="ca-chip">{t(`app.access.${data.access}`)}</span>
          {data.placement ? <span className="ca-chip">{data.placement}</span> : null}
        </>
      }
      banner={data.access === 'own' ? <span>{t('app.ownScopeBanner')}</span> : undefined}
    >
      <Section title={t('app.session.title')}>
        <FieldList>
          <Field label={t('app.session.user')} value={`#${data.user_id}`} />
          <Field label={t('app.session.access')} value={t(`app.access.${data.access}`)} />
          <Field label={t('app.session.placement')} value={data.placement ?? '—'} />
          <Field
            label={t('app.session.entity')}
            value={
              path ? (
                <button type="button" className="ca-button ca-button-quiet" onClick={open}>
                  {entityLabel}
                </button>
              ) : (
                entityLabel
              )
            }
          />
          <Field label={t('app.session.timezone')} value={data.timezone ?? '—'} />
          <Field label={t('app.session.language')} value={data.locale ?? locale} />
        </FieldList>
      </Section>

      <p className="ca-muted text-[13px]">{t('app.preview.body')}</p>
    </PageShell>
  );
}
