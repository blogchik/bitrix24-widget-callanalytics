'use client';

/**
 * The two pages of the left-menu placement, as links.
 *
 * They are `next/link`, not buttons that push a route, for one reason that matters here:
 * the session token lives in a module variable with a `sessionStorage` copy, so a soft
 * navigation keeps it and a reload recovers it — but only a real link gives a reader the
 * middle-click, the keyboard focus order and the "current page" semantics they already
 * know. `aria-current="page"` is what a screen reader reads; the underline is what
 * everyone else does, and neither is the only cue.
 *
 * `/settings` is offered to administrators and to nobody else (§4.5) - showing it to a
 * viewer who cannot open it would put a door in front of them. It used to be left out
 * entirely, on the reasoning that Bitrix24's own settings placement was the way in. That
 * was true of the Marketplace registration; a LOCAL application's form has no settings
 * path at all, so this nav became the only door and there was none.
 *
 * Its href carries the Bitrix24 query string for the reason `SyncBanner` gives: `APP_SID`
 * has to survive the navigation or `BX24.init` never fires on the page it lands on
 * (§4.4 step 8).
 */

import Link from 'next/link';
import { useEffect, useState } from 'react';

export interface PageNavItem {
  href: string;
  label: string;
}

export interface PageNavProps {
  items: readonly PageNavItem[];
  /** The settings tab's label, already translated. Omitted for a non-administrator. */
  settingsLabel?: string;
  /** The route that is open now, as `usePathname()` returns it. */
  current: string;
  /** Accessible name for the nav landmark, already translated. */
  label: string;
}

export const NAV_CSS = `
.ca-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  border-bottom: 1px solid var(--ca-border);
}
.ca-nav a {
  display: inline-flex;
  align-items: center;
  /* 40px of height before the border, so the target clears the coarse-pointer floor
     without the tab looking like a button. */
  min-height: 40px;
  padding: 0 12px;
  margin-bottom: -1px;
  border-bottom: 2px solid transparent;
  color: var(--ca-muted);
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
  transition: color var(--ca-dur-fast) var(--ca-ease),
    border-color var(--ca-dur-fast) var(--ca-ease);
}
.ca-nav a:hover {
  color: var(--ca-text);
}
.ca-nav a[aria-current='page'] {
  color: var(--ca-text);
  border-bottom-color: var(--ca-accent);
}
@media (pointer: coarse) {
  .ca-nav a {
    min-height: 44px;
  }
}
`;

export function PageNav({ items, current, label, settingsLabel }: PageNavProps) {
  const [settingsHref, setSettingsHref] = useState('/settings');

  useEffect(() => {
    setSettingsHref(`/settings${window.location.search}`);
  }, []);

  return (
    <nav className="ca-nav" aria-label={label}>
      {items.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          aria-current={item.href === current ? 'page' : undefined}
        >
          {item.label}
        </Link>
      ))}
      {settingsLabel ? (
        <Link
          href={settingsHref}
          aria-current={current === '/settings' ? 'page' : undefined}
        >
          {settingsLabel}
        </Link>
      ) : null}
    </nav>
  );
}

export default PageNav;
