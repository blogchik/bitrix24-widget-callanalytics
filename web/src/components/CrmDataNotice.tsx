'use client';

/**
 * The informational CRM notice for administrators (D-7, docs/crm-mirror-notice.md sections 1-2).
 *
 * It informs and gates nothing: storage started with the release. What it owes an administrator
 * is the one decision that is theirs - turning CRM analytics off, which deletes the portal's CRM
 * copy - behind a confirmation, and a way to stop seeing the notice.
 *
 * The sentences are the owner-approved ones. A change of wording starts in that document.
 */

import { useTranslations } from 'next-intl';
import { useState } from 'react';

import { dismissCrmNotice, setCrmAnalytics, type MeCrm } from '@/lib/api';

type Step = 'notice' | 'confirm' | 'busy' | 'failed' | 'hidden';

export interface CrmDataNoticeProps {
  crm: MeCrm | null | undefined;
  isAdmin: boolean;
  /** Called once CRM analytics is off, so the page can re-read `/me`. */
  onChanged?: () => void;
}

export function CrmDataNotice({ crm, isAdmin, onChanged }: CrmDataNoticeProps) {
  const t = useTranslations();
  const [step, setStep] = useState<Step>('notice');

  if (!isAdmin || !crm?.notice_visible || step === 'hidden') {
    return null;
  }

  const dismiss = () => {
    setStep('hidden');
    // Best effort: the notice is informational, so a dismissal that did not reach the server
    // only means it is shown once more, never that anything is stored or deleted.
    void dismissCrmNotice().catch(() => undefined);
  };

  const turnOff = async () => {
    setStep('busy');
    try {
      await setCrmAnalytics(false);
      setStep('hidden');
      onChanged?.();
    } catch {
      setStep('failed');
    }
  };

  if (step !== 'notice') {
    return (
      <div className="ca-banner" role="alertdialog" aria-labelledby="ca-crm-confirm-title">
        <p id="ca-crm-confirm-title" className="font-semibold">
          {t('app.crmNotice.confirmTitle')}
        </p>
        <p className="mt-1">{t('app.crmNotice.confirmBody')}</p>
        {step === 'failed' ? <p className="mt-2 text-[13px]">{t('app.crmNotice.failed')}</p> : null}
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            className="ca-button"
            disabled={step === 'busy'}
            onClick={() => void turnOff()}
          >
            {t('app.crmNotice.confirmYes')}
          </button>
          <button
            type="button"
            className="ca-button ca-button-quiet"
            disabled={step === 'busy'}
            onClick={() => setStep('notice')}
          >
            {t('app.crmNotice.confirmNo')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="ca-banner" role="status">
      <p className="font-semibold">{t('app.crmNotice.title')}</p>
      <p className="mt-1">{t('app.crmNotice.body')}</p>
      <p className="mt-1">{t('app.crmNotice.optOut')}</p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <a
          className="ca-button ca-button-quiet"
          href="/privacy"
          target="_blank"
          rel="noopener noreferrer"
        >
          {t('app.crmNotice.privacy')}
        </a>
        <button
          type="button"
          className="ca-button ca-button-quiet"
          onClick={() => setStep('confirm')}
        >
          {t('app.crmNotice.turnOff')}
        </button>
        <button type="button" className="ca-button" onClick={dismiss}>
          {t('app.crmNotice.dismiss')}
        </button>
      </div>
    </div>
  );
}

export default CrmDataNotice;
