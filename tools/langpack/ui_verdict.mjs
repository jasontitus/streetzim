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

// The labels verdict (ui_gate.mjs). On the base or an en entry: one name,
// the local one. On a map in another language: the style reads name:<LANG>,
// the viewer's second language is LANG, the city's label is what the label
// rule says, no rendered label breaks the rule, and some rendered label
// shows two names, unless that cannot happen: the map language is one of the
// region's own (its places' names are the native ones), or no rendered place
// names itself differently in LANG while the tiles do carry LANG (map-config
// tileLanguages): Vietnamese on Switzerland, where Zurich is "Zürich" in
// both and the city's views hold no other name (the gate failed it).
//
// facts: {second, usesKey, lang2, lang, cityRendered, cityShown, cityWant,
//         cityLocal, bilingual (count), bilingualWrong (count), ownLang,
//         tileLanguages}
export function labelsVerdict(f) {
  if (!f.second) {
    const pass = f.lang2 === '' && f.cityRendered && f.cityShown === f.cityLocal;
    return { pass, vacuous: false };
  }
  const tiles = Array.isArray(f.tileLanguages) && f.tileLanguages.includes(f.lang);
  const vacuous = !f.ownLang && f.bilingual === 0 && tiles;
  const pass = f.usesKey && f.lang2 === f.lang && f.cityRendered && f.cityShown === f.cityWant
    && (f.ownLang || f.bilingual > 0 || vacuous) && f.bilingualWrong === 0;
  return { pass, vacuous };
}

// The search check's place among the results: a row whose shown name, or
// whose data-name (the record's own name, whatever the map's language: a map
// in German shows Warsaw as Warschau), is the name the region file expects (a
// name or a list), alone or followed by more ("Warsaw Chopin").
export function searchFound(rows, expect) {
  const want = [].concat(expect).filter((e) => typeof e === 'string' && e);
  const is = (n) => typeof n === 'string' && want.some((e) => n === e || n.startsWith(e + ' '));
  return rows.some((r) => is(r.shown) || is(r.name));
}
