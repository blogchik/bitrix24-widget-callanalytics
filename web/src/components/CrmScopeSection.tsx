/**
 * The settings section where an administrator says who may see more than Bitrix24 shows them.
 *
 * This is the one control in the product that can show a person MORE than Bitrix24 would,
 * and the section says so at the top rather than in a tooltip. Bitrix24 exposes no CRM
 * permission data over REST - `crm.role.list` and every sibling answer
 * ERROR_METHOD_NOT_FOUND - so an administrator who wants a supervisor to see the whole
 * funnel has no other place to say it, and the server audits every change to
 * `portal_events`.
 *
 * A grant names its AREAS. The CRM reports and the call pages are scoped by two different
 * systems - one by this app, one by Bitrix24's telephony verdict - and an administrator who
 * opens one has not opened the other. The calls area is the wider disclosure of the two: a
 * call row carries the customer's phone number, and the hint under the control says so.
 *
 * One editor at a time, on purpose: a page full of open forms invites an administrator to
 * change three people and remember one save.
 */
'use client';

import { useTranslations } from 'next-intl';
import { useCallback, useEffect, useState } from 'react';

import { Section } from '@/components/AppFrame';
import { Input, MultiSelect, SegmentedControl } from '@/components/ui';
import {
  clearCrmGrant,
  fetchCrmGrants,
  setCrmGrant,
  type CrmGrant,
  type CrmGrantKind,
  type CrmScopeDepartment,
  type CrmScopeEmployee,
  type CrmScopeSettings,
} from '@/lib/api';

/** The three things an administrator can say, including "say nothing". */
type EditorKind = 'default' | CrmGrantKind;

/** Which parts of the app a grant opens. `both` is the default, and the narrower two exist
 * because the owner asked to be able to give less than everything. */
type EditorAreas = 'crm' | 'calls' | 'both';

const AREAS: Record<EditorAreas, { crm: boolean; calls: boolean }> = {
  crm: { crm: true, calls: false },
  calls: { crm: false, calls: true },
  both: { crm: true, calls: true },
};

function areasOf(grant: CrmGrant | null): EditorAreas {
  if (grant === null) {
    return 'both';
  }
  if (grant.covers_crm && grant.covers_calls) {
    return 'both';
  }
  return grant.covers_calls ? 'calls' : 'crm';
}

interface EditorState {
  userId: number;
  kind: EditorKind;
  areas: EditorAreas;
  departmentIds: string[];
  note: string;
  busy: boolean;
  failed: boolean;
}

function departmentLabel(
  department: CrmScopeDepartment,
  t: ReturnType<typeof useTranslations>,
): string {
  // `department.get` degrades to bare ids on any failure, because a name is a convenience
  // for the person choosing and never a permission input.
  return department.name || t('app.crmScope.departmentFallback', { id: department.id });
}

export default function CrmScopeSection() {
  const t = useTranslations();
  const [data, setData] = useState<CrmScopeSettings | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [editor, setEditor] = useState<EditorState | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setFailed(false);
    fetchCrmGrants(controller.signal)
      .then((next) => setData(next))
      .catch(() => {
        if (!controller.signal.aborted) {
          setFailed(true);
        }
      });
    return () => controller.abort();
  }, [attempt]);

  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  const grantOf = useCallback(
    (userId: number): CrmGrant | null =>
      data?.grants.find((grant) => grant.user_id === userId) ?? null,
    [data],
  );

  const openEditor = useCallback(
    (employee: CrmScopeEmployee) => {
      const grant = grantOf(employee.user_id);
      setEditor({
        userId: employee.user_id,
        kind: grant?.kind ?? 'default',
        areas: areasOf(grant),
        departmentIds: (grant?.department_ids ?? []).map(String),
        note: grant?.note ?? '',
        busy: false,
        failed: false,
      });
    },
    [grantOf],
  );

  const save = useCallback(async () => {
    if (!editor) {
      return;
    }
    setEditor({ ...editor, busy: true, failed: false });
    try {
      if (editor.kind === 'default') {
        await clearCrmGrant(editor.userId);
      } else {
        await setCrmGrant({
          user_id: editor.userId,
          kind: editor.kind,
          department_ids: editor.departmentIds.map(Number),
          covers_crm: AREAS[editor.areas].crm,
          covers_calls: AREAS[editor.areas].calls,
          note: editor.note,
        });
      }
      setEditor(null);
      reload();
    } catch {
      setEditor((current) => (current ? { ...current, busy: false, failed: true } : null));
    }
  }, [editor, reload]);

  if (failed) {
    return (
      <Section title={t('app.crmScope.section')}>
        <p className="text-[14px]">{t('app.crmScope.failed')}</p>
        <div className="ca-actionrow">
          <button type="button" className="ca-button" onClick={reload}>
            {t('app.crmScope.retry')}
          </button>
        </div>
      </Section>
    );
  }

  if (!data) {
    return (
      <Section title={t('app.crmScope.section')}>
        <p className="ca-muted text-[13px]">{t('app.crmScope.loading')}</p>
      </Section>
    );
  }

  const departmentOptions = data.departments.map((department) => ({
    value: String(department.id),
    label: departmentLabel(department, t),
  }));

  return (
    <Section title={t('app.crmScope.section')}>
      {data.widens_beyond_bitrix24 ? (
        <p className="mt-1 text-[14px]">{t('app.crmScope.warning')}</p>
      ) : null}
      <p className="ca-muted mt-2 text-[13px]">{t('app.crmScope.hint')}</p>

      {data.employees.length === 0 ? (
        <p className="ca-muted mt-3 text-[13px]">{t('app.crmScope.empty')}</p>
      ) : (
        <ul className="mt-3 flex flex-col gap-2">
          {data.employees.map((employee) => {
            const grant = grantOf(employee.user_id);
            const editing = editor?.userId === employee.user_id;
            return (
              <li key={employee.user_id} className="border-t pt-2 first:border-t-0 first:pt-0">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <div className="min-w-0">
                    <span className="text-[14px]">{employee.name || String(employee.user_id)}</span>
                    {employee.active ? null : (
                      <span className="ca-muted ml-2 text-[13px]">
                        {t('app.crmScope.inactive')}
                      </span>
                    )}
                    <span className="ca-muted ml-2 text-[13px]">
                      {grant === null
                        ? t('app.crmScope.scopeDefault')
                        : grant.kind === 'portal'
                          ? `${t('app.crmScope.scopePortal')} · ${t(
                              `app.crmScope.area.${areasOf(grant)}`,
                            )}`
                          : t('app.crmScope.scopeDepartments', {
                              list: grant.department_ids
                                .map((id) => {
                                  const known = data.departments.find((d) => d.id === id);
                                  return known
                                    ? departmentLabel(known, t)
                                    : t('app.crmScope.departmentFallback', { id });
                                })
                                .join(', '),
                            })}
                    </span>
                  </div>
                  {editing ? null : (
                    <button
                      type="button"
                      className="ca-button ca-button-quiet"
                      onClick={() => openEditor(employee)}
                    >
                      {t('app.crmScope.change')}
                    </button>
                  )}
                </div>

                {grant !== null && !editing ? (
                  <p className="ca-muted mt-1 text-[13px]">
                    {t('app.crmScope.grantedBy', { user: grant.granted_by })}
                    {grant.note ? ` — ${grant.note}` : ''}
                  </p>
                ) : null}

                {editing && editor ? (
                  <div className="mt-2 flex flex-col gap-2">
                    <SegmentedControl<EditorKind>
                      label={t('app.crmScope.kindLabel')}
                      value={editor.kind}
                      onChange={(kind) => setEditor({ ...editor, kind })}
                      options={[
                        { value: 'default', label: t('app.crmScope.kindDefault') },
                        { value: 'portal', label: t('app.crmScope.kindPortal') },
                        {
                          value: 'departments',
                          label: t('app.crmScope.kindDepartments'),
                          // Nothing to choose from is not a choice: the server would refuse
                          // a departments grant with no departments anyway.
                          disabled: departmentOptions.length === 0,
                        },
                      ]}
                      disabled={editor.busy}
                    />
                    {editor.kind === 'default' ? null : (
                      <SegmentedControl<EditorAreas>
                        label={t('app.crmScope.areasLabel')}
                        value={editor.areas}
                        onChange={(areas) => setEditor({ ...editor, areas })}
                        options={[
                          { value: 'crm', label: t('app.crmScope.area.crm') },
                          { value: 'calls', label: t('app.crmScope.area.calls') },
                          { value: 'both', label: t('app.crmScope.area.both') },
                        ]}
                        hint={
                          AREAS[editor.areas].calls
                            ? t('app.crmScope.areasCallsHint')
                            : undefined
                        }
                        disabled={editor.busy}
                      />
                    )}
                    {editor.kind === 'departments' ? (
                      <MultiSelect
                        label={t('app.crmScope.departmentsLabel')}
                        values={editor.departmentIds}
                        onChange={(departmentIds) => setEditor({ ...editor, departmentIds })}
                        options={departmentOptions}
                        allLabel={t('app.crmScope.departmentsNone')}
                        summaryLabel={(count) =>
                          t('app.crmScope.departmentsSummary', { count })
                        }
                        emptyText={t('app.crmScope.departmentsEmpty')}
                        disabled={editor.busy}
                      />
                    ) : null}
                    {editor.kind === 'default' ? null : (
                      <Input
                        label={t('app.crmScope.noteLabel')}
                        hint={t('app.crmScope.noteHint')}
                        value={editor.note}
                        onValueChange={(note) => setEditor({ ...editor, note })}
                        maxLength={500}
                        disabled={editor.busy}
                      />
                    )}
                    <div className="ca-actionrow">
                      <button
                        type="button"
                        className="ca-button"
                        disabled={
                          editor.busy ||
                          (editor.kind === 'departments' && editor.departmentIds.length === 0)
                        }
                        onClick={() => void save()}
                      >
                        {editor.busy ? t('app.crmScope.saving') : t('app.crmScope.save')}
                      </button>
                      <button
                        type="button"
                        className="ca-button ca-button-quiet"
                        disabled={editor.busy}
                        onClick={() => setEditor(null)}
                      >
                        {t('app.crmScope.cancel')}
                      </button>
                    </div>
                    {editor.kind === 'default' ? (
                      <p className="ca-muted text-[13px]">{t('app.crmScope.defaultHint')}</p>
                    ) : null}
                    {editor.failed ? (
                      <p className="text-[13px]" role="alert">
                        {t('app.crmScope.saveFailed')}
                      </p>
                    ) : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Section>
  );
}
