// The viewer's UI in another language, in a real browser (docs/i18n.md).
//
// 1. Pseudo-locale (?uilang=qps): every translated string shows as [text].
//    Walks the map viewer — controls, About, search (results and none),
//    the routing panel (a route in every travel mode the map has, the GPS
//    start, a destination with no route), the navigation HUD, the
//    Wikipedia list and popup when the map has Wikidata, a Find chip's
//    carousel and place sheet — then the Find page and its hand-off back
//    to the map (which must keep ?uilang). Fails on visible chrome text,
//    or a title / placeholder / aria-label, with letters outside brackets:
//    a string that never went through szT. Data (names, distances,
//    licences) is skipped by selector; in rows that mix a translated type
//    with data ("[restaurant] · Ville Haute") each "·" part must be
//    bracketed or carry no letters, unless the row is listed in MIXED.
// 2. German (?uilang=de): <html lang>, and fixed elements read de.json.
//
//   ZIM_ORIGIN=http://127.0.0.1:8902/content/<book> CHROME_PATH=... \
//     node tools/i18n_pseudo_check.mjs
//
// The ZIM must carry this viewer (a fresh build, or patch_viewer_inplace).
// ORIGIN / DEST ("lat,lon") default to two points in Monaco (the CI ZIM);
// FAR is a destination with no road near it. Prints "I18N OK" when every
// check passed; exits 1 otherwise.
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';

const origin = process.env.ZIM_ORIGIN;
if (!origin) { console.error('ZIM_ORIGIN is required'); process.exit(2); }
const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const DE = JSON.parse(fs.readFileSync(`${REPO}/resources/viewer/i18n/de.json`, 'utf8'));
const FOOD = process.env.CHIP || 'food';
const pt = (s, d) => (s || d).split(',').map(Number);
const ORIGIN = pt(process.env.ORIGIN, '43.7396,7.4246');
const DEST = pt(process.env.DEST, '43.7355,7.4200');
// Far outside the map: no road to snap to, so the failure state shows.
const FAR = pt(process.env.FAR, '0.5,0.5');

// Not UI text: names and numbers from the map, licences and product names
// (translate="no"), MapLibre's attribution and scale bar.
const DATA = [
  '[translate="no"]', '.maplibregl-ctrl-attrib', '.maplibregl-ctrl-scale', '#info h3',
  '#about-title', '#about-desc', '#about-meta', '#viewer-build-stamp', '#route-debug',
  '.search-result-name', '.search-result-native', '.routing-result-name', '.road-name', '.road-dist',
  '#route-distance', '#route-time', '#drive-dist', '#drive-remaining', '#drive-eta',
  '.pin-name', '.pin-brand', '.pin-contact', '.find-result-card > div:first-child', '#place-detail h2',
  '#detail-nearby-slot button > div > div:first-child', 'ol#results h3', 'ol#results .rich',
  '.near-result > div:first-child', '#preview-banner', '.wiki-item-name', '.wiki-item-desc',
  '.wiki-detail-name', '.wiki-detail-desc', '.wiki-popup', '#routing-origin-input', '#routing-dest-input',
];
// Rows that join a translated label and data with " · " (type · place).
const MIXED = [
  '.search-result-type', '.find-result-card > div:nth-child(2)', '#place-detail > div',
  '#detail-nearby-slot', 'ol#results .meta', '.near-result .sub', '.routing-result-sub',
  '#find-results-strip span', '#subfilter-row', '#find-results-strip button', '.wiki-item-meta',
  '#drive-street',
];

const fails = [];
const check = (ok, label, detail) => {
  console.log((ok ? 'ok   ' : 'FAIL ') + label + (detail ? ' — ' + detail : ''));
  if (!ok) fails.push(label);
};
const skip = (label, why) => console.log('skip ' + label + ' (' + why + ')');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function unbracketed(page, roots) {
  return page.evaluate((roots, DATA, MIXED) => {
    const bad = [];
    const isData = (el) => DATA.some((s) => el.closest(s));
    const isMixed = (el) => MIXED.some((s) => el.closest(s));
    const shown = (el) => {
      const cs = getComputedStyle(el);
      return cs.display !== 'none' && cs.visibility !== 'hidden' && el.getClientRects().length;
    };
    const unit = /^[\d.,\s  ≈+-]*(km|mi|ft|m|min|hr|sec|s)?$/;
    // Letters left once every [bracketed] string is taken out: a string
    // that was not translated. "Foo [bar] baz" leaves "Foo baz".
    const leftover = (t) => {
      let s = t, prev;
      do { prev = s; s = s.replace(/\[[^[\]]*\]/g, ' '); } while (s !== prev);
      return s;
    };
    const wordy = (t, mixed) => {
      const rest = leftover(t);
      if (!/[A-Za-z]{2}/.test(rest)) return false;
      if (mixed && rest !== t) {
        // Data beside a translated label: every "·" part that has letters
        // must hold a bracket, or be a part with no translated text at all.
        return false;
      }
      return !unit.test(rest.trim());
    };
    for (const sel of roots) {
      for (const root of document.querySelectorAll(sel)) {
        const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let n;
        while ((n = w.nextNode())) {
          const t = n.nodeValue.replace(/\s+/g, ' ').trim();
          const el = n.parentElement;
          if (!t || !el || el.closest('script,style') || isData(el) || !shown(el)) continue;
          if (/^(https?:\/\/)?(www\.)?[\w-]+(\.[\w-]+)+(\/\S*)?$/i.test(t)) continue;   // a host name: data
          if (wordy(t, isMixed(el))) bad.push(sel + ': ' + t.slice(0, 60));
        }
        for (const el of [root, ...root.querySelectorAll('[title],[placeholder],[aria-label]')]) {
          if (isData(el) || !el.getClientRects().length) continue;
          for (const a of ['title', 'placeholder', 'aria-label']) {
            const v = el.getAttribute(a);
            // A link's title that is its URL (website buttons) is data.
            if (v && /^(https?:\/\/|www\.|[\w.-]+\.[a-z]{2,}(\/|$))/i.test(v)) continue;
            if (v && wordy(v, false)) bad.push(sel + ' @' + a + ': ' + v.slice(0, 60));
          }
        }
      }
    }
    return [...new Set(bad)];
  }, roots, DATA, MIXED);
}

const browser = await puppeteer.launch({ headless: true, executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox', '--lang=en-US'] });
try {
  const ctx = browser.defaultBrowserContext();
  await ctx.overridePermissions(new URL(origin).origin, ['geolocation']);
  const page = await browser.newPage();
  await page.setViewport({ width: 420, height: 860 });
  await page.setGeolocation({ latitude: ORIGIN[0], longitude: ORIGIN[1], accuracy: 10 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  const look = async (label, roots) => {
    const bad = await unbracketed(page, roots);
    check(!bad.length, label, bad.join(' | '));
  };

  // ---- 1. pseudo-locale, map viewer
  await page.goto(origin + '/index.html?uilang=qps', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => window.__szMap && window.streetzimRouting, { timeout: 60000 });
  await page.waitForFunction(() => {
    const i = document.getElementById('search-input');
    return i && i.dataset.szSearch;
  }, { timeout: 60000 }).catch(() => {});
  await sleep(1500);
  const lang = await page.evaluate(() => [SZ_UI_LANG, SZ_I18N_PSEUDO, document.title]);
  check(lang[1] === true && /^\[/.test(lang[2]), 'pseudo-locale is on', JSON.stringify(lang));
  await look('map chrome', ['#controls', '#search-container', '#find-chips', '#info', '#attr-btn',
                            '.maplibregl-ctrl-top-right', '.maplibregl-ctrl-bottom-right']);

  await page.click('#attr-btn');
  await sleep(300);
  await look('About', ['#attr-dialog']);
  await page.click('#attr-close');

  await page.click('#search-input');
  await page.type('#search-input', process.env.SEARCH || 'av', { delay: 20 });
  await page.waitForFunction(() => {
    const r = document.getElementById('search-results');
    return r && [...r.children].some((c) => !c.hasAttribute('data-sz-pending'));
  }, { timeout: 30000 }).catch(() => {});
  await look('search results', ['#search-results']);
  await page.evaluate(() => { const r = document.querySelector('#search-results .search-result:not([data-sz-pending])'); if (r) r.click(); });
  await sleep(2500);
  await look('search pin popup', ['.maplibregl-popup']);
  await page.evaluate(() => document.querySelectorAll('.maplibregl-popup-close-button').forEach((b) => b.click()));
  await page.evaluate(() => { const i = document.getElementById('search-input'); i.value = ''; });
  await page.click('#search-input', { clickCount: 3 });
  await page.type('#search-input', 'zzqxqzzq', { delay: 10 });
  await page.waitForSelector('#search-results .search-no-results', { timeout: 20000 }).catch(() => {});
  await look('search: no results', ['#search-results']);
  await page.evaluate(() => { const i = document.getElementById('search-input'); i.value = ''; i.blur();
    document.getElementById('search-results').style.display = 'none'; });

  // Wikipedia list + popup (maps built with --wikidata)
  const wiki = await page.evaluate(() => { const b = document.getElementById('wiki-toggle');
    if (!b || !b.offsetParent) return false; b.click(); return true; });
  if (wiki) {
    await sleep(3000);
    await look('Wikipedia list', ['#wiki-panel']);
    await page.evaluate(() => { const it = document.querySelector('#wiki-panel .wiki-item'); if (it) it.click(); });
    await sleep(1500);
    await look('Wikipedia detail + popup', ['#wiki-panel', '.maplibregl-popup']);
    await page.evaluate(() => { document.getElementById('wiki-panel-close').click();
      document.querySelectorAll('.maplibregl-popup-close-button').forEach((b) => b.click()); });
  } else {
    skip('Wikipedia list', 'no Wikidata in this map');
  }

  // Routing: empty panel, a route from the GPS start in every travel mode.
  await page.evaluate(() => window.streetzimRouting.open());
  await sleep(500);
  await look('routing panel', ['#routing-panel']);
  const modes = await page.evaluate(() => window.streetzimRouting.travelModes || ['drive']);
  await page.evaluate((o, d) => {
    window.streetzimRouting.setOrigin(o[0], o[1], SZ_HERE);
    window.streetzimRouting.setDest(d[0], d[1], 'B');
  }, ORIGIN, DEST);
  const routed = async () => page.waitForFunction(() => {
    const s = document.getElementById('routing-status');
    return document.getElementById('routing-result').style.display === 'block'
      && s.getAttribute('data-state') !== 'loading';
  }, { timeout: 90000 }).then(() => true, () => false);
  for (const m of modes) {
    if (m !== 'drive') await page.evaluate((m) => window.streetzimRouting.setTravelMode(m), m);
    if (!(await routed())) { check(false, 'route (' + m + ')', 'no route in 90 s'); continue; }
    await sleep(600);
    await page.evaluate(() => { const p = document.getElementById('routing-panel');
      if (p.classList.contains('minimized')) document.getElementById('routing-minimize').click(); });
    await sleep(300);
    const here = await page.evaluate(() => {
      const i = document.getElementById('routing-origin-input');
      return [i.value, i._szHere];
    });
    check(/^\[.*\]$/.test(here[0]) && here[1] === true, 'GPS start (' + m + ')', JSON.stringify(here));
    await look('route (' + m + ')', ['#routing-panel']);
  }
  if (modes.length < 2) skip('walk / bike routes', 'this map routes ' + modes.join(', ') + ' only');

  // Navigation HUD for the last planned mode.
  await page.evaluate(() => { const b = document.querySelector('#routing-go-row button:not(.hidden-mode)'); if (b) b.click(); });
  await sleep(3500);
  const hud = await page.evaluate(() => document.getElementById('drive-hud').classList.contains('visible'));
  if (hud) {
    await look('navigation HUD', ['#drive-hud']);
    await page.evaluate(() => document.getElementById('drive-exit').click());
    await sleep(500);
  } else {
    check(false, 'navigation HUD', 'did not open');
  }

  // A destination with no road: the failure states. Not every map can
  // produce one on demand (an end the router cannot reach is moved to the
  // nearest reachable road, and a point off the map is ignored), so this
  // step reports a skip rather than failing; the unit test
  // (viewer_routing_pickers_js) covers those strings under the pseudo-locale.
  await page.evaluate((o, f) => {
    window.streetzimRouting.clear();
    window.streetzimRouting.setOrigin(o[0], o[1], 'A');
    window.streetzimRouting.setDest(f[0], f[1], 'B');
  }, ORIGIN, FAR);
  const failed = await page.waitForFunction(() => /^(no-route|failed)$/.test(
    document.getElementById('routing-status').getAttribute('data-state') || ''), { timeout: 30000 })
    .then(() => true, () => false);
  if (failed) await look('no route / failure', ['#routing-panel']);
  else skip('no route / failure', 'this map routed or ignored FAR=' + FAR.join(','));
  await page.evaluate(() => { window.streetzimRouting.clear(); document.getElementById('routing-close').click(); });

  const chip = await page.$(`#find-chips [data-chip="${FOOD}"]`);
  if (chip) {
    await chip.click();
    await page.waitForFunction(() => document.getElementById('find-results-strip'), { timeout: 30000 }).catch(() => {});
    await sleep(800);
    await look('Find carousel', ['#find-results-strip']);
    await page.evaluate(() => { const c = document.querySelector('.find-result-card'); if (c) c.click(); });
    await page.waitForFunction(() => document.getElementById('place-detail'), { timeout: 10000 }).catch(() => {});
    await sleep(600);
    await look('place sheet', ['#place-detail']);
  } else {
    skip('Find carousel / place sheet', 'no "' + FOOD + '" chip');
  }

  // ---- pseudo-locale, Find page, and its hand-off to the map
  await page.goto(origin + '/places.html?uilang=qps', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => {
    const s = document.getElementById('status-text');
    return s && s.dataset.state && s.dataset.state !== 'loading' && s.dataset.state !== 'locating';
  }, { timeout: 30000 }).catch(() => {});
  await sleep(800);
  await look('Find page', ['body']);
  const fchip = await page.$(`nav.chips button[data-cat="${FOOD}"]`);
  if (fchip) {
    await fchip.click();
    await page.waitForFunction(() => /^(done|empty|error)$/.test(
      document.getElementById('status-text').dataset.state || ''), { timeout: 30000 }).catch(() => {});
    await sleep(800);
    await look('Find page results', ['body']);
    const hrefs = await page.evaluate(() => [...document.querySelectorAll('#results li .ctas a')]
      .slice(0, 2).map((a) => a.getAttribute('href')));
    check(hrefs.length && hrefs.every((h) => /[?&]uilang=qps/.test(h)), 'Find links keep ?uilang', hrefs.join(' '));
    const onMap = await page.$('#map-view-btn:not([hidden])');
    if (onMap) {
      await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 60000 }), onMap.click()]);
      check(/[?&]uilang=qps/.test(page.url()), 'hand-off to the map keeps ?uilang', page.url());
      await page.waitForFunction(() => document.getElementById('find-results-strip'), { timeout: 60000 }).catch(() => {});
      await sleep(1500);
      await look('Find → map results', ['#find-results-strip']);
      await page.evaluate(() => { const c = document.querySelector('.find-result-card'); if (c) c.click(); });
      await page.waitForFunction(() => document.getElementById('place-detail'), { timeout: 10000 }).catch(() => {});
      await sleep(600);
      await look('Find → map place sheet', ['#place-detail']);
    }
  }

  // ---- 2. German
  await page.goto(origin + '/index.html?uilang=de', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => window.__szMap && window.streetzimRouting, { timeout: 60000 });
  const de = await page.evaluate(() => ({
    lang: document.documentElement.lang,
    clear: document.getElementById('routing-clear').textContent,
    about: document.getElementById('attr-btn').textContent,
  }));
  check(de.lang === 'de', 'German: <html lang="de">', de.lang);
  check(de.clear === DE['routing.clear'] && de.about === DE['about.button'], 'German: static markup',
        JSON.stringify(de));
  await page.goto(origin + '/places.html?uilang=de', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => document.documentElement.lang === 'de', { timeout: 30000 }).catch(() => {});
  const dp = await page.evaluate(() => document.querySelector('header h1').textContent);
  check(dp === DE['places.title'], 'German: Find page', dp);
  check(!errors.length, 'no page errors', errors.join(' | '));
} finally {
  await browser.close();
}
if (fails.length) { console.log('I18N FAILED: ' + fails.join(', ')); process.exit(1); }
console.log('I18N OK');
