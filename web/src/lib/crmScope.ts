/**
 * Spending one Bitrix24 token on a census, once, so the CRM reports can come from Postgres.
 *
 * `/me` says whether this viewer would be answered differently with a census and has none
 * (`crm.scope_needed`). The census itself costs seconds - measured at 8.8 s on a real portal
 * - which is why it is not part of `/me`, and why the page holds its report back while one
 * runs instead of firing a live read that would be thrown away moments later.
 *
 * A failure is deliberately silent. The viewer keeps the live read, which is what they had
 * before this existed; an error banner would name a mechanism they did not ask about.
 */
'use client';

import { useEffect, useRef, useState } from 'react';

import { requestCrmScope, type Me, type Resource } from '@/lib/api';
import { viewerAccessToken } from '@/lib/calls';

export interface CensusState {
  /** True while a census is in flight: the page should not start a report yet. */
  running: boolean;
}

export function useCrmCensus(me: Resource<Me>): CensusState {
  const [running, setRunning] = useState(false);
  // Once per mount, whatever `/me` says afterwards. A census that came back `unprovable`
  // would otherwise be re-asked on every reload it triggers, in a loop.
  const asked = useRef(false);
  const needed = me.data?.crm?.scope_needed === true;
  const { reload } = me;

  useEffect(() => {
    if (!needed || asked.current) {
      return;
    }
    asked.current = true;
    let live = true;
    setRunning(true);
    void (async () => {
      try {
        const token = await viewerAccessToken();
        if (token) {
          await requestCrmScope(token);
        }
      } catch {
        // Stay on the live read. `/me` is re-read either way, so the page settles on
        // whatever the server now says rather than on what this call hoped for.
      }
      if (live) {
        setRunning(false);
        reload();
      }
    })();
    return () => {
      live = false;
    };
  }, [needed, reload]);

  return { running };
}
