'use client';

import { useTranslations } from 'next-intl';
import { useEffect, useState, type ReactNode } from 'react';

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
  const [standalone, setStandalone] = useState(false);
  useEffect(() => {
    captureToken();
  }, []);

  // Its own effect, and not a line inside the one below: that one returns early when the
  // SDK is absent, which is precisely the case this needs to detect.
  useEffect(() => {
    setStandalone(!isAvailable());
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

  /*
   * A viewport-height floor, and ONLY when this is not a Bitrix24 slider.
   *
   * Inside one, the height of this element is what `fitWindow()` measures and reports to
   * the parent frame. A `100vh` floor there resolves against the iframe's CURRENT height,
   * so every measurement would come back at least as tall as the frame already is: the
   * slider could grow and never shrink, and switching from a month to a single day would
   * leave the frame stretched around a two-row table forever. That is why this file
   * refused a blanket `min-h-screen`, and it still refuses one.
   *
   * Standalone - a browser tab, the audit harness - nothing is measuring and nothing is
   * resized, so a short page simply ends partway down the window and the rest is
   * browser-coloured nothing. There the floor is exactly what is wanted.
   *
   * Decided after mount rather than during render: `isAvailable()` reads `window`, so the
   * server renders `false` and a value read during the first client render would disagree
   * with the HTML it is hydrating.
   */
  return (
    <div className={standalone ? 'ca-page w-full min-h-screen' : 'ca-page w-full'}>
      {children}
    </div>
  );
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
  /**
   * Let the column grow past the reading width, for the data pages.
   *
   * The 1280px cap is sized for prose. It is the wrong measure for a page whose content
   * is a grid: the by-hour table is twenty-six columns and wants about 1430px, so under
   * that cap it scrolled sideways on a 1920px screen with seven hundred spare pixels on
   * either side of it - a scrollbar the display never needed, put there by the layout
   * rather than by the data. The dashboard is the same kind of page and reads the same
   * way, so the two left-menu pages share the measure; only the narrow CRM tab and the
   * text pages keep the reading width.
   */
  wide?: boolean;
  /**
   * Links between the pages of this placement, under the header.
   *
   * Only the left-menu placement has more than one page to move between, so the CRM tab
   * and the settings page pass nothing and render exactly as before. It is a slot rather
   * than a fixed list because this shell is also what the state and error pages use, and
   * those must never offer a link to a page the reader cannot open.
   */
  nav?: ReactNode;
  children: ReactNode;
}

/**
 * The one page layout: a title block, an optional banner, then content.
 *
 * Two measurements decide the numbers here, and both are about the frame this renders
 * in rather than about taste:
 *
 *  * **The column is 1280px on a wide slider, not 1024.** A Bitrix24 slider opens close
 *    to the full width of a desktop browser; at 1440 the old `max-w-5xl` left ~230px of
 *    empty margin on each side while the summary tiles inside it were narrow enough to
 *    wrap their own comparison line. Width is the cheapest fix for a cramped tile.
 *  * **The gutter is 16px on a phone, not 24.** The dashboard puts its two chart panels
 *    in a `minmax(340px, 1fr)` grid, so at 375 the panel is 340px wide whatever the
 *    padding is; every pixel taken off the gutter goes to the heatmap inside it.
 *
 * Everything is a multiple of 4, and vertical steps come in one pair (16 on a phone,
 * 20-32 from `sm` up) so the page has a single rhythm instead of one per component.
 */
export function PageShell({
  title,
  subtitle,
  chips,
  banner,
  nav,
  wide = false,
  children,
}: PageShellProps) {
  return (
    <div
      className={`${wide ? WIDE_SHELL_WIDTH : SHELL_WIDTH} px-4 py-6 sm:px-6 lg:px-8 lg:py-8`}
    >
      <header className="mb-4 flex flex-wrap items-start justify-between gap-3 sm:mb-6">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold leading-tight">{title}</h1>
          {subtitle ? <p className="ca-muted mt-1 text-[13px]">{subtitle}</p> : null}
        </div>
        {chips ? <div className="flex flex-wrap items-center gap-2">{chips}</div> : null}
      </header>
      {nav ? <div className="mb-4 sm:mb-5">{nav}</div> : null}
      {banner ? <div className="ca-banner mb-4 sm:mb-6">{banner}</div> : null}
      <div className="flex flex-col gap-4 sm:gap-5">{children}</div>
    </div>
  );
}

/** The content column, shared by {@link PageShell} and {@link LoadingBlock}. */
const SHELL_WIDTH = 'mx-auto w-full max-w-[1280px]';

/**
 * The column for a page that is one wide table.
 *
 * Still capped, and the cap is not arbitrary: the grid is `width: max-content` with
 * `min-width: 100%`, so a container wider than the table stretches its columns rather than
 * leaving a margin. 1600px clears the grid's natural width without pulling twenty-four
 * columns of numbers apart on an ultrawide display.
 */
const WIDE_SHELL_WIDTH = 'mx-auto w-full max-w-[1600px]';

/**
 * A titled card. Milestone 5 drops charts and tables into these.
 *
 * `min-w-0` is load-bearing: these are grid items on the dashboard, and without it a
 * chart's own minimum content width would push the card wider than its track instead of
 * letting the chart shrink (or, where it genuinely cannot, scroll inside itself).
 */
export function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="ca-card min-w-0 px-4 py-4 sm:px-6 sm:py-5">
      <h2 className="ca-muted mb-3 text-[12px] font-semibold uppercase tracking-wider sm:mb-4">
        {title}
      </h2>
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
    <div
      className={`${SHELL_WIDTH} px-4 py-6 sm:px-6 lg:px-8 lg:py-8`}
      role="status"
      aria-label={label}
    >
      <div className="ca-skeleton h-6 w-56" />
      <div className="ca-skeleton mt-3 h-4 w-80" />
      <div className="ca-card mt-6 px-4 py-4 sm:px-6 sm:py-5">
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

/**
 * The error that arrived while a previous answer is still on screen.
 *
 * Distinct from {@link ErrorState}, which replaces the page: §4.11 asks for a view that
 * never goes blank, so a refetch that failed says so in a band above data that is merely
 * stale rather than throwing away numbers the reader can still use. It lives here for the
 * same reason the states above do - two hand-rolled copies is how one of them ends up
 * saying something different about the same failure.
 */
export function StaleNotice({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const t = useTranslations();
  const { bodyKey } = presentError(error);
  return (
    <div className="ca-banner flex flex-wrap items-center gap-x-4 gap-y-2" role="status">
      <span className="flex-1">{t(bodyKey)}</span>
      <button type="button" className="ca-button ca-button-quiet" onClick={onRetry}>
        {t('app.retry')}
      </button>
    </div>
  );
}

export default AppFrame;
