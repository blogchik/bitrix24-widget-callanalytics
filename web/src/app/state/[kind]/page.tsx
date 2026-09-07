import type { Metadata } from 'next';
import { getTranslations } from 'next-intl/server';

import StateCard, { FALLBACK_STATE_KIND, isStateKind } from '@/components/StateCard';

/**
 * The §4.11 states as SPA routes.
 *
 * `POST /app/` redirects here instead of minting a JWT whenever the answer is "not
 * this user, not this portal, not right now" - `/state/denied` for a user without the
 * "Call statistics - view" permission (§4.4 step 5), `/state/crm_no_access` when a CRM
 * command failed, `/state/retry`, `/state/scope`, `/state/method_missing`,
 * `/state/not_installed`, `/state/unsupported_portal`, `/state/reauth`, `/state/error`.
 * They carry Bitrix24's original query string, so the CSP `middleware.ts` computes and
 * the language are the same as on any other route, and they carry **no token**.
 *
 * The copy is read from the shared catalogue (§8) under `state.<kind>.*` - the exact
 * strings the server-rendered `state.html` uses, including the mandated `denied` text.
 * An unknown kind renders the generic `error` copy rather than a dotted key or a 404,
 * because §4.11 makes a blank frame a rejection.
 */

type StatePageProps = {
  params: Promise<{ kind: string }>;
};

function resolveKind(kind: string): string {
  return isStateKind(kind) ? kind : FALLBACK_STATE_KIND;
}

export async function generateMetadata({ params }: StatePageProps): Promise<Metadata> {
  const { kind } = await params;
  const resolved = resolveKind(decodeURIComponent(kind));
  const [state, common] = await Promise.all([
    getTranslations('state'),
    getTranslations('common'),
  ]);
  return { title: `${state(`${resolved}.title`)} — ${common('appName')}` };
}

export default async function StatePage({ params }: StatePageProps) {
  const { kind } = await params;
  const resolved = resolveKind(decodeURIComponent(kind));
  const t = await getTranslations('state');

  return (
    <StateCard kind={resolved} title={t(`${resolved}.title`)} body={t(`${resolved}.body`)} />
  );
}
