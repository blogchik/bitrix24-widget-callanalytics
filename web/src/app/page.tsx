import { getTranslations } from 'next-intl/server';

import StateCard from '@/components/StateCard';

/**
 * The route reached with no Bitrix24 context at all (§4.10).
 *
 * `middleware.ts` has already set `frame-ancestors 'none'` here, because without a
 * validated `DOMAIN` there is no portal we would allow to frame us. There is no session
 * token either - `handoff.html` never targets `/` - so the page's whole job is the one
 * sentence §4.10 mandates, in the catalogue's own words.
 */
export default async function HomePage() {
  const t = await getTranslations('common');
  return <StateCard title={t('appName')} body={t('openFromBitrix24')} />;
}
