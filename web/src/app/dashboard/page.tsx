'use client';

import { useLocale, useTranslations } from 'next-intl';

import {
  ErrorState,
  Field,
  FieldList,
  LoadingBlock,
  PageShell,
  Section,
} from '@/components/AppFrame';
import StateCard from '@/components/StateCard';
import { deniedBodyKey, useMe, type Me } from '@/lib/api';
import { formatCount, formatDateTime } from '@/lib/format';

/**
 * The left-menu view (`PLACEMENT` `DEFAULT` / `LEFT_MENU`, §4.4).
 *
 * **Placeholder for milestone 3.** Milestone 5 replaces the body with the summary
 * cards, the per-day chart, the hour x weekday heatmap, the per-employee bars and the
 * calls table. What is real here is the plumbing those views sit on, and it is exactly
 * the part that is easy to get wrong later:
 *
 *  * the session token comes from the URL fragment and never appears anywhere else
 *    (§4.6, `lib/session.ts`),
 *  * `GET /api/v1/me` goes through the bearer wrapper that retries once through the
 *    session exchange (§4.6, §4.8),
 *  * loading, error and `acc='denied'` are rendered states, not blank frames (§4.11),
 *  * `acc='own'` shows the mandated "your own calls only" banner (§4.7),
 *  * the "importing history" banner of §4.11 is driven by the sync summary `GET /me`
 *    already returns, so the real dashboard needs no extra round trip for it.
 */
export default function DashboardPage() {
  const t = useTranslations();
  const locale = useLocale();
  const { data, error, loading, reload } = useMe();

  if (loading) {
    return <LoadingBlock label={t('app.loading')} />;
  }
  if (error || !data) {
    return <ErrorState error={error} onRetry={reload} />;
  }

  // §4.7: `denied` is not an error - it is a state with mandated copy, and `GET /me`
  // is the one endpoint that still answers for such a user.
  if (data.access === 'denied') {
    return (
      <StateCard kind="denied" title={t('state.denied.title')} body={t(deniedBodyKey(data))} />
    );
  }

  const importing = importText(data, locale, t);

  return (
    <PageShell
      title={t('app.dashboard.title')}
      subtitle={t('app.signedInAs', { id: String(data.user_id) })}
      chips={
        <>
          <span className="ca-chip">{t(`app.access.${data.access}`)}</span>
          {data.placement ? <span className="ca-chip">{data.placement}</span> : null}
        </>
      }
      banner={
        importing ? (
          <span>{importing}</span>
        ) : data.access === 'own' ? (
          <span>{t('app.ownScopeBanner')}</span>
        ) : undefined
      }
    >
      <Section title={t('app.session.title')}>
        <FieldList>
          <Field label={t('app.session.user')} value={`#${data.user_id}`} />
          <Field label={t('app.session.access')} value={t(`app.access.${data.access}`)} />
          <Field label={t('app.session.placement')} value={data.placement ?? '—'} />
          <Field label={t('app.session.timezone')} value={data.timezone ?? '—'} />
          <Field label={t('app.session.language')} value={data.locale ?? locale} />
        </FieldList>
      </Section>

      <Section title={t('app.sync.title')}>
        <FieldList>
          <Field label={t('app.sync.status')} value={syncStatusText(data, locale, t)} />
          <Field
            label={t('app.sync.lastRun')}
            value={formatDateTime(data.sync?.last_incremental_at, locale, data.timezone)}
          />
        </FieldList>
      </Section>

      <p className="ca-muted text-[13px]">{t('app.preview.body')}</p>
    </PageShell>
  );
}

type Translate = ReturnType<typeof useTranslations>;

/**
 * §4.11: "Importing history 12 300 / 250 000" while the backfill is still running.
 *
 * `null` once the history is complete, which is what makes an empty period mean "no
 * calls in this period" rather than "not imported yet".
 */
function importText(me: Me, locale: string, t: Translate): string | null {
  const sync = me.sync;
  if (!sync?.importing) {
    return null;
  }
  const total = sync.backfill_total ?? null;
  if (total === null) {
    return t('app.sync.pending');
  }
  return t('app.sync.importing', {
    done: formatCount(sync.backfill_done ?? 0, locale),
    total: formatCount(total, locale),
  });
}

function syncStatusText(me: Me, locale: string, t: Translate): string {
  const importing = importText(me, locale, t);
  if (importing) {
    return importing;
  }
  return me.sync?.backfill_status === 'done' ? t('app.sync.done') : t('app.sync.pending');
}
