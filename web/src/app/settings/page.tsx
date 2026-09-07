'use client';

/**
 * The administrator's status page (`POST /settings/`, §4.5).
 *
 * Every field on this page is a support question the operator would otherwise have to
 * ask us, and §4.5 names them: sync state and backfill progress; the token owner with
 * `token_status` and the age that decides whether the 180-day refresh chain is about to
 * die (§5.8); placement bind results with a re-bind; the quarantined-row count that
 * says "the sync is fine, some rows were not readable" (§3); `capabilities`; and the
 * last error code, which is a machine code shown verbatim because that is what gets
 * quoted to support (§8 - the codes are not sentences and are not translated).
 *
 * Two actions live here, and both are privileged:
 *
 *  * **Re-authorise** posts the pair from `BX24.getAuth()` to
 *    `POST /api/v1/portal/reauthorize`, where §4.5 validates it with a refresh whose
 *    `member_id` must match *and* a `user.admin` proof before anything is stored. The
 *    browser sends the token pair and nothing else: `member_id`, `portal_id` and
 *    `user_id` reach the database only from the verified JWT (§4.1).
 *  * **Re-bind** re-runs the `placement.bind` batch of §4.3 step 5, which is the cure
 *    for a portal whose CRM tab never appeared and for §8's "add a language, click
 *    re-bind".
 *
 * The admin check is repeated client-side even though `POST /settings/` already renders
 * "administrators only" for a non-admin: the JWT is the only authority on who the
 * viewer is (§4.7), and a page that prints a portal's token state should not depend on
 * an upstream redirect having happened.
 */
import { useLocale, useTranslations } from 'next-intl';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

import {
  ErrorState,
  Field,
  FieldList,
  LoadingBlock,
  PageShell,
  Section,
} from '@/components/AppFrame';
import StateCard from '@/components/StateCard';
import { apiFetch, useMe, type Me } from '@/lib/api';
import { getAuth, refreshAuth } from '@/lib/bx24';
import { useCopy, type Copy } from '@/lib/calls';
import { formatCount, formatDateTime } from '@/lib/format';

const STYLE_ID = 'ca-settings-page';

const CSS = `
.ca-progress {
  height: 6px;
  border-radius: 999px;
  background: var(--ca-border-soft);
  overflow: hidden;
}
.ca-progress-fill {
  height: 100%;
  border-radius: 999px;
  background: var(--ca-accent);
}
.ca-kv {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px 12px;
  padding: 4px 0;
  font-size: 13px;
}
.ca-kv-key {
  color: var(--ca-muted);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.ca-status-ok {
  color: #0ca30c;
}
.ca-status-bad {
  color: #d03b3b;
}
.ca-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
.ca-actionrow {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}
.ca-num {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
`;

/** §4.3 step 5 binds exactly these four; showing all of them makes a gap visible. */
const PLACEMENT_KEYS: readonly string[] = [
  'CRM_DEAL_DETAIL_TAB',
  'CRM_LEAD_DETAIL_TAB',
  'CRM_CONTACT_DETAIL_TAB',
  'CRM_COMPANY_DETAIL_TAB',
];

/** §5.8: the refresh chain dies at 180 days; warn from 150. */
const TOKEN_WARN_DAYS = 150;
const TOKEN_LIFETIME_DAYS = 180;

// ---------------------------------------------------------------------------------
// GET /api/v1/portal/sync-status, read defensively
// ---------------------------------------------------------------------------------

interface PlacementState {
  ok: boolean | null;
  at: string | null;
}

interface PortalStatus {
  domain: string | null;
  portal_status: string | null;
  app_version: number | null;
  installed_at: string | null;
  last_admin_opened_at: string | null;
  token_status: string | null;
  token_user_id: number | null;
  token_refreshed_at: string | null;
  token_admin_verified_at: string | null;
  token_age_days: number | null;
  /** §5.8's 150-day banner, decided server-side so both ends agree on the threshold. */
  reauthorize_soon: boolean | null;
  backfill_status: string | null;
  backfill_total: number | null;
  backfill_done: number | null;
  importing: boolean | null;
  last_incremental_at: string | null;
  next_run_at: string | null;
  consecutive_failures: number | null;
  throttle_hits: number | null;
  quarantined_rows: number | null;
  last_error_code: string | null;
  last_error_at: string | null;
  placements: Record<string, PlacementState>;
  capabilities: Record<string, unknown>;
}

function obj(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function str(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function bool(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function placementsOf(value: unknown): Record<string, PlacementState> {
  const source = obj(value) ?? {};
  const result: Record<string, PlacementState> = {};
  for (const [key, raw] of Object.entries(source)) {
    if (typeof raw === 'boolean') {
      result[key] = { ok: raw, at: null };
      continue;
    }
    const record = obj(raw);
    result[key] = {
      ok: record ? (bool(record.ok) ?? bool(record.bound)) : null,
      at: record ? str(record.at) : null,
    };
  }
  return result;
}

/**
 * `GET /portal/sync-status`'s answer -> the flat shape this page renders.
 *
 * Each field is read from the section `api/app/api/portal.py` puts it in, with the flat
 * spelling accepted as a fallback: a nesting disagreement between two agents should cost
 * a code review, not a page of em-dashes on a customer's portal.
 */
function normaliseStatus(raw: unknown): PortalStatus {
  const body = obj(raw) ?? {};
  const portal = obj(body.portal) ?? body;
  const token = obj(body.token) ?? body;
  const backfill = obj(body.backfill) ?? body;
  const sync = obj(body.sync) ?? body;
  const lastError = obj(body.last_error) ?? body;
  return {
    domain: str(portal.domain),
    portal_status: str(portal.status),
    app_version: num(portal.app_version),
    installed_at: str(portal.installed_at ?? portal.install_completed_at),
    last_admin_opened_at: str(portal.last_admin_opened_at),
    token_status: str(token.status ?? token.token_status),
    token_user_id: num(token.owner_user_id ?? token.token_user_id),
    token_refreshed_at: str(token.refreshed_at ?? token.token_refreshed_at),
    token_admin_verified_at: str(token.admin_verified_at ?? token.token_admin_verified_at),
    token_age_days: num(token.age_days),
    reauthorize_soon: bool(token.reauthorize_soon),
    backfill_status: str(backfill.status ?? backfill.backfill_status),
    backfill_total: num(backfill.total ?? backfill.backfill_total),
    backfill_done: num(backfill.done ?? backfill.backfill_done),
    importing: bool(backfill.importing),
    last_incremental_at: str(sync.last_incremental_at),
    next_run_at: str(sync.next_run_at),
    consecutive_failures: num(sync.consecutive_failures),
    throttle_hits: num(sync.throttle_hits),
    quarantined_rows: num(sync.quarantined_rows ?? sync.rejected_rows),
    last_error_code: str(lastError.code ?? lastError.last_error_code),
    last_error_at: str(lastError.at ?? lastError.last_error_at),
    placements: placementsOf(body.placements ?? portal.placements),
    capabilities: obj(body.capabilities ?? portal.capabilities) ?? {},
  };
}

/**
 * `GET /portal/sync-status`, with the summary from `GET /me` as the floor.
 *
 * The detailed endpoint failing is itself a support situation, so it degrades to a note
 * plus whatever the session already carries rather than to an error page: an admin who
 * came here to read `token_status` should still read it.
 */
function usePortalStatus(enabled: boolean) {
  const [data, setData] = useState<PortalStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(enabled);
  const [attempt, setAttempt] = useState(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setFailed(false);
    apiFetch<unknown>('/portal/sync-status', { signal: controller.signal })
      .then((raw) => {
        if (mounted.current && !controller.signal.aborted) {
          setData(normaliseStatus(raw));
        }
      })
      .catch(() => {
        if (mounted.current && !controller.signal.aborted) {
          setFailed(true);
        }
      })
      .finally(() => {
        if (mounted.current) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [enabled, attempt]);

  const reload = useCallback(() => setAttempt((value) => value + 1), []);
  return { data, failed, loading, reload };
}

// ---------------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------------

type ActionState = { kind: 'idle' } | { kind: 'busy' } | { kind: 'ok' } | { kind: 'failed' };

export default function SettingsPage() {
  const t = useTranslations();
  const c = useCopy();
  const locale = useLocale();
  const { data: me, error, loading, reload } = useMe();

  const isAdmin = Boolean(me?.is_admin);
  const status = usePortalStatus(isAdmin && !loading);

  const [reauth, setReauth] = useState<ActionState>({ kind: 'idle' });
  const [rebind, setRebind] = useState<ActionState>({ kind: 'idle' });
  const [reauthNoAuth, setReauthNoAuth] = useState(false);

  /**
   * §4.5: the body is the pair from `BX24.getAuth()` and nothing else.
   *
   * The backend proves it (a refresh whose `member_id` matches, then `user.admin`)
   * before a single credential column is written; nothing the browser says about which
   * portal this is would be believed anyway (§4.1).
   */
  const onReauthorize = useCallback(async () => {
    setReauth({ kind: 'busy' });
    setReauthNoAuth(false);
    const auth = (await getAuth()) ?? (await refreshAuth());
    if (!auth) {
      setReauthNoAuth(true);
      setReauth({ kind: 'idle' });
      return;
    }
    try {
      await apiFetch<unknown>('/portal/reauthorize', {
        method: 'POST',
        body: { access_token: auth.access_token, refresh_token: auth.refresh_token },
      });
      setReauth({ kind: 'ok' });
      status.reload();
    } catch {
      // §6 excludes this request's body from every log; its failure is a fixed sentence
      // here for the same reason - nothing from the exchange is echoed to the page.
      setReauth({ kind: 'failed' });
    }
  }, [status]);

  const onRebind = useCallback(async () => {
    setRebind({ kind: 'busy' });
    try {
      await apiFetch<unknown>('/portal/rebind-placements', { method: 'POST', body: {} });
      setRebind({ kind: 'ok' });
      status.reload();
    } catch {
      setRebind({ kind: 'failed' });
    }
  }, [status]);

  if (loading) {
    return <LoadingBlock label={t('app.loading')} />;
  }
  if (error || !me) {
    return <ErrorState error={error} onRetry={reload} />;
  }
  if (!me.is_admin) {
    return (
      <StateCard
        kind="admin_only"
        title={t('state.admin_only.title')}
        body={t('state.admin_only.body')}
      />
    );
  }

  const view = merged(me, status.data);
  // The server's own age when it answered (§5.8 owns the arithmetic), the local one when
  // only `GET /me` is available.
  const tokenAge = view.token_age_days ?? ageInDays(view.token_refreshed_at);
  const tokenExpiring =
    view.reauthorize_soon ?? (tokenAge !== null && tokenAge >= TOKEN_WARN_DAYS);
  const tokenOk = (view.token_status ?? 'ok') === 'ok';
  const zone = me.timezone;

  return (
    <PageShell
      title={t('app.settings.title')}
      subtitle={t('app.signedInAs', { id: String(me.user_id) })}
      chips={
        view.token_status ? (
          <span className="ca-chip">{view.token_status}</span>
        ) : undefined
      }
      banner={
        tokenExpiring ? (
          <span>
            {c('app.settings.tokenExpiring', {
              days: Math.max(TOKEN_LIFETIME_DAYS - (tokenAge ?? 0), 0),
            })}
          </span>
        ) : undefined
      }
    >
      <style href={STYLE_ID} precedence="default" dangerouslySetInnerHTML={{ __html: CSS }} />

      {status.failed ? (
        <p className="ca-muted text-[13px]">{c('app.settings.statusUnavailable')}</p>
      ) : null}

      {/* ---------------------------------------------------------------- sync ---- */}
      <Section title={t('app.sync.title')}>
        <FieldList>
          <Field label={t('app.sync.status')} value={view.backfill_status ?? '—'} />
          <Field
            label={t('app.sync.progress')}
            value={
              <BackfillProgress
                done={view.backfill_done}
                total={view.backfill_total}
                locale={locale}
              />
            }
          />
          <Field
            label={t('app.sync.lastRun')}
            value={formatDateTime(view.last_incremental_at, locale, zone)}
          />
          <Field
            label={c('app.settings.nextRun')}
            value={formatDateTime(view.next_run_at, locale, zone)}
          />
        </FieldList>
      </Section>

      {/* ----------------------------------------------------------- token ------- */}
      <Section title={t('app.settings.tokenSection')}>
        <FieldList>
          <Field
            label={t('app.settings.tokenStatus')}
            value={
              <StatusWord
                ok={view.token_status === null ? null : tokenOk}
                label={view.token_status ?? '—'}
              />
            }
          />
          <Field
            label={c('app.settings.tokenOwner')}
            value={view.token_user_id === null ? '—' : `#${view.token_user_id}`}
          />
          <Field
            label={c('app.settings.tokenRefreshed')}
            value={formatDateTime(view.token_refreshed_at, locale, zone)}
          />
          <Field
            label={c('app.settings.tokenAge')}
            value={tokenAge === null ? '—' : c('app.settings.tokenAgeDays', { days: tokenAge })}
          />
          <Field
            label={c('app.settings.tokenAdminVerified')}
            value={formatDateTime(view.token_admin_verified_at, locale, zone)}
          />
        </FieldList>
        <div className="ca-actionrow">
          <button
            type="button"
            className="ca-button"
            onClick={() => void onReauthorize()}
            disabled={reauth.kind === 'busy'}
          >
            {reauth.kind === 'busy'
              ? c('app.settings.working')
              : c('app.settings.reauthorize')}
          </button>
          <span className="ca-muted text-[13px]">{c('app.settings.reauthorizeHint')}</span>
        </div>
        <ActionNote
          state={reauth}
          okKey="app.settings.reauthorizeOk"
          failedKey="app.settings.reauthorizeFailed"
          copy={c}
        />
        {reauthNoAuth ? (
          <p className="ca-muted mt-2 text-[13px]">{c('app.settings.reauthorizeNoAuth')}</p>
        ) : null}
      </Section>

      {/* ------------------------------------------------------- placements ------ */}
      <Section title={c('app.settings.placementsSection')}>
        <FieldList>
          {placementRows(view.placements).map(([key, state]) => (
            <Field
              key={key}
              label={key}
              value={
                <span className="ca-status">
                  <StatusWord
                    ok={state.ok}
                    label={
                      state.ok === null
                        ? c('app.settings.placementUnknown')
                        : state.ok
                          ? c('app.settings.placementBound')
                          : c('app.settings.placementFailed')
                    }
                  />
                  {state.at ? (
                    <span className="ca-muted">{formatDateTime(state.at, locale, zone)}</span>
                  ) : null}
                </span>
              }
            />
          ))}
        </FieldList>
        <div className="ca-actionrow">
          <button
            type="button"
            className="ca-button ca-button-quiet"
            onClick={() => void onRebind()}
            disabled={rebind.kind === 'busy'}
          >
            {rebind.kind === 'busy' ? c('app.settings.working') : c('app.settings.rebind')}
          </button>
          <span className="ca-muted text-[13px]">{c('app.settings.rebindHint')}</span>
        </div>
        <ActionNote
          state={rebind}
          okKey="app.settings.rebindOk"
          failedKey="app.settings.rebindFailed"
          copy={c}
        />
      </Section>

      {/* ---------------------------------------------------------- health ------- */}
      <Section title={c('app.settings.qualitySection')}>
        <FieldList>
          <Field
            label={c('app.settings.quarantined')}
            value={
              view.quarantined_rows === null
                ? '—'
                : formatCount(view.quarantined_rows, locale)
            }
          />
          <Field
            label={c('app.settings.failures')}
            value={
              view.consecutive_failures === null
                ? '—'
                : formatCount(view.consecutive_failures, locale)
            }
          />
          <Field
            label={c('app.settings.throttle')}
            value={view.throttle_hits === null ? '—' : formatCount(view.throttle_hits, locale)}
          />
          {/* A machine code, shown verbatim: §8 keeps API error codes untranslated
              precisely so an admin can quote one to support. */}
          <Field
            label={t('app.sync.lastErrorCode')}
            value={view.last_error_code ?? c('app.settings.lastErrorNone')}
          />
          <Field
            label={c('app.settings.lastError')}
            value={formatDateTime(view.last_error_at, locale, zone)}
          />
        </FieldList>
        <p className="ca-muted mt-3 text-[13px]">{c('app.settings.quarantinedHint')}</p>
      </Section>

      {/* --------------------------------------------------- capabilities -------- */}
      <Section title={c('app.settings.capabilitiesSection')}>
        {Object.keys(view.capabilities).length === 0 ? (
          <p className="ca-muted text-[13px]">—</p>
        ) : (
          <div>
            {Object.entries(view.capabilities).map(([key, value]) => (
              <div className="ca-kv" key={key}>
                <span className="ca-kv-key">{key}</span>
                <span>{capabilityText(value, c)}</span>
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* --------------------------------------------------------- portal -------- */}
      <Section title={c('app.settings.portalSection')}>
        <FieldList>
          {/* Display data only - §4.1 never uses DOMAIN as a lookup key or a REST base,
              and `sync-status` returns it for exactly this line. */}
          <Field label={c('app.settings.domain')} value={view.domain ?? '—'} />
          <Field
            label={c('app.settings.appVersion')}
            value={view.app_version === null ? '—' : String(view.app_version)}
          />
          <Field
            label={c('app.settings.installedAt')}
            value={formatDateTime(view.installed_at, locale, zone)}
          />
          <Field
            label={c('app.settings.lastAdminOpen')}
            value={formatDateTime(view.last_admin_opened_at, locale, zone)}
          />
          <Field label={t('app.session.timezone')} value={me.timezone ?? '—'} />
          <Field label={t('app.session.language')} value={me.locale ?? locale} />
        </FieldList>
      </Section>
    </PageShell>
  );
}

// ---------------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------------

/** The detailed status when it answered, `GET /me`'s summary when it did not. */
function merged(me: Me, status: PortalStatus | null): PortalStatus {
  const sync = me.sync ?? null;
  return {
    domain: status?.domain ?? null,
    portal_status: status?.portal_status ?? null,
    app_version: status?.app_version ?? null,
    installed_at: status?.installed_at ?? null,
    last_admin_opened_at: status?.last_admin_opened_at ?? null,
    token_status: status?.token_status ?? sync?.token_status ?? null,
    token_user_id: status?.token_user_id ?? null,
    token_refreshed_at: status?.token_refreshed_at ?? null,
    token_admin_verified_at: status?.token_admin_verified_at ?? null,
    token_age_days: status?.token_age_days ?? null,
    reauthorize_soon: status?.reauthorize_soon ?? null,
    backfill_status: status?.backfill_status ?? sync?.backfill_status ?? null,
    backfill_total: status?.backfill_total ?? sync?.backfill_total ?? null,
    backfill_done: status?.backfill_done ?? sync?.backfill_done ?? null,
    importing: status?.importing ?? sync?.importing ?? null,
    last_incremental_at: status?.last_incremental_at ?? sync?.last_incremental_at ?? null,
    next_run_at: status?.next_run_at ?? null,
    consecutive_failures: status?.consecutive_failures ?? null,
    throttle_hits: status?.throttle_hits ?? null,
    quarantined_rows: status?.quarantined_rows ?? null,
    last_error_code: status?.last_error_code ?? sync?.last_error_code ?? null,
    last_error_at: status?.last_error_at ?? null,
    placements: status?.placements ?? {},
    capabilities: status?.capabilities ?? {},
  };
}

/** The four §4.3 placements first, in a fixed order, then anything else the portal has. */
function placementRows(
  placements: Record<string, PlacementState>,
): [string, PlacementState][] {
  const rows: [string, PlacementState][] = PLACEMENT_KEYS.map((key) => [
    key,
    placements[key] ?? { ok: null, at: null },
  ]);
  for (const [key, state] of Object.entries(placements)) {
    if (!PLACEMENT_KEYS.includes(key)) {
      rows.push([key, state]);
    }
  }
  return rows;
}

/** Whole days since a timestamp, or `null` when there is none. */
function ageInDays(value: string | null): number | null {
  if (!value) {
    return null;
  }
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) {
    return null;
  }
  return Math.max(Math.floor((Date.now() - parsed) / 86_400_000), 0);
}

/**
 * A status word in green or red.
 *
 * The chart palette forbids green/red as the *only* cue, and it is not one here: the
 * glyph and the word carry the meaning, the colour only repeats it. Summary status is
 * the one surface where those two hues are allowed at all.
 */
function StatusWord({ ok, label }: { ok: boolean | null; label: string }) {
  if (ok === null) {
    return <span className="ca-muted">{label}</span>;
  }
  return (
    <span className={ok ? 'ca-status ca-status-ok' : 'ca-status ca-status-bad'}>
      <svg
        width="12"
        height="12"
        viewBox="0 0 12 12"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
      >
        {ok ? <path d="M2.5 6.5l2.5 2.5 4.5-5.5" /> : <path d="M3 3l6 6M9 3l-6 6" />}
      </svg>
      {label}
    </span>
  );
}

/** §4.11's "importing history 12 300 / 250 000", as a bar plus the two numbers. */
function BackfillProgress({
  done,
  total,
  locale,
}: {
  done: number | null;
  total: number | null;
  locale: string;
}) {
  if (total === null || total <= 0) {
    return <span>{done === null ? '—' : formatCount(done, locale)}</span>;
  }
  const ratio = Math.min(Math.max((done ?? 0) / total, 0), 1);
  return (
    <span className="block w-full max-w-xs">
      <span className="ca-num block">
        {formatCount(done ?? 0, locale)} / {formatCount(total, locale)}
      </span>
      <span
        className="ca-progress mt-2 block"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done ?? 0}
      >
        <span className="ca-progress-fill block" style={{ width: `${ratio * 100}%` }} />
      </span>
    </span>
  );
}

/** The one-line outcome of an action, in catalogue words - never the server's text. */
function ActionNote({
  state,
  okKey,
  failedKey,
  copy,
}: {
  state: ActionState;
  okKey: string;
  failedKey: string;
  copy: Copy;
}): ReactNode {
  if (state.kind === 'ok') {
    return <p className="mt-2 text-[13px]">{copy(okKey)}</p>;
  }
  if (state.kind === 'failed') {
    return <p className="mt-2 text-[13px]">{copy(failedKey)}</p>;
  }
  return null;
}

/** A `capabilities` value as one line: `true`/`false` translated, the rest verbatim. */
function capabilityText(value: unknown, copy: Copy): string {
  if (typeof value === 'boolean') {
    return copy(value ? 'app.settings.capabilityOn' : 'app.settings.capabilityOff');
  }
  if (value === null || value === undefined) {
    return '—';
  }
  if (typeof value === 'object') {
    return JSON.stringify(value);
  }
  return String(value);
}
