'use client';

import { useTranslations } from 'next-intl';
import { useEffect, type ReactNode } from 'react';

import { StateCard } from '@/components/StateCard';
import { presentError } from '@/lib/api';
import { fitWindow, isAvailable, placementInfo } from '@/lib/bx24';
import { captureToken } from '@/lib/session';

/**
 * The shell every page renders inside (§4.6, §4.10).
 *
 * It does exactly two things, and both of them have to happen before anything else:
 *
 *  1. **Take the session token out of the URL.** `handoff.html` delivers the JWT as
 *     `#s=<jwt>`; §4.6 says read it once and `history.replaceState` it away. Child
 *     effects run before parent effects in React, so `lib/session.ts` also captures
 *     lazily on first `getToken()` - this call is the guarantee for pages that never
 *     fetch anything (a state page, `/`).
 *  2. **Keep the Bitrix24 slider the size of the content.** §4.10: `BX24.fitWindow()`
 *     after first paint and on every debounced `ResizeObserver` change. Without it the
 *     frame keeps whatever height Bitrix24 guessed and the page scrolls inside a stub.
 *
 * There is no app chrome here on purpose: this renders inside a Bitrix24 slider, and a
 * second header, a second background or a second scrollbar would make it read as a
 * third-party page bolted in.
 */

/** Enough to coalesce a chart re-layout, short enough to feel immediate. */
const RESIZE_DEBOUNCE_MS = 120;

export interface AppFrameProps {
  children: ReactNode;
}

export function AppFrame({ children }: AppFrameProps) {
  useEffect(() => {
    captureToken();
  }, []);

  useEffect(() => {
    if (!isAvailable()) {
      // Opened outside Bitrix24 (the `/` route, a moderator pasting the URL): there is
      // no parent frame to resize, and the SDK must not even be loaded.
      return;
    }

    let timer: number | undefined;
    let cancelled = false;

    const schedule = () => {
      if (cancelled) {
        return;
      }
      window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        void fitWindow();
      }, RESIZE_DEBOUNCE_MS);
    };

    // First paint: `useEffect` already runs after the browser has laid the page out.
    void fitWindow();

    // §10 step 3 asks for proof, on the dev portal and on every placement, that the
    // SDK actually reached the parent frame and that Bitrix24 agrees with the
    // `PLACEMENT` the backend routed on. One console line is the cheapest evidence and
    // it exposes nothing: `placement.info()` carries no token.
    void placementInfo().then((info) => {
      if (!cancelled && info) {
        console.info('BX24.placement.info()', info.placement, info.options);
      }
    });

    const target = document.documentElement;
    const observer = new ResizeObserver(schedule);
    observer.observe(target);
    window.addEventListener('resize', schedule);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      observer.disconnect();
      window.removeEventListener('resize', schedule);
    };
  }, []);

  // No `min-h-screen`: the height of this element is what `fitWindow()` measures, so
  // forcing a viewport height would pin the slider open at full size forever.
  return <div className="ca-page w-full">{children}</div>;
}

/*
 * ---------------------------------------------------------------------------------
 * Shell-level pieces the three placeholder views share.
 *
 * They live in this file rather than in files of their own because milestone 3 owns a
 * fixed file list; milestone 5, which replaces the page bodies with the real charts and
 * tables, is where they earn their own modules. What matters now is that the loading,
 * error and denied paths are written ONCE: §4.11 makes each of them a rendered,
 * translated page, and three hand-rolled copies is how one of them ends up blank.
 * ---------------------------------------------------------------------------------
 */

export interface PageShellProps {
  title: string;
  /** A short line under the title: who is signed in, which entity, which portal. */
  subtitle?: ReactNode;
  /** Small pill(s) to the right of the title: access level, placement. */
  chips?: ReactNode;
  /** Full-width notice above the content ("you see your own calls only"). */
  banner?: ReactNode;
  children: ReactNode;
}

/** The one page layout: a title block, an optional banner, then content. */
export function PageShell({ title, subtitle, chips, banner, children }: PageShellProps) {
  return (
    <div className="mx-auto w-full max-w-5xl px-6 py-8">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-[20px] font-semibold leading-tight">{title}</h1>
          {subtitle ? <p className="ca-muted mt-1 text-[13px]">{subtitle}</p> : null}
        </div>
        {chips ? <div className="flex flex-wrap items-center gap-2">{chips}</div> : null}
      </header>
      {banner ? <div className="ca-banner mb-6">{banner}</div> : null}
      <div className="flex flex-col gap-5">{children}</div>
    </div>
  );
}

/** A titled card. Milestone 5 drops charts and tables into these. */
export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="ca-card px-6 py-5">
      <h2 className="ca-muted mb-4 text-[12px] font-semibold uppercase tracking-wider">{title}</h2>
      {children}
    </section>
  );
}

/** One label/value row. `value` is already formatted and already translated. */
export function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-1.5">
      <dt className="ca-muted w-56 shrink-0 text-[13px]">{label}</dt>
      <dd className="min-w-0 break-words text-[14px]">{value}</dd>
    </div>
  );
}

/** Wraps {@link Field} rows. */
export function FieldList({ children }: { children: ReactNode }) {
  return <dl>{children}</dl>;
}

/** Content-shaped placeholder while `GET /me` is in flight. */
export function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="mx-auto w-full max-w-5xl px-6 py-8" role="status" aria-label={label}>
      <div className="ca-skeleton h-6 w-56" />
      <div className="ca-skeleton mt-3 h-4 w-80" />
      <div className="ca-card mt-6 px-6 py-5">
        <div className="ca-skeleton h-4 w-40" />
        <div className="ca-skeleton mt-4 h-4 w-full" />
        <div className="ca-skeleton mt-2 h-4 w-5/6" />
        <div className="ca-skeleton mt-2 h-4 w-2/3" />
      </div>
    </div>
  );
}

/**
 * Any thrown value as one of the §4.11 states, in the mandated words.
 *
 * `presentError` decides which state and which catalogue key; nothing from the error
 * object itself is ever rendered.
 */
export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const t = useTranslations();
  const { kind, titleKey, bodyKey } = presentError(error);
  return (
    <StateCard
      kind={kind}
      title={t(titleKey)}
      body={t(bodyKey)}
      action={
        onRetry ? (
          <button type="button" className="ca-button" onClick={onRetry}>
            {t('app.retry')}
          </button>
        ) : undefined
      }
    />
  );
}

export default AppFrame;
