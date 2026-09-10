import type { ReactNode } from 'react';

/**
 * The shell the two public legal documents render into.
 *
 * These pages are the odd ones out in this app. Everything else is a panel inside a
 * Bitrix24 slider, deliberately without chrome, and sized by `BX24.fitWindow()`. The
 * licence and the privacy policy are the opposite: standalone pages on
 * `b24.texnobus.uz`, opened directly from the Marketplace listing, most often by a
 * moderator and occasionally by a customer's lawyer. So they get a header, a readable
 * measure and a footer - and they still have to look like the same product.
 *
 * That is why the visual language is the app's own token layer (`globals.css`) rather
 * than a second palette: the same surface, ink, accent and border, the same system font
 * stack, the same light/dark behaviour. What changes is the rhythm. A dashboard packs
 * information; a contract has to be read start to finish, so the measure is capped near
 * 70 characters, the line-height opens up, and the sections are far enough apart to be
 * scanned by heading.
 *
 * `AppFrame` still wraps these pages, and that is harmless on purpose: outside a
 * Bitrix24 iframe `isAvailable()` is false, so no SDK is fetched and no resize observer
 * is installed, and a URL with no `#s=` gives `captureToken()` nothing to capture.
 */

export interface LegalDocumentProps {
  /** The document's own title, rendered as the only `h1` on the page. */
  title: string;
  /** The line under it: which app, which solution code, which revision. */
  subtitle: ReactNode;
  /** The other document. Both pages point at each other so neither is a dead end. */
  sibling: { href: string; label: string };
  children: ReactNode;
}

export function LegalDocument({ title, subtitle, sibling, children }: LegalDocumentProps) {
  return (
    <>
      <style href="ca-legal" precedence="default" dangerouslySetInnerHTML={{ __html: CSS }} />
      {/*
        `lang="ru"` on the document itself, not inherited. The root layout sets
        `<html lang>` from Bitrix24's `LANG`, which can be `en` for a viewer whose portal
        is English - but these two documents exist only in Russian, and telling a screen
        reader to pronounce Russian as English is worse than a mismatched attribute.
      */}
      <article className="ca-legal" lang="ru">
        <header className="ca-legal-head">
          <h1 className="ca-legal-title">{title}</h1>
          <p className="ca-legal-sub">{subtitle}</p>
        </header>

        {children}

        <footer className="ca-legal-foot">
          <p className="ca-legal-also">
            <a href={sibling.href}>{sibling.label}</a>
          </p>
          <p className="ca-legal-sig">
            Jabborov Abduroziq Narimon o'g'li ·{' '}
            <a href="mailto:support@texnobus.uz">support@texnobus.uz</a> · b24.texnobus.uz
          </p>
        </footer>
      </article>
    </>
  );
}

/**
 * A callout for the one paragraph in the privacy policy that answers the question every
 * reader actually arrived with: what you do *not* keep.
 */
export function LegalNote({ children }: { children: ReactNode }) {
  return <aside className="ca-legal-note">{children}</aside>;
}

const CSS = `
.ca-legal{
  box-sizing:border-box;
  max-width:74ch;
  margin:0 auto;
  padding:40px 20px 72px;
  color:var(--ca-text);
  font-size:16px;
  line-height:1.7;
}
@media (min-width:640px){.ca-legal{padding:56px 32px 96px;}}

.ca-legal-head{margin:0 0 32px;padding-bottom:24px;border-bottom:1px solid var(--ca-border);}
.ca-legal-title{margin:0 0 10px;font-size:27px;line-height:1.25;font-weight:650;letter-spacing:-0.01em;}
@media (max-width:479px){.ca-legal-title{font-size:23px;}}
.ca-legal-sub{margin:0;color:var(--ca-muted);font-size:14px;line-height:1.55;}

/* Sections are separated by space rather than by rules: a contract is already a list of
 * numbered blocks, and a line above every one of them turns it into a table. */
.ca-legal h2{
  margin:38px 0 12px;
  font-size:18px;
  line-height:1.35;
  font-weight:650;
  letter-spacing:-0.005em;
  /* The heading and its first paragraph must not be split across a printed page: these
   * documents get printed to PDF for procurement more often than one would think. */
  break-after:avoid;
}
.ca-legal h2:first-of-type{margin-top:0;}
.ca-legal p{margin:0 0 12px;}
.ca-legal ul{margin:0 0 12px;padding-left:22px;}
.ca-legal li{margin:0 0 8px;}
.ca-legal li::marker{color:var(--ca-muted);}
.ca-legal strong{font-weight:600;}

.ca-legal a{
  color:var(--ca-accent);
  text-decoration:underline;
  text-underline-offset:2px;
  text-decoration-thickness:1px;
  border-radius:3px;
}
.ca-legal a:hover{text-decoration-thickness:2px;}

/* An email address inside running text must not force the page sideways on a phone. */
.ca-legal a[href^="mailto:"]{word-break:break-word;}

.ca-legal-note{
  margin:20px 0;
  padding:16px 18px;
  border:1px solid var(--ca-border);
  border-left:3px solid var(--ca-accent);
  border-radius:var(--ca-radius);
  background:var(--ca-surface-soft);
}
.ca-legal-note p{margin:0;}

.ca-legal-foot{
  margin-top:48px;
  padding-top:20px;
  border-top:1px solid var(--ca-border);
  color:var(--ca-muted);
  font-size:14px;
}
/* The cross-link to the other document is navigation, not prose: it is the only thing in
 * its line and someone will tap it. WCAG 2.5.8 exempts a link inside a sentence from the
 * target-size floor precisely because enlarging it would wreck the paragraph - that
 * exemption does not apply here, so this one gets a real box. */
.ca-legal-also{margin:0 0 4px;}
.ca-legal-also a{
  display:inline-flex;
  align-items:center;
  min-height:var(--ca-control-h);
  margin-left:-8px;
  padding:0 8px;
  border-radius:var(--ca-radius);
  text-decoration-thickness:1px;
  transition:background-color var(--ca-dur-fast) var(--ca-ease);
}
.ca-legal-also a:hover{background:var(--ca-accent-soft);}
.ca-legal-sig{margin:0;}

@media print{
  .ca-legal{max-width:none;padding:0;font-size:11pt;}
  .ca-legal a{color:inherit;text-decoration:none;}
  .ca-legal-note{background:none;}
}
`;

export default LegalDocument;
