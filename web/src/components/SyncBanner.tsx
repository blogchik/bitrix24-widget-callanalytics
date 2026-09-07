'use client';

/**
 * The sync banner above the dashboard (§4.11, §5.8).
 *
 * It reads the `sync` summary `GET /api/v1/me` already carries, so the dashboard needs
 * no second round trip to answer the two questions an empty or thin page provokes:
 *
 *  1. **"Where are my calls?"** While the backfill runs, §4.11 mandates
 *     "Importing history 12 300 / 250 000" with visible progress. This is what makes an
 *     empty period mean *"no calls then"* rather than *"not imported yet"* - and it is
 *     why the empty state downstream changes its wording while `importing` is true.
 *  2. **"Why did it stop?"** `portals.token_status` is the one column that explains a
 *     stalled sync (§3). Each of its four non-ok values gets its own sentence, and an
 *     administrator - only an administrator - gets a way to act on it.
 *
 * A regular employee never sees a machine code or a support instruction they cannot
 * carry out; they see what is happening and nothing they would have to escalate blind.
 */

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { useEffect, useState } from 'react';

import type { MeSync } from '@/lib/api';
import { formatCount } from '@/lib/format';

/** `portals_token_status_chk` (§3): the values that mean "sync is not running". */
const TOKEN_ISSUES: ReadonlySet<string> = new Set([
  'reauth_required',
  'no_stats_permission',
  'method_missing',
  'filter_unsupported',
]);

export interface SyncBannerProps {
  sync: MeSync | null | undefined;
  /** Only an administrator is offered the settings page (§4.5). */
  isAdmin: boolean;
  locale: string;
}

export function SyncBanner({ sync, isAdmin, locale }: SyncBannerProps) {
  const t = useTranslations();
  const [settingsHref, setSettingsHref] = useState('/settings');

  // The Bitrix24 query string (`APP_SID` above all) has to survive the navigation or
  // `BX24.init` will never fire on the settings page (§4.4 step 8).
  useEffect(() => {
    setSettingsHref(`/settings${window.location.search}`);
  }, []);

  const status = sync?.token_status ?? null;
  const issue = status && TOKEN_ISSUES.has(status) ? status : null;

  if (issue) {
    return (
      <div className="ca-banner flex flex-wrap items-center gap-x-4 gap-y-2" role="status">
        <span className="flex-1">{t(`app.sync.tokenIssue.${issue}`)}</span>
        {isAdmin ? (
          <Link className="ca-button ca-button-quiet" href={settingsHref}>
            {t('app.sync.openSettings')}
          </Link>
        ) : null}
      </div>
    );
  }

  if (!sync?.importing) {
    return null;
  }

  const done = sync.backfill_done ?? 0;
  const totalRows = sync.backfill_total ?? null;
  // `backfill_total` is only known after `head_fetch` (§5.2); until then progress is a
  // sentence, not a bar, because a bar with an invented denominator is worse than none.
  const share = totalRows && totalRows > 0 ? Math.min(1, done / totalRows) : null;

  return (
    <div className="ca-banner" role="status">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="ca-viz-num">
          {totalRows === null
            ? t('app.sync.pending')
            : t('app.sync.importingShort', {
                done: formatCount(done, locale),
                total: formatCount(totalRows, locale),
              })}
        </span>
        {share !== null ? (
          <span className="ca-viz-num ca-muted text-[12px]">{Math.round(share * 100)}%</span>
        ) : null}
      </div>
      {share !== null ? (
        <div
          className="mt-2 overflow-hidden"
          style={{ height: 6, borderRadius: 999, background: 'var(--ca-border)' }}
          role="progressbar"
          aria-label={t('app.sync.progressLabel')}
          aria-valuemin={0}
          aria-valuemax={totalRows ?? 0}
          aria-valuenow={done}
        >
          <div
            style={{
              width: `${Math.max(share * 100, 1)}%`,
              height: '100%',
              background: 'var(--ca-accent)',
            }}
          />
        </div>
      ) : null}
    </div>
  );
}

export default SyncBanner;
