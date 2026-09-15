'use client';

/**
 * What a report built from the CRM mirror can promise (§4.14 constraint 4).
 *
 * A mirror still loading history, or one whose sweeps have fallen behind, draws numbers that
 * look complete and are not. This says so beside the tables, in the note style the Deals and
 * Sources pages already use, and renders nothing at all when there is nothing to warn about -
 * which is also every live report, since those carry no coverage block.
 */

import type { useTranslations } from 'next-intl';

import type { MirrorReportMeta } from '@/lib/api';
import { formatDateTime } from '@/lib/format';

interface CrmCoverageNoticeProps {
  report: MirrorReportMeta;
  locale: string;
  /** The viewer's zone from `/me`: a portal in another region reads its own clock. */
  timeZone: string | null;
  t: ReturnType<typeof useTranslations>;
  /** The page's own note class, so the sentence sits with the notes it belongs to. */
  className: string;
}

export default function CrmCoverageNotice({
  report,
  locale,
  timeZone,
  t,
  className,
}: CrmCoverageNoticeProps) {
  const notes: { key: string; text: string }[] = [];

  if (report.coverage && !report.coverage.history_complete) {
    notes.push({
      key: 'loading',
      text: t('app.crmCoverage.loading', { percent: report.coverage.progress_pct }),
    });
  }
  if (report.stale) {
    notes.push(
      report.stale.since
        ? {
            key: 'stale',
            text: t('app.crmCoverage.stale', {
              time: formatDateTime(report.stale.since, locale, timeZone),
            }),
          }
        : { key: 'never', text: t('app.crmCoverage.neverSynced') },
    );
  }

  if (notes.length === 0) {
    return null;
  }
  return (
    <>
      {notes.map((note) => (
        <p key={note.key} className={className} role="status">
          {note.text}
        </p>
      ))}
    </>
  );
}
