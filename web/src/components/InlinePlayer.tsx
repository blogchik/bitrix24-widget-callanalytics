'use client';

/**
 * The recording player a call row expands into (§4.6, §5.7 rule 3, §9).
 *
 * Four facts decide everything about this component:
 *
 *  1. **An `<audio src>` cannot send an `Authorization` header.** So the source is not an
 *     API path plus a bearer token but `GET /calls/{id}/record?t=<grant>`, where the
 *     grant is the five-minute, one-call token `POST /calls/{id}/play-url` mints under
 *     the normal session (§4.6). It is minted when the row expands, not when the page
 *     loads: a grant per visible row would be fifty grants per page.
 *  2. **A non-admin viewer must be proven to Bitrix24 with their own token** (§9 step 3).
 *     "Call Recording: Listen" is a separate right that REST does not expose, so the only
 *     honest enforcement is to fetch the recording *as the viewer*. The endpoint asks for
 *     it with `viewer_token_required` and this component answers with `BX24.getAuth()`.
 *     The token goes in a POST body - never on the `<audio src>`, which is exactly the
 *     place browser history and proxies keep.
 *  3. **`RECORDING_MODE` is `off` until the §9 spike is answered**, and that is the
 *     *shipping* state, not a failure. The row still shows that a recording exists and
 *     this panel says, in words, that it is played in Bitrix24 - with a `BX24.openPath()`
 *     button that gets the listener there. It must read as a decision, because it is one.
 *  4. **A cached recording link goes stale.** The research doc is explicit that a
 *     recording can be deleted in Bitrix24 while our row still claims `has_record`. On a
 *     playback failure we call `POST /calls/{id}/refresh` **once** (§5.7 rule 3, which
 *     makes the next sync visit re-read the row) and name the likely cause instead of
 *     leaving a dead control on the page.
 *
 * The component never sees `call_record_url`: §9 keeps it server-side, so there is
 * nothing here that could leak a credential-bearing URL into a screenshot.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '@/lib/api';
import { openPath } from '@/lib/bx24';
import {
  RECORDING_MISSING_CODES,
  RECORDING_OFF_CODE,
  VIEWER_TOKEN_REQUIRED,
  mintPlaybackSource,
  requestCallRefresh,
  useCopy,
  viewerAccessToken,
} from '@/lib/calls';

const STYLE_ID = 'ca-inline-player';

/**
 * Codes that are about the *viewer or the portal*, never about this recording (§4.7).
 *
 * They must not spend §5.7's on-demand refresh: nothing is stale about the row.
 */
const PRINCIPAL_CODES: ReadonlySet<string> = new Set([
  'no_stats_permission',
  'portal_inactive',
  'invalid_session',
  'crm_no_access',
  'context_missing',
]);

const CSS = `
.ca-player {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--ca-surface-soft);
  border: 1px solid var(--ca-border-soft);
}
.ca-player-audio {
  width: 100%;
  max-width: 520px;
  height: 36px;
}
.ca-player-note {
  margin: 0;
  max-width: 62ch;
  font-size: 13px;
  line-height: 1.5;
  color: var(--ca-muted);
}
.ca-player-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--ca-text);
}
.ca-player-head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.ca-player-glyph {
  flex: 0 0 auto;
  margin-top: 1px;
  color: var(--ca-muted);
}
.ca-player-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
`;

type PlayerState =
  | { kind: 'loading' }
  | { kind: 'ready'; src: string }
  /** `RECORDING_MODE=off` (§9): deliberate, not broken. */
  | { kind: 'off' }
  /** The server says this call has no audio (any more). */
  | { kind: 'missing' }
  /** Bitrix24 would not confirm the viewer, so §9 step 3 cannot be satisfied. */
  | { kind: 'no_viewer_token' }
  /** Playback failed once; a refresh has been requested (§5.7 rule 3). */
  | { kind: 'stale' }
  /** It failed again after the refresh, or the API refused for another reason. */
  | { kind: 'failed' };

export interface InlinePlayerProps {
  /** `calls.id` - the surrogate key both call endpoints take. */
  callId: number;
  /**
   * Where "open in Bitrix24" goes: the call's CRM card when it has one, else the
   * Telephony section (`lib/calls.ts::recordingFallbackPath`).
   */
  fallbackPath: string;
  /**
   * §9's `RECORDING_MODE`, as `GET /calls` reports it.
   *
   * Knowing it up front saves a request that would only ever answer 409, and lets the
   * panel open straight into its "listen in Bitrix24" state.
   */
  recordingMode?: string | null;
}

/**
 * One expanded row's player.
 *
 * Rendered only for rows whose `has_record` is true, and only while the row is open -
 * unmounting is what stops the audio and drops the (already short-lived) grant.
 */
export function InlinePlayer({ callId, fallbackPath, recordingMode = null }: InlinePlayerProps) {
  const c = useCopy();
  const [state, setState] = useState<PlayerState>({ kind: 'loading' });
  const [openFailed, setOpenFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);

  /** §5.7 rule 3 says "once": a loop of refresh requests is a rate-limit incident. */
  const refreshed = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (recordingMode === 'off') {
      setState({ kind: 'off' });
      return;
    }

    const controller = new AbortController();
    let cancelled = false;

    /**
     * §5.7 rule 3: ask the worker to re-read this row, once, then say what happened.
     *
     * The user is told the recording *may have been deleted in Bitrix24* rather than
     * being shown a dead control: that is by far the likeliest cause, and it is the one
     * thing they can check for themselves.
     */
    const stale = async (): Promise<void> => {
      if (refreshed.current) {
        if (!cancelled && mounted.current) {
          setState({ kind: 'failed' });
        }
        return;
      }
      refreshed.current = true;
      await requestCallRefresh(callId);
      if (!cancelled && mounted.current) {
        setState({ kind: 'stale' });
      }
    };

    const classify = async (error: unknown): Promise<void> => {
      const code = error instanceof ApiError ? error.code : 'unknown';
      const status = error instanceof ApiError ? error.status : 0;
      if (code === RECORDING_OFF_CODE) {
        setState({ kind: 'off' });
        return;
      }
      if (RECORDING_MISSING_CODES.has(code)) {
        setState({ kind: 'missing' });
        return;
      }
      // A 403/404 about *the recording* is the same situation as a failed playback: the
      // row we cached is not what Bitrix24 has any more. A 403 about the *viewer* (§4.7's
      // `denied`, an inactive portal, a dead session) is a different problem and must not
      // spend a refresh request on a row that is perfectly fine.
      if (!PRINCIPAL_CODES.has(code) && (status === 403 || status === 404)) {
        await stale();
        return;
      }
      setState({ kind: 'failed' });
    };

    const run = async (): Promise<void> => {
      setState({ kind: 'loading' });
      try {
        const source = await mintPlaybackSource(callId, null, controller.signal);
        if (!cancelled && mounted.current) {
          setState({ kind: 'ready', src: source.src });
        }
        return;
      } catch (error: unknown) {
        if (cancelled || !mounted.current) {
          return;
        }
        const code = error instanceof ApiError ? error.code : '';
        if (code !== VIEWER_TOKEN_REQUIRED) {
          await classify(error);
          return;
        }
        // §9 step 3: prove the viewer to Bitrix24 with the viewer's own token.
        const token = await viewerAccessToken();
        if (cancelled || !mounted.current) {
          return;
        }
        if (!token) {
          setState({ kind: 'no_viewer_token' });
          return;
        }
        try {
          const source = await mintPlaybackSource(callId, token, controller.signal);
          if (!cancelled && mounted.current) {
            setState({ kind: 'ready', src: source.src });
          }
        } catch (second: unknown) {
          if (!cancelled && mounted.current) {
            await classify(second);
          }
        }
      }
    };

    void run();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [callId, attempt, recordingMode]);

  /**
   * The `<audio>` element failed.
   *
   * A media error carries no HTTP status - the browser will not say whether it was a 403,
   * a 404 or a truncated stream - so it is treated as §5.7's stale-link case. That is the
   * only failure mode we can actually cure, and the copy is worded to fit the others too.
   */
  const handleAudioError = useCallback(() => {
    void (async () => {
      if (refreshed.current) {
        if (mounted.current) {
          setState({ kind: 'failed' });
        }
        return;
      }
      refreshed.current = true;
      await requestCallRefresh(callId);
      if (mounted.current) {
        setState({ kind: 'stale' });
      }
    })();
  }, [callId]);

  const retry = useCallback(() => {
    setOpenFailed(false);
    setAttempt((value) => value + 1);
  }, []);

  const open = useCallback(() => {
    void openPath(fallbackPath).then((ok) => {
      if (mounted.current) {
        setOpenFailed(!ok);
      }
    });
  }, [fallbackPath]);

  const openButton = (
    <button type="button" className="ca-button ca-button-quiet" onClick={open}>
      {c('app.calls.player.open')}
    </button>
  );

  return (
    <div className="ca-player">
      <style href={STYLE_ID} precedence="default" dangerouslySetInnerHTML={{ __html: CSS }} />

      {state.kind === 'loading' ? (
        <p className="ca-player-note" role="status">
          {c('app.calls.player.loading')}
        </p>
      ) : null}

      {state.kind === 'ready' ? (
        <audio
          key={state.src}
          className="ca-player-audio"
          controls
          // `metadata` so a dead link surfaces immediately rather than on the first click,
          // and so a ten-minute recording is not streamed into a row nobody plays.
          preload="metadata"
          src={state.src}
          onError={handleAudioError}
        >
          {c('app.calls.player.unsupported')}
        </audio>
      ) : null}

      {state.kind === 'off' ? (
        <>
          <div className="ca-player-head">
            <SpeakerGlyph />
            <div>
              <p className="ca-player-title">{c('app.calls.player.offTitle')}</p>
              <p className="ca-player-note">{c('app.calls.player.offBody')}</p>
            </div>
          </div>
          <div className="ca-player-actions">{openButton}</div>
        </>
      ) : null}

      {state.kind === 'missing' ? (
        <p className="ca-player-note">{c('app.calls.player.missing')}</p>
      ) : null}

      {state.kind === 'no_viewer_token' ? (
        <>
          <p className="ca-player-note">{c('app.calls.player.noViewerToken')}</p>
          <div className="ca-player-actions">{openButton}</div>
        </>
      ) : null}

      {state.kind === 'stale' ? (
        <>
          <p className="ca-player-note">{c('app.calls.player.stale')}</p>
          <div className="ca-player-actions">
            <button type="button" className="ca-button ca-button-quiet" onClick={retry}>
              {c('app.calls.player.retry')}
            </button>
            {openButton}
          </div>
        </>
      ) : null}

      {state.kind === 'failed' ? (
        <>
          <p className="ca-player-note">{c('app.calls.player.failed')}</p>
          <div className="ca-player-actions">{openButton}</div>
        </>
      ) : null}

      {openFailed ? <p className="ca-player-note">{c('app.calls.player.openFailed')}</p> : null}
    </div>
  );
}

/** A speaker outline. Decorative: every state it appears in also says it in words. */
function SpeakerGlyph() {
  return (
    <svg
      className="ca-player-glyph"
      width="18"
      height="18"
      viewBox="0 0 18 18"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M3 7v4h2.5L9 14V4L5.5 7H3z" />
      <path d="M11.6 6.4a3.6 3.6 0 0 1 0 5.2" />
      <path d="M13.8 4.2a6.8 6.8 0 0 1 0 9.6" />
    </svg>
  );
}

export default InlinePlayer;
