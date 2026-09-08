'use client';

/**
 * The transport a recording is played with. Presentation only.
 *
 * This component knows about **one audio file and a set of words**. It has never heard of
 * `/api/v1`, of the five-minute playback grant, of `viewer_token_required` or of any other
 * machine code: `InlinePlayer` owns all of that and hands this component a finished `src`
 * and finished copy. The separation is not tidiness for its own sake - it is what keeps a
 * credential-bearing URL out of a component that renders attributes, and what makes the
 * transport testable with a plain `blob:` URL and reusable by anything that grows a player
 * later.
 *
 * Three rules follow from where it runs (a Bitrix24 slider, often on a phone):
 *
 *  1. **The `src` is written to exactly one place: `<audio src>`.** Never a `title`, never
 *     a `data-` attribute, never a link, never a log line. The grant on it is live.
 *  2. **Nothing spins forever.** The audio is proxied, so the first byte can take a moment
 *     and a stall is a real outcome rather than a hypothetical. Pressing play arms a timer;
 *     if no byte arrives before it fires, the owner is told and an error with a way out is
 *     shown in place of the transport.
 *  3. **Every duration comes from the `--ca-dur-*` tokens**, which `globals.css` collapses
 *     to 1ms under `prefers-reduced-motion`. The only hardcoded millisecond here is the
 *     stall deadline, which is a network timeout and not motion.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent, PointerEvent, ReactNode } from 'react';

import { formatDuration } from '@/lib/format';

const STYLE_ID = 'ca-audio-player';

/** The speeds a call recording is actually listened to at. One control cycles them. */
export const PLAYBACK_RATES: readonly number[] = [1, 1.25, 1.5, 2];

/** Arrow-key and root-shortcut seek step, in seconds. */
const STEP = 5;

/** Page Up / Page Down seek step, in seconds. */
const BIG_STEP = 30;

/**
 * How long to wait for the first byte after pressing play.
 *
 * Not motion: this is the deadline that turns "loading" into an error with a fallback
 * action, so it is a plain number and deliberately not a `--ca-dur-*` token.
 */
const DEFAULT_STALL_MS = 12_000;

/** Why playback stopped being possible. Both are the same story to a listener. */
export type AudioPlayerFailure = 'media' | 'timeout';

/**
 * Every word this component renders, supplied by the owner.
 *
 * There is no fallback English in here on purpose: §8 keeps one message source, and a
 * component that invented its own copy would quietly become a second one.
 */
export interface AudioPlayerLabels {
  /** Accessible name of the transport as a whole. */
  player: string;
  play: string;
  pause: string;
  /** Accessible name of the seek slider. */
  seek: string;
  /**
   * `aria-valuetext`: the two formatted times, in one sentence.
   *
   * A formatter rather than a template because the owner's message catalogue is ICU and
   * knows how to place the values; re-implementing interpolation here would be a second
   * message system in a component that is supposed to own no copy at all.
   */
  position: (current: string, total: string) => string;
  /** Accessible name of the elapsed-time readout. */
  elapsed: string;
  /** Accessible name of the total-time readout. */
  total: string;
  /** Announced while waiting for the first byte. */
  loading: string;
  mute: string;
  unmute: string;
  /** Accessible name of the speed control; the rate itself is its visible label. */
  rate: string;
  /** Shown when playback fails and the owner passed no message of its own. */
  error: string;
  /** `<audio>` fallback text for a browser with no media support. */
  unsupported: string;
}

export interface AudioPlayerProps {
  /**
   * The audio to play.
   *
   * Rendered to `<audio src>` and nowhere else. Treat it as a credential.
   */
  src: string;
  labels: AudioPlayerLabels;
  /**
   * Total length in seconds, when the owner already knows it.
   *
   * Used until the element reports its own, so the total time is a number rather than a
   * dash for the first moment of a proxied stream.
   */
  duration?: number | null;
  /**
   * An error the owner has already resolved to copy, shown in place of the transport.
   *
   * The owner maps its machine codes; this component only renders the sentence.
   */
  errorMessage?: string | null;
  /** The owner's way out, rendered beside the error - typically an "open there" button. */
  fallback?: ReactNode;
  /** Deadline for the first byte after pressing play. */
  stallTimeoutMs?: number;
  /** Playback became impossible. The owner decides what that means and what to show. */
  onError?: (reason: AudioPlayerFailure) => void;
  onEnded?: () => void;
}

/** Custom properties are how the fill and the knob learn the progress ratio. */
type CssVars = CSSProperties & Record<`--${string}`, string>;

const CSS = `
.ca-ap {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  border-radius: var(--ca-radius);
}
/* The panel this sits in clips itself while it opens, so the ring goes inside. */
.ca-ap:focus-visible {
  outline: 2px solid var(--ca-accent);
  outline-offset: -2px;
}
.ca-ap-media {
  display: none;
}
/* Every glyph and every stripe of the bar is decoration: the button and the slider
 * are the hit targets. Saying so keeps a stray SVG path from behaving - and from
 * being measured - like a control of its own. */
.ca-ap svg,
.ca-ap svg * {
  pointer-events: none;
  cursor: auto;
}
/*
 * The live region: read aloud, never drawn.
 *
 * clip-path does the hiding rather than overflow:hidden on purpose. Both hide the
 * text, but the overflow version reports itself to any layout check as a 1px box with
 * 200px of clipped content in it - a defect everywhere else in the app, and noise here.
 */
.ca-ap-sr {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: visible;
  clip-path: inset(50%);
  white-space: nowrap;
  pointer-events: none;
  border: 0;
}

/*
 * Capped, because a seek bar drawn across 1400px of a wide slider stops reading as a
 * control and starts reading as a rule across the page. 640px is wide enough to scrub a
 * long call to the second and still looks like something you operate.
 */
.ca-ap-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  min-width: 0;
  max-width: 640px;
}

/* ---- play / pause ------------------------------------------------------ */
/*
 * Sized from the control token, not from a constant: 40px under a cursor, 44px under a
 * finger, plus the 4px that makes the primary control of the row read as the primary
 * control of the row. The drawn box is the whole hit area - no invisible pseudo-element
 * pretending to be bigger than what the eye can see and the thumb can aim at.
 */
.ca-ap-play {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: calc(var(--ca-control-h) + 4px);
  height: calc(var(--ca-control-h) + 4px);
  padding: 0;
  border-radius: 999px;
  border: 1px solid var(--ca-accent);
  background: var(--ca-accent);
  color: var(--ca-accent-contrast);
  cursor: pointer;
  transition:
    transform var(--ca-dur-fast) var(--ca-ease),
    filter var(--ca-dur-fast) var(--ca-ease);
}
.ca-ap-play:hover {
  filter: brightness(1.06);
}
/* Scale, never size or margin: the seek bar beside it must not twitch. */
.ca-ap-play:active {
  transform: scale(0.93);
}

/* The two glyphs share one canvas and trade places. */
.ca-ap-icon {
  display: block;
}
.ca-ap-glyph {
  transform-box: fill-box;
  transform-origin: center;
  transition:
    opacity var(--ca-dur-fast) var(--ca-ease),
    transform var(--ca-dur-fast) var(--ca-ease);
}
.ca-ap-glyph-play {
  opacity: 1;
  transform: none;
}
.ca-ap-glyph-pause {
  opacity: 0;
  transform: scale(0.55) rotate(20deg);
}
.ca-ap[data-playing='true'] .ca-ap-glyph-play {
  opacity: 0;
  transform: scale(0.55) rotate(-20deg);
}
.ca-ap[data-playing='true'] .ca-ap-glyph-pause {
  opacity: 1;
  transform: none;
}

.ca-ap-spin {
  transform-box: fill-box;
  transform-origin: center;
  animation: ca-ap-spin calc(var(--ca-dur-panel) * 4) linear infinite;
}
@keyframes ca-ap-spin {
  to {
    transform: rotate(360deg);
  }
}
/* An indeterminate loop cannot be expressed as a token, so it is switched off the same
 * way globals.css switches off .ca-skeleton. */
@media (prefers-reduced-motion: reduce) {
  .ca-ap-spin {
    animation: none;
  }
}

/* ---- seek -------------------------------------------------------------- */
/*
 * The basis, not the min-width, is what decides whether the tools stay on this line:
 * a wrapping flex container breaks lines on hypothetical sizes and only shrinks
 * afterwards. 130px keeps play + bar + speed + mute on one row inside the 309px an
 * expanded call row actually offers at 375px, and anything narrower than that drops
 * the two pills to a second, right-aligned line rather than pushing the bar out.
 */
.ca-ap-lane {
  flex: 1 1 130px;
  min-width: 110px;
  display: flex;
  flex-direction: column;
}

/* Padding, not height: a 6px track inside a strip as tall as the play button beside
 * it. The strip is the touch target, and it spans the whole lane, so a thumb finds it
 * without aiming. */
.ca-ap-seek {
  position: relative;
  padding: calc((var(--ca-control-h) + 4px - 6px) / 2) 0;
  cursor: pointer;
  touch-action: pan-y;
  border-radius: var(--ca-radius);
}
.ca-ap-seek[aria-disabled='true'] {
  cursor: default;
}
.ca-ap-seek:focus-visible {
  outline: 2px solid var(--ca-accent);
  outline-offset: -2px;
}
.ca-ap-track {
  position: relative;
  height: 6px;
  border-radius: 999px;
  background: var(--ca-border-soft);
  overflow: hidden;
  pointer-events: none;
  cursor: auto;
  transition: transform var(--ca-dur-fast) var(--ca-ease);
}
/* Thicken by scaling, so hovering the bar cannot reflow the row. */
.ca-ap-seek:hover .ca-ap-track,
.ca-ap-seek:focus-visible .ca-ap-track {
  transform: scaleY(1.4);
}
.ca-ap-buffered,
.ca-ap-played {
  position: absolute;
  inset: 0;
  transform-origin: left center;
  will-change: transform;
  pointer-events: none;
  cursor: auto;
}
.ca-ap-buffered {
  background: var(--ca-muted);
  opacity: 0.32;
  transform: scaleX(var(--ca-ap-buffered, 0));
  transition: transform var(--ca-dur) var(--ca-ease-out);
}
.ca-ap-played {
  background: var(--ca-accent);
  transform: scaleX(var(--ca-ap-progress, 0));
  transition: transform var(--ca-dur-fast) linear;
}
.ca-ap-knob {
  position: absolute;
  top: 50%;
  left: calc(6px + var(--ca-ap-progress, 0) * (100% - 12px));
  width: 12px;
  height: 12px;
  border-radius: 999px;
  background: var(--ca-accent);
  border: 2px solid var(--ca-surface);
  transform: translate(-50%, -50%);
  pointer-events: none;
  cursor: auto;
  transition:
    left var(--ca-dur-fast) linear,
    transform var(--ca-dur-fast) var(--ca-ease);
}
.ca-ap-seek:hover .ca-ap-knob {
  transform: translate(-50%, -50%) scale(1.12);
}
/* While a finger is on it the bar tracks the finger, not an easing curve. */
.ca-ap-seek[data-dragging='true'] .ca-ap-played,
.ca-ap-seek[data-dragging='true'] .ca-ap-knob {
  transition: none;
}
.ca-ap-seek[data-dragging='true'] .ca-ap-knob {
  transform: translate(-50%, -50%) scale(1.2);
}

.ca-ap-times {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  line-height: 1.2;
  color: var(--ca-muted);
  /* The 44px strip above already carries the breathing room. */
  margin-top: -4px;
  /* Tabular figures: 0:09 -> 0:10 must not shove the layout sideways. */
  font-variant-numeric: tabular-nums;
  font-feature-settings: 'tnum' 1;
}
.ca-ap-time {
  white-space: nowrap;
}

/* ---- speed and mute ---------------------------------------------------- */
.ca-ap-tools {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}
.ca-ap-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: var(--ca-control-h);
  min-width: var(--ca-control-h);
  padding: 0 10px;
  border-radius: 999px;
  border: 1px solid var(--ca-border);
  background: var(--ca-surface);
  color: var(--ca-text);
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  cursor: pointer;
  transition:
    background var(--ca-dur-fast) var(--ca-ease),
    color var(--ca-dur-fast) var(--ca-ease),
    transform var(--ca-dur-fast) var(--ca-ease);
}
.ca-ap-chip:hover {
  background: var(--ca-accent-soft);
  color: var(--ca-accent);
}
.ca-ap-chip:active {
  transform: scale(0.94);
}
.ca-ap-chip[aria-pressed='true'] {
  background: var(--ca-accent-soft);
  color: var(--ca-accent);
  border-color: var(--ca-accent);
}
.ca-ap-chip-icon {
  padding: 0;
}

/* ---- error ------------------------------------------------------------- */
.ca-ap-error {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.ca-ap-error-text {
  margin: 0;
  max-width: 62ch;
  font-size: 13px;
  line-height: 1.5;
  color: var(--ca-muted);
}
`;

function clamp(value: number, low: number, high: number): number {
  return value < low ? low : value > high ? high : value;
}

/**
 * How far the element has buffered ahead of where the head is.
 *
 * `buffered` is a set of ranges, not a number, and after a seek the interesting one is the
 * range the head is inside - not the first one, which may be a stale fragment from before
 * the seek. A proxied stream fills these ranges visibly late, which is the whole reason
 * the indicator exists.
 */
function bufferedAhead(el: HTMLAudioElement): number {
  const ranges = el.buffered;
  const head = el.currentTime;
  let ahead = 0;
  for (let index = 0; index < ranges.length; index += 1) {
    const start = ranges.start(index);
    const end = ranges.end(index);
    if (start <= head + 0.25 && end > ahead) {
      ahead = end;
    }
  }
  return ahead;
}

/**
 * A play/pause button, a real slider, times, speed and mute - and nothing else.
 *
 * @see AudioPlayerProps - the owner supplies the source and every word.
 */
export function AudioPlayer({
  src,
  labels,
  duration = null,
  errorMessage = null,
  fallback = null,
  stallTimeoutMs = DEFAULT_STALL_MS,
  onError,
  onEnded,
}: AudioPlayerProps) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const seekRef = useRef<HTMLDivElement | null>(null);
  const stallRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Callbacks in refs, so arming a timer does not depend on the owner's render identity.
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  const onEndedRef = useRef(onEnded);
  onEndedRef.current = onEnded;

  const [playing, setPlaying] = useState(false);
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
  const [current, setCurrent] = useState(0);
  const [buffered, setBuffered] = useState(0);
  const [known, setKnown] = useState<number | null>(null);
  const [drag, setDrag] = useState<number | null>(null);
  const [rateIndex, setRateIndex] = useState(0);
  const [muted, setMuted] = useState(false);

  /*
   * A new recording is a new component as far as state is concerned - and the reset has
   * to happen *during the render that introduces the source*, not in an effect after it.
   * An effect runs after the DOM is written, and a source the browser rejects outright
   * can already have fired `error` by then: resetting there quietly wiped the failure and
   * left a dead transport looking perfectly healthy. React's documented "adjust state
   * when a prop changes" pattern puts the reset strictly before the element is touched.
   */
  const [loaded, setLoaded] = useState(src);
  if (loaded !== src) {
    setLoaded(src);
    setPlaying(false);
    setPending(false);
    setFailed(false);
    setCurrent(0);
    setBuffered(0);
    setKnown(null);
    setDrag(null);
  }

  const hinted = duration !== null && Number.isFinite(duration) && duration > 0 ? duration : 0;
  const total = known ?? hinted;
  const seekable = total > 0;
  const position = drag ?? current;
  const progress = seekable ? clamp(position / total, 0, 1) : 0;
  const bufferedRatio = seekable ? clamp(buffered / total, 0, 1) : 0;
  const rate = PLAYBACK_RATES[rateIndex] ?? 1;
  const showError = failed || Boolean(errorMessage);

  const clearStall = useCallback(() => {
    if (stallRef.current !== null) {
      clearTimeout(stallRef.current);
      stallRef.current = null;
    }
  }, []);

  const failWith = useCallback(
    (reason: AudioPlayerFailure) => {
      clearStall();
      setPending(false);
      setPlaying(false);
      setFailed(true);
      onErrorRef.current?.(reason);
    },
    [clearStall],
  );

  /**
   * Arm the first-byte deadline. Rule 2: the spinner is never the last state.
   *
   * Arming is deliberately **not** idempotent-by-restart. A source that is stuck emits
   * `waiting` and `stalled` over and over, and a deadline that restarted on each of them
   * would never be reached - which is precisely the forever-spinner this is here to
   * prevent. So an already-running deadline stands, and only real progress (`playing`)
   * or a pause clears it.
   */
  const armStall = useCallback(() => {
    if (stallRef.current !== null) {
      return;
    }
    stallRef.current = setTimeout(() => {
      const el = audioRef.current;
      if (el && !el.paused) {
        el.pause();
      }
      failWith('timeout');
    }, stallTimeoutMs);
  }, [failWith, stallTimeoutMs]);

  // A deadline belongs to the source that armed it, and to this mount.
  useEffect(() => clearStall, [src, clearStall]);

  /*
   * A failure that beat every listener into place.
   *
   * `preload="metadata"` starts fetching the moment the attribute lands, and a source the
   * browser refuses (a 404 page where audio was expected) can error before React has
   * attached its handler. The element remembers: `error` is still set here.
   */
  useEffect(() => {
    const el = audioRef.current;
    if (el?.error) {
      failWith('media');
    }
  }, [src, failWith]);

  // The element is the source of truth for rate and mute; React only mirrors them.
  useEffect(() => {
    const el = audioRef.current;
    if (el) {
      el.playbackRate = rate;
    }
  }, [rate]);

  useEffect(() => {
    const el = audioRef.current;
    if (el) {
      el.muted = muted;
    }
  }, [muted]);

  const commit = useCallback(
    (seconds: number) => {
      const el = audioRef.current;
      if (!el || !seekable) {
        return;
      }
      const next = clamp(seconds, 0, total);
      el.currentTime = next;
      setCurrent(next);
    },
    [seekable, total],
  );

  const seekBy = useCallback(
    (delta: number) => {
      commit(position + delta);
    },
    [commit, position],
  );

  const toggle = useCallback(() => {
    const el = audioRef.current;
    if (!el || failed) {
      return;
    }
    if (!el.paused) {
      el.pause();
      return;
    }
    setPending(true);
    // A press is a fresh attempt, so it gets a fresh deadline even if one was standing.
    clearStall();
    armStall();
    void el.play().catch((cause: unknown) => {
      // `AbortError` is a play interrupted by a pause - the listener's own doing, not a
      // failure. Anything else with a media error behind it is the real thing; anything
      // else without one (an autoplay refusal) simply leaves the button where it was,
      // because it is not a reason to spend the owner's one refresh request.
      const name = cause instanceof DOMException ? cause.name : '';
      clearStall();
      setPending(false);
      if (name === 'AbortError') {
        return;
      }
      if (el.error) {
        failWith('media');
      }
    });
  }, [armStall, clearStall, failWith, failed]);

  const toggleMute = useCallback(() => setMuted((value) => !value), []);

  const cycleRate = useCallback(() => {
    setRateIndex((index) => (index + 1) % PLAYBACK_RATES.length);
  }, []);

  // ---- media events ------------------------------------------------------

  const readDuration = useCallback(() => {
    const el = audioRef.current;
    if (el && Number.isFinite(el.duration) && el.duration > 0) {
      setKnown(el.duration);
    }
  }, []);

  const readProgress = useCallback(() => {
    const el = audioRef.current;
    if (el) {
      setBuffered(bufferedAhead(el));
    }
  }, []);

  const handleTimeUpdate = useCallback(() => {
    const el = audioRef.current;
    if (!el) {
      return;
    }
    if (drag === null) {
      setCurrent(el.currentTime);
    }
    setBuffered(bufferedAhead(el));
  }, [drag]);

  const handlePlaying = useCallback(() => {
    clearStall();
    setPending(false);
    setPlaying(true);
  }, [clearStall]);

  const handleWaiting = useCallback(() => {
    setPending(true);
    armStall();
  }, [armStall]);

  const handlePause = useCallback(() => {
    clearStall();
    setPending(false);
    setPlaying(false);
  }, [clearStall]);

  const handleEnded = useCallback(() => {
    clearStall();
    setPending(false);
    setPlaying(false);
    onEndedRef.current?.();
  }, [clearStall]);

  const handleMediaError = useCallback(() => failWith('media'), [failWith]);

  // ---- pointer scrubbing -------------------------------------------------

  const ratioAt = useCallback((clientX: number): number => {
    const el = seekRef.current;
    if (!el) {
      return 0;
    }
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) {
      return 0;
    }
    return clamp((clientX - rect.left) / rect.width, 0, 1);
  }, []);

  const handlePointerDown = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (!seekable || event.button !== 0) {
        return;
      }
      event.currentTarget.setPointerCapture(event.pointerId);
      event.currentTarget.focus();
      setDrag(ratioAt(event.clientX) * total);
    },
    [ratioAt, seekable, total],
  );

  const handlePointerMove = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (drag === null) {
        return;
      }
      event.preventDefault();
      setDrag(ratioAt(event.clientX) * total);
    },
    [drag, ratioAt, total],
  );

  /**
   * Commit on release, not on every move.
   *
   * Every `currentTime` write on a proxied source is a fresh range request upstream; a
   * scrub that wrote on each pointer move would fire dozens of them for one gesture.
   */
  const handlePointerUp = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (drag === null) {
        return;
      }
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
      commit(drag);
      setDrag(null);
    },
    [commit, drag],
  );

  // ---- keyboard ----------------------------------------------------------

  const handleSeekKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (!seekable) {
        return;
      }
      const steps: Readonly<Record<string, number | 'home' | 'end'>> = {
        ArrowLeft: -STEP,
        ArrowDown: -STEP,
        ArrowRight: STEP,
        ArrowUp: STEP,
        PageDown: -BIG_STEP,
        PageUp: BIG_STEP,
        Home: 'home',
        End: 'end',
      };
      const action = steps[event.key];
      if (action === undefined) {
        return;
      }
      event.preventDefault();
      // The root listens for the same arrows; the slider owns them when it has focus.
      event.stopPropagation();
      if (action === 'home') {
        commit(0);
      } else if (action === 'end') {
        commit(total);
      } else {
        commit(position + action);
      }
    },
    [commit, position, seekable, total],
  );

  /**
   * The shortcuts the whole transport answers to.
   *
   * The slider owns the arrows while it has focus (it stops them propagating) and a
   * focused button owns Space, so nothing is ever handled twice.
   */
  const handleRootKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const target = event.target as HTMLElement | null;
      const onButton = target?.tagName === 'BUTTON';
      if (event.key === ' ' || event.key === 'Spacebar' || event.code === 'Space') {
        if (onButton) {
          return;
        }
        event.preventDefault();
        toggle();
        return;
      }
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        seekBy(-STEP);
        return;
      }
      if (event.key === 'ArrowRight') {
        event.preventDefault();
        seekBy(STEP);
        return;
      }
      // `code` as well as `key`, so a Cyrillic layout gets the same shortcut.
      if (event.code === 'KeyM' || event.key.toLowerCase() === 'm') {
        event.preventDefault();
        toggleMute();
      }
    },
    [seekBy, toggle, toggleMute],
  );

  const elapsedText = formatDuration(position);
  const totalText = seekable ? formatDuration(total) : '—';
  const style: CssVars = {
    '--ca-ap-progress': String(progress),
    '--ca-ap-buffered': String(bufferedRatio),
  };

  return (
    <div
      className="ca-ap"
      style={style}
      role="group"
      aria-label={labels.player}
      aria-busy={pending}
      data-playing={playing ? 'true' : 'false'}
      tabIndex={0}
      onKeyDown={handleRootKeyDown}
    >
      <style href={STYLE_ID} precedence="default" dangerouslySetInnerHTML={{ __html: CSS }} />

      {/* The one and only place `src` is written. */}
      <audio
        ref={audioRef}
        className="ca-ap-media"
        // `metadata` so a dead link surfaces when the row opens rather than on the first
        // click, and so a ten-minute recording is not streamed into a row nobody plays.
        preload="metadata"
        src={src || undefined}
        onLoadedMetadata={readDuration}
        onDurationChange={readDuration}
        onTimeUpdate={handleTimeUpdate}
        onProgress={readProgress}
        onPlaying={handlePlaying}
        onWaiting={handleWaiting}
        onStalled={handleWaiting}
        onPause={handlePause}
        onEnded={handleEnded}
        onError={handleMediaError}
      >
        {labels.unsupported}
      </audio>

      <span className="ca-ap-sr" role="status">
        {failed ? labels.error : pending ? labels.loading : ''}
      </span>

      {showError ? (
        <div className="ca-ap-error">
          <p className="ca-ap-error-text">{errorMessage ?? labels.error}</p>
          {fallback}
        </div>
      ) : (
        <div className="ca-ap-bar">
          <button
            type="button"
            className="ca-ap-play"
            aria-label={playing ? labels.pause : labels.play}
            title={playing ? labels.pause : labels.play}
            onClick={toggle}
          >
            {pending ? <LoadingIcon /> : <TransportIcon />}
          </button>

          <div className="ca-ap-lane">
            <div
              ref={seekRef}
              className="ca-ap-seek"
              role="slider"
              tabIndex={0}
              aria-label={labels.seek}
              aria-valuemin={0}
              aria-valuemax={Math.round(total)}
              aria-valuenow={Math.round(position)}
              aria-valuetext={labels.position(elapsedText, totalText)}
              aria-disabled={seekable ? undefined : true}
              data-dragging={drag === null ? 'false' : 'true'}
              onKeyDown={handleSeekKeyDown}
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={handlePointerUp}
              onPointerCancel={handlePointerUp}
            >
              <div className="ca-ap-track">
                <div className="ca-ap-buffered" />
                <div className="ca-ap-played" />
              </div>
              <div className="ca-ap-knob" />
            </div>
            <div className="ca-ap-times">
              <span className="ca-ap-time" aria-label={labels.elapsed}>
                {elapsedText}
              </span>
              <span className="ca-ap-time" aria-label={labels.total}>
                {totalText}
              </span>
            </div>
          </div>

          <div className="ca-ap-tools">
            <button
              type="button"
              className="ca-ap-chip"
              aria-label={`${labels.rate}: ${rate}×`}
              title={labels.rate}
              onClick={cycleRate}
            >
              {`${rate}×`}
            </button>
            <button
              type="button"
              className="ca-ap-chip ca-ap-chip-icon"
              aria-label={muted ? labels.unmute : labels.mute}
              aria-pressed={muted}
              title={muted ? labels.unmute : labels.mute}
              onClick={toggleMute}
            >
              {muted ? <MutedIcon /> : <SoundIcon />}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Play and pause on one canvas.
 *
 * Both glyphs are always in the DOM; the CSS above fades and scales one out as the other
 * comes in, so the button reads as a single shape changing rather than two icons swapping.
 */
function TransportIcon() {
  return (
    <svg
      className="ca-ap-icon"
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path
        className="ca-ap-glyph ca-ap-glyph-play"
        d="M9.8 7.1 17.2 12 9.8 16.9Z"
        fill="currentColor"
      />
      <g className="ca-ap-glyph ca-ap-glyph-pause" fill="currentColor">
        <rect x="9.1" y="7.2" width="1.9" height="9.6" rx="0.95" />
        <rect x="13" y="7.2" width="1.9" height="9.6" rx="0.95" />
      </g>
    </svg>
  );
}

/** Waiting for the first byte. Always bounded by the stall deadline. */
function LoadingIcon() {
  return (
    <svg
      className="ca-ap-icon"
      width="20"
      height="20"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="12" cy="12" r="7.5" opacity="0.3" />
      <path className="ca-ap-spin" d="M12 4.5a7.5 7.5 0 0 1 7.5 7.5" />
    </svg>
  );
}

function SoundIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M4 9.5v5h3.2L11 18V6L7.2 9.5H4z" />
      <path d="M14.6 9.4a3.6 3.6 0 0 1 0 5.2" />
      <path d="M17.2 6.8a7.2 7.2 0 0 1 0 10.4" />
    </svg>
  );
}

function MutedIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M4 9.5v5h3.2L11 18V6L7.2 9.5H4z" />
      <path d="m15 9.8 4.4 4.4" />
      <path d="m19.4 9.8-4.4 4.4" />
    </svg>
  );
}

export default AudioPlayer;
