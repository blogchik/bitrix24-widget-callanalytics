import type { ReactNode } from 'react';

/**
 * The §4.11 states, rendered client-side.
 *
 * The list is deliberately identical to `STATE_KINDS` in
 * `api/app/handlers/render.py`: the same nine kinds the API renders server-side into
 * `state.html`, plus `bad_request` and `admin_only`, which only the API can reach
 * today but which must not blank the frame if a link to them ever appears. Copy comes
 * from the one shared catalogue (`state.<kind>.title` / `.body`, §8), so a portal that
 * saw the server-rendered page and then lands here reads the exact same sentence.
 */
export const STATE_KINDS = [
  'denied',
  'not_installed',
  'reauth',
  'retry',
  'scope',
  'method_missing',
  'crm_no_access',
  'unsupported_portal',
  'error',
  'bad_request',
  'admin_only',
] as const;

export type StateKind = (typeof STATE_KINDS)[number];

/** Anything else is a caller bug and renders the generic `error` copy. */
export const FALLBACK_STATE_KIND: StateKind = 'error';

export function isStateKind(value: string): value is StateKind {
  return (STATE_KINDS as readonly string[]).includes(value);
}

export interface StateCardProps {
  /** Names the §4.11 kind in the markup, like `state.html`'s `data-state`. */
  kind?: string;
  title: string;
  body: string;
  /** A second paragraph: a countdown, a "what to do next" line. */
  hint?: string;
  /** A button or link; keep it to one, this page is a dead end by design. */
  action?: ReactNode;
}

/**
 * One centred card with a translated title and body - never a raw error string.
 *
 * No hooks and no client directive, so both Server and Client Components can render it.
 */
export function StateCard({ kind, title, body, hint, action }: StateCardProps) {
  return (
    <div
      data-state={kind}
      className="mx-auto flex w-full max-w-xl flex-col items-start gap-3 px-6 py-10"
    >
      <div className="ca-card w-full px-7 py-6">
        <h1 className="text-[19px] font-semibold leading-snug">{title}</h1>
        <p className="mt-3 text-[15px] leading-relaxed">{body}</p>
        {hint ? <p className="ca-muted mt-2 text-[13px] leading-relaxed">{hint}</p> : null}
        {action ? <div className="mt-5">{action}</div> : null}
      </div>
    </div>
  );
}

export default StateCard;
