/**
 * The message catalogues have to agree with each other, and nothing else checks that.
 *
 *   node tools/check-i18n.mjs
 *
 * Three failures this catches, all of which ship silently otherwise because a missing
 * key renders as the key itself in one language and nobody reads that language:
 *
 *  1. **A key in one bundle and not the other.** Four people adding strings to the same
 *     two files in the same afternoon is exactly how this happens.
 *  2. **A declared locale with no bundle.** `src/i18n/locales.json` is read by the SPA
 *     *and* by `api/app/i18n.py`, so a locale declared without a `messages/<code>.json`
 *     makes the server-rendered state pages fall back while the SPA does not.
 *  3. **A translation that dropped a placeholder.** `"{count} calls"` translated as
 *     "Звонки" loses the number with no error at all - `next-intl` renders the message
 *     it was given. Comparing the ICU argument sets per key is what finds it.
 *
 * Exits non-zero with the offending keys listed. No dependencies: it runs on the bare
 * runner before anything is installed.
 */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const repo = join(dirname(fileURLToPath(import.meta.url)), '..');
const localesPath = join(repo, 'web/src/i18n/locales.json');
const messagesDir = join(repo, 'web/messages');

const read = (p) => JSON.parse(readFileSync(p, 'utf8'));

/** `{a: {b: 1}}` -> `{'a.b': 1}`, so two catalogues can be compared as flat key sets. */
function flatten(value, prefix = '', out = {}) {
  for (const [key, child] of Object.entries(value)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (child && typeof child === 'object' && !Array.isArray(child)) {
      flatten(child, path, out);
    } else {
      out[path] = child;
    }
  }
  return out;
}

/**
 * The ICU argument names in a message.
 *
 * Deliberately shallow: it takes the identifier after each `{` and ignores the rest of
 * the construct, so `{count, plural, one {# call} other {# calls}}` yields `count` and
 * the nested `#` forms contribute nothing. That is enough to catch a dropped variable
 * without re-implementing an ICU parser.
 */
function icuArgs(message) {
  if (typeof message !== 'string') return new Set();
  const args = new Set();
  for (const match of message.matchAll(/\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,}]/g)) {
    args.add(match[1]);
  }
  return args;
}

const problems = [];

const locales = read(localesPath);
const declared = locales.locales;

// 2. every declared locale has a bundle, and the default is one of them
for (const locale of declared) {
  const bundle = join(messagesDir, `${locale}.json`);
  if (!existsSync(bundle)) {
    problems.push(`locales.json declares "${locale}" but web/messages/${locale}.json does not exist`);
  }
}
if (!declared.includes(locales.default)) {
  problems.push(`locales.json default "${locales.default}" is not in its own locales list`);
}
for (const [from, to] of Object.entries(locales.fallback ?? {})) {
  if (!declared.includes(to)) {
    problems.push(`locales.json maps "${from}" -> "${to}", which is not a declared locale`);
  }
}

const bundles = new Map();
for (const locale of declared) {
  const bundle = join(messagesDir, `${locale}.json`);
  if (existsSync(bundle)) bundles.set(locale, flatten(read(bundle)));
}

// 1. the key sets are identical. The default locale is the reference, because it is the
//    one a missing translation falls back to.
const reference = locales.default;
const referenceKeys = bundles.get(reference);
if (referenceKeys) {
  for (const [locale, keys] of bundles) {
    if (locale === reference) continue;
    for (const key of Object.keys(referenceKeys)) {
      if (!(key in keys)) problems.push(`${locale}.json is missing "${key}" (present in ${reference}.json)`);
    }
    for (const key of Object.keys(keys)) {
      if (!(key in referenceKeys)) problems.push(`${locale}.json has "${key}", which ${reference}.json does not`);
    }
  }

  // 3. same ICU arguments per key, in every locale that has the key
  for (const [locale, keys] of bundles) {
    if (locale === reference) continue;
    for (const [key, message] of Object.entries(keys)) {
      if (!(key in referenceKeys)) continue;
      const want = icuArgs(referenceKeys[key]);
      const got = icuArgs(message);
      const missing = [...want].filter((a) => !got.has(a));
      const extra = [...got].filter((a) => !want.has(a));
      if (missing.length) problems.push(`${locale}.json "${key}" drops placeholder(s): ${missing.join(', ')}`);
      if (extra.length) problems.push(`${locale}.json "${key}" adds placeholder(s) ${reference}.json has no value for: ${extra.join(', ')}`);
    }
  }
}

const counts = [...bundles].map(([l, k]) => `${l}=${Object.keys(k).length}`).join(' ');
if (problems.length) {
  console.error(`i18n catalogues disagree (${counts}):\n`);
  for (const p of problems) console.error(`  - ${p}`);
  console.error(`\n${problems.length} problem(s).`);
  process.exit(1);
}
console.log(`i18n catalogues agree: ${counts}`);
