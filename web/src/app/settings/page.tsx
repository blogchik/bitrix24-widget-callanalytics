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
import { useMe } from '@/lib/api';
import { formatCount, formatDateTime } from '@/lib/format';

/**
 * The admin status page (`POST /settings/`, §4.5).
 *
 * **Placeholder for milestone 3.** §4.5's full page - placement bind results, the
 * quarantined-row count, `capabilities`, and the "Re-authorize" / "Re-bind" buttons -
 * needs `GET /portal/sync-status` and `POST /portal/reauthorize`, which arrive with
 * their milestones. What `GET /me` already carries is the sync summary and
 * `token_status`, and that is what this page renders.
 *
 * The admin check is repeated here even though `POST /settings/` renders the
 * "administrators only" state server-side for a non-admin (§4.5): the JWT is the only
 * authority on who the viewer is (§4.7), and a page that shows a portal's token status
 * should not depend on a redirect having happened upstream.
 */
export default function SettingsPage() {
  const t = useTranslations();
  const locale = useLocale();
  const { data, error, loading, reload } = useMe();

  if (loading) {
    return <LoadingBlock label={t('app.loading')} />;
  }
  if (error || !data) {
    return <ErrorState error={error} onRetry={reload} />;
  }

  if (!data.is_admin) {
    return (
      <StateCard
        kind="admin_only"
        title={t('state.admin_only.title')}
        body={t('state.admin_only.body')}
      />
    );
  }

  const sync = data.sync ?? null;
  const total = sync?.backfill_total ?? null;

  return (
    <PageShell
      title={t('app.settings.title')}
      subtitle={t('app.signedInAs', { id: String(data.user_id) })}
      chips={
        sync?.token_status ? <span className="ca-chip">{sync.token_status}</span> : undefined
      }
    >
      <Section title={t('app.settings.tokenSection')}>
        <FieldList>
          <Field label={t('app.settings.tokenStatus')} value={sync?.token_status ?? '—'} />
        </FieldList>
        <p className="ca-muted mt-3 text-[13px]">{t('app.settings.actionsLater')}</p>
      </Section>

      <Section title={t('app.sync.title')}>
        <FieldList>
          <Field label={t('app.sync.status')} value={sync?.backfill_status ?? '—'} />
          <Field
            label={t('app.sync.progress')}
            value={
              total === null
                ? '—'
                : `${formatCount(sync?.backfill_done ?? 0, locale)} / ${formatCount(total, locale)}`
            }
          />
          <Field
            label={t('app.sync.lastRun')}
            value={formatDateTime(sync?.last_incremental_at, locale, data.timezone)}
          />
          {/* Admin-only field: support vocabulary, shown verbatim because it is a
              machine code, not a sentence, and the admin quotes it to support (§6). */}
          <Field label={t('app.sync.lastErrorCode')} value={sync?.last_error_code ?? '—'} />
        </FieldList>
      </Section>

      <Section title={t('app.session.title')}>
        <FieldList>
          <Field label={t('app.session.user')} value={`#${data.user_id}`} />
          <Field label={t('app.session.access')} value={t(`app.access.${data.access}`)} />
          <Field label={t('app.session.placement')} value={data.placement ?? '—'} />
          <Field label={t('app.session.timezone')} value={data.timezone ?? '—'} />
          <Field label={t('app.session.language')} value={data.locale ?? locale} />
        </FieldList>
      </Section>
    </PageShell>
  );
}
