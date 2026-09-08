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
 *
 * The transport itself - the button, the seek bar, the times - lives in
 * `components/ui/AudioPlayer.tsx` and knows none of the above. It is handed a finished
 * `src` and finished words, which is what keeps the credential handling in one file and
 * makes the player something a test can mount with a `blob:` URL.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import AudioPlayer from '@/components/ui/AudioPlayer';
import type { AudioPlayerLabels } from '@/components/ui/AudioPlayer';
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
/*
 * The row does not snap open, it grows: a 0fr -> 1fr grid row is a real height
 * animation without anybody having to measure the panel first. A browser that cannot
 * interpolate the track still gets the fade, which is a shorter version of the same
 * gesture rather than a different one - and under prefers-reduced-motion the token is
 * 1ms, so this whole rule costs nothing.
 */
.ca-player-reveal {
  display: grid;
  grid-template-rows: 0fr;
  animation: ca-player-open var(--ca-dur-panel) var(--ca-ease-out) forwards;
}
@keyframes ca-player-open {
  from {
    grid-template-rows: 0fr;
    opacity: 0;
  }
  to {
    grid-template-rows: 1fr;
    opacity: 1;
  }
}
.ca-player {
  /* Both needed for the grid row above to be able to squeeze this to nothing. */
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--ca-surface-soft);
  border: 1px solid var(--ca-border-soft);
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
/* The disabled-playback state is a decision, not a failure, so it gets a real
 * icon plate rather than a lonely line of grey text. */
.ca-player-glyph {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 999px;
  background: var(--ca-accent-soft);
  color: var(--ca-accent);
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
  /**
   * `duration` is what `play-url` reported, not what the file says.
   *
   * It is carried so the transport can print a total length before the element has
   * fetched a byte of metadata - on a proxied source that is a visible moment, and a
   * dash that turns into a number reads like a glitch.
   */
  | { kind: 'ready'; src: string; duration: number | null }
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
          setState({ kind: 'ready', src: source.src, duration: source.duration });
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
            setState({ kind: 'ready', src: source.src, duration: source.duration });
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

  /**
   * Every word the transport renders, translated here.
   *
   * `ui/AudioPlayer` owns no copy at all, so this object is the whole of its vocabulary.
   * Memoised because a fresh identity on every keystroke of the parent would re-render a
   * component that is, four times a second, redrawing a progress bar.
   */
  const labels = useMemo<AudioPlayerLabels>(
    () => ({
      player: c('app.calls.player.transport'),
      play: c('app.calls.player.play'),
      pause: c('app.calls.player.pause'),
      seek: c('app.calls.player.seek'),
      position: (current: string, total: string) =>
        c('app.calls.player.position', { current, total }),
      elapsed: c('app.calls.player.elapsed'),
      total: c('app.calls.player.total'),
      loading: c('app.calls.player.loading'),
      mute: c('app.calls.player.mute'),
      unmute: c('app.calls.player.unmute'),
      rate: c('app.calls.player.rate'),
      // The transport's own last-resort error is the same sentence the `failed` state
      // below uses: from a listener's seat it is the same situation.
      error: c('app.calls.player.failed'),
      unsupported: c('app.calls.player.unsupported'),
    }),
    [c],
  );

  return (
    <div className="ca-player-reveal">
      <div className="ca-player">
        <style href={STYLE_ID} precedence="default" dangerouslySetInnerHTML={{ __html: CSS }} />

        {state.kind === 'loading' ? (
          <p className="ca-player-note" role="status">
            {c('app.calls.player.loading')}
          </p>
        ) : null}

        {state.kind === 'ready' ? (
          <AudioPlayer
            src={state.src}
            duration={state.duration}
            labels={labels}
            // A stalled first byte is the same cure as a media error: §5.7's one refresh,
            // then the sentence that names the likely cause. Either way the panel below
            // replaces the transport, so nothing is left spinning.
            onError={handleAudioError}
            fallback={openButton}
          />
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
    </div>
  );
}

/** A speaker outline on a plate. Decorative: every state it appears in says it in words. */
function SpeakerGlyph() {
  return (
    <span className="ca-player-glyph" aria-hidden="true">
      <svg
        width="18"
        height="18"
        viewBox="0 0 18 18"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        focusable="false"
      >
        <path d="M3 7v4h2.5L9 14V4L5.5 7H3z" />
        <path d="M11.6 6.4a3.6 3.6 0 0 1 0 5.2" />
        <path d="M13.8 4.2a6.8 6.8 0 0 1 0 9.6" />
      </svg>
    </span>
  );
}

export default InlinePlayer;
