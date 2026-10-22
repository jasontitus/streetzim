// The UI gate's html_lang verdict (ui_gate.mjs), from what the page shows.
//
// The UI must be in the map's language: a map language without a UI table
// FAILS (a Thai map with an English UI is not a Thai map), unless the viewer
// declares a fallback for it (SZ_UI_FALLBACK: Romansh -> German, a language
// whose speakers all read the fallback) and shows that one: then the UI and
// <html lang> are the fallback's, and the verdict says so. Right-to-left
// languages need <html dir="rtl">. English (base, en entry): English UI.
//
// facts: {lang, uiLang, hasTable (a table object is loaded), htmlLang, dir,
//         rtl (lang is right to left), shownCount (translated strings on
//         screen), fallback (SZ_UI_FALLBACK[lang] or null)}
export function uiVerdict(f) {
  const why = [];
  let ui = f.lang;
  if (f.lang === 'en') {
    if (f.uiLang !== 'en') why.push(`UI in ${f.uiLang}, not English`);
  } else {
    if (f.fallback && f.uiLang === f.fallback && f.hasTable) ui = f.fallback;
    if (!f.hasTable || f.uiLang !== ui) {
      why.push(`no ${f.lang} UI table: the UI is in ${f.uiLang || '?'}`
        + (f.fallback ? ` (the viewer's fallback for ${f.lang} is ${f.fallback})` : ''));
    }
    if ((f.htmlLang || '').split('-')[0] !== ui) why.push(`<html lang="${f.htmlLang}">`);
    if (f.hasTable && !f.shownCount) why.push('no translated string on screen');
    if (f.rtl && f.dir !== 'rtl') why.push(`<html dir="${f.dir}"> for a right-to-left language`);
  }
  return { ui, fallbackUsed: ui !== f.lang ? ui : null, why, pass: why.length === 0 };
}
