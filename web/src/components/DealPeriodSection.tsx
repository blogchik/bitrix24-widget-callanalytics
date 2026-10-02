/**
 * The settings section where an administrator says which stages of the Deals report count a
 * deal in a period by more than its creation (owner decision 3, 2026-10-02).
 *
 * A deal belongs to the period it was created in. On a stage named here it ALSO belongs to the
 * period in which it moved onto that stage or was modified - the owner asked for this for
 * «Успешные» and «Заклад». The choice is per stage rather than per outcome on purpose: a portal
 * can hold a won stage whose deals arrived there in bulk, as portal 1's «База» did on
 * 2026-09-14, and a rule of "every won stage" would count all of them as that month's sales.
 *
 * One picker per funnel, because a stage belongs to exactly one funnel and two funnels' stages
 * may share a name («Успешний» is in both of portal 1's). One Save for the whole rule, because
 * it is one decision, and the server audits it once to `portal_events`.
 */
'use client';

import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { Section } from '@/components/AppFrame';
import { MultiSelect, type SelectOption } from '@/components/ui';
import {
  fetchDealPeriodRule,
  setDealPeriodRule,
  type DealPeriodFunnel,
  type DealPeriodRule,
} from '@/lib/api';

type SaveState = 'idle' | 'busy' | 'saved' | 'failed';

function sameKeys(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) {
    return false;
  }
  const wanted = new Set(left);
  return right.every((key) => wanted.has(key));
}

export default function DealPeriodSection() {
  const t = useTranslations();
  const [data, setData] = useState<DealPeriodRule | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<readonly string[]>([]);
  const [save, setSave] = useState<SaveState>('idle');

  useEffect(() => {
    const controller = new AbortController();
    setFailed(false);
    fetchDealPeriodRule(controller.signal)
      .then((next) => {
        setData(next);
        setSelected(next.stage_keys);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setFailed(true);
        }
      });
    return () => controller.abort();
  }, [attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  const dirty = useMemo(
    () => data !== null && !sameKeys(data.stage_keys, selected),
    [data, selected],
  );

  const onSave = useCallback(async () => {
    setSave('busy');
    try {
      const next = await setDealPeriodRule(selected);
      setData(next);
      setSelected(next.stage_keys);
      setSave('saved');
    } catch {
      setSave('failed');
    }
  }, [selected]);

  /** One funnel's picker writes only its own keys and keeps every other funnel's. */
  const onFunnelChange = useCallback((funnel: DealPeriodFunnel, next: string[]) => {
    const own = new Set(funnel.stages.map((stage) => stage.key));
    setSave('idle');
    setSelected((current) => [...current.filter((key) => !own.has(key)), ...next]);
  }, []);

  const optionsOf = useCallback(
    (funnel: DealPeriodFunnel): SelectOption[] =>
      funnel.stages.map((stage) => ({
        value: stage.key,
        // A stage NAME is portal data; only the fallback for an unnamed one is a key.
        label: stage.name || t('app.deals.unknownStage', { id: stage.key.slice(stage.key.indexOf(':') + 1) }),
        hint:
          stage.semantic === 'S'
            ? t('app.settings.dealPeriod.won')
            : stage.semantic === 'F'
              ? t('app.settings.dealPeriod.lost')
              : undefined,
      })),
    [t],
  );

  if (failed) {
    return (
      <Section title={t('app.settings.dealPeriod.section')}>
        <p className="text-[14px]">{t('app.settings.dealPeriod.loadFailed')}</p>
        <div className="ca-actionrow">
          <button type="button" className="ca-button" onClick={reload}>
            {t('app.settings.dealPeriod.retry')}
          </button>
        </div>
      </Section>
    );
  }

  if (!data) {
    return (
      <Section title={t('app.settings.dealPeriod.section')}>
        <p className="ca-muted text-[13px]">{t('app.settings.dealPeriod.loading')}</p>
      </Section>
    );
  }

  return (
    <Section title={t('app.settings.dealPeriod.section')}>
      <p className="ca-muted mt-1 text-[13px]">{t('app.settings.dealPeriod.hint')}</p>
      {!data.available ? (
        <p className="ca-muted mt-3 text-[13px]">{t('app.settings.dealPeriod.unavailable')}</p>
      ) : (
        <>
          <div className="mt-3 grid items-end gap-3 [grid-template-columns:repeat(auto-fit,minmax(220px,1fr))]">
            {data.funnels.map((funnel) => {
              const own = new Set(funnel.stages.map((stage) => stage.key));
              return (
                <MultiSelect
                  key={funnel.category_id}
                  label={funnel.name || t('app.deals.unknownFunnel', { id: funnel.category_id })}
                  values={selected.filter((key) => own.has(key))}
                  onChange={(next) => onFunnelChange(funnel, next)}
                  options={optionsOf(funnel)}
                  allLabel={t('app.settings.dealPeriod.none')}
                  summaryLabel={(count) => t('app.settings.dealPeriod.selected', { count })}
                  emptyText={t('app.settings.dealPeriod.noStages')}
                  disabled={save === 'busy'}
                />
              );
            })}
          </div>
          <div className="ca-actionrow">
            <button
              type="button"
              className="ca-button"
              disabled={!dirty || save === 'busy'}
              onClick={() => void onSave()}
            >
              {save === 'busy' ? t('app.settings.dealPeriod.saving') : t('app.settings.dealPeriod.save')}
            </button>
          </div>
          {save === 'saved' ? (
            <p className="mt-2 text-[13px]" role="status">
              {t('app.settings.dealPeriod.saved')}
            </p>
          ) : null}
          {save === 'failed' ? (
            <p className="mt-2 text-[13px]" role="alert">
              {t('app.settings.dealPeriod.failed')}
            </p>
          ) : null}
        </>
      )}
    </Section>
  );
}
