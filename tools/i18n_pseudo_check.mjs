// The viewer's UI in another language, in a real browser (docs/i18n.md).
//
// 1. Pseudo-locale (?uilang=qps): every translated string shows as [text].
//    Walks the map viewer (controls, About, search, routing panel, a Find
//    chip's carousel and place sheet) and the Find page, and fails on any
//    visible chrome text — text nodes and title / placeholder / aria-label —
//    that has letters but no bracket: a string that never went through szT.
//    Data (place and road names, distances, licences, the map's own title)
//    is skipped by selector, below.
// 2. German (?uilang=de): <html lang>, and a few fixed elements read the
//    de.json text.
//
//   ZIM_ORIGIN=http://127.0.0.1:8902/content/<book> CHROME_PATH=... \
//     node tools/i18n_pseudo_check.mjs
//
// The ZIM must carry this viewer (a fresh build, or patch_viewer_inplace).
// Prints "I18N OK" when every check passed; exits 1 otherwise.
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';

const origin = process.env.ZIM_ORIGIN;
if (!origin) { console.error('ZIM_ORIGIN is required'); process.exit(2); }
const REPO = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const DE = JSON.parse(fs.readFileSync(`${REPO}/resources/viewer/i18n/de.json`, 'utf8'));
const FOOD = process.env.CHIP || 'food';

// Not UI text: names and numbers from the map, licences and product names
// (translate="no"), MapLibre's attribution and scale bar.
const DATA = [
  '[translate="no"]', '.maplibregl-ctrl-attrib', '.maplibregl-ctrl-scale', '#info h3',
  '#about-title', '#about-desc', '#about-meta', '#viewer-build-stamp', '#route-debug',
  '.search-result-name', '.search-result-native', '.routing-result-name', '.road-name', '.road-dist',
  '#route-distance', '#route-time', '#drive-dist', '#drive-remaining', '#drive-eta',
  '.pin-name', '.pin-brand', '.find-result-card > div:first-child', '#place-detail h2',
  '#detail-nearby-slot button > div > div:first-child', 'ol#results h3', '.near-result > div:first-child',
  '#preview-banner',
];

const fails = [];
const check = (ok, label, detail) => {
  console.log((ok ? 'ok   ' : 'FAIL ') + label + (detail ? ' — ' + detail : ''));
  if (!ok) fails.push(label);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function unbracketed(page, roots) {
  return page.evaluate((roots, DATA) => {
    const bad = [];
    const skip = (el) => DATA.some((s) => el.closest(s));
    const shown = (el) => {
      const cs = getComputedStyle(el);
      return cs.display !== 'none' && cs.visibility !== 'hidden' && el.getClientRects().length;
    };
    // A distance ("0.3 mi", "250 m") is a number with a unit symbol, not text.
    const wordy = (t) => /[A-Za-z]{2}/.test(t) && !/\[/.test(t)
      && !/^[\d.,\s\u00a0\u202f]+(km|mi|ft|m)$/.test(t);
    for (const sel of roots) {
      for (const root of document.querySelectorAll(sel)) {
        const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let n;
        while ((n = w.nextNode())) {
          const t = n.nodeValue.replace(/\s+/g, ' ').trim();
          const el = n.parentElement;
          if (!t || !el || el.closest('script,style') || skip(el) || !shown(el)) continue;
          if (wordy(t)) bad.push(sel + ': ' + t.slice(0, 60));
        }
        for (const el of [root, ...root.querySelectorAll('[title],[placeholder],[aria-label]')]) {
          if (skip(el) || !el.getClientRects().length) continue;
          for (const a of ['title', 'placeholder', 'aria-label']) {
            const v = el.getAttribute(a);
            if (v && wordy(v)) bad.push(sel + ' @' + a + ': ' + v.slice(0, 60));
          }
        }
      }
    }
    return [...new Set(bad)];
  }, roots, DATA);
}

const browser = await puppeteer.launch({ headless: true, executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox', '--lang=en-US'] });
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 420, height: 860 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));

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
  const chrome = ['#controls', '#search-container', '#find-chips', '#info', '#attr-btn',
                  '.maplibregl-ctrl-top-right', '.maplibregl-ctrl-bottom-right'];
  let bad = await unbracketed(page, chrome);
  check(!bad.length, 'map chrome', bad.join(' | '));

  await page.click('#attr-btn');
  await sleep(300);
  bad = await unbracketed(page, ['#attr-dialog']);
  check(!bad.length, 'About', bad.join(' | '));
  await page.click('#attr-close');

  await page.click('#search-input');
  await page.type('#search-input', process.env.SEARCH || 'a', { delay: 20 });
  await page.type('#search-input', process.env.SEARCH ? '' : 'v', { delay: 20 });
  await page.waitForFunction(() => {
    const r = document.getElementById('search-results');
    return r && [...r.children].some((c) => !c.hasAttribute('data-sz-pending'));
  }, { timeout: 30000 }).catch(() => {});
  bad = await unbracketed(page, ['#search-results']);
  check(!bad.length, 'search results', bad.join(' | '));
  await page.evaluate(() => { const i = document.getElementById('search-input'); i.value = ''; i.blur();
    document.getElementById('search-results').style.display = 'none'; });

  await page.evaluate(() => window.streetzimRouting.open());
  await sleep(500);
  bad = await unbracketed(page, ['#routing-panel']);
  check(!bad.length, 'routing panel', bad.join(' | '));
  await page.evaluate(() => { const c = document.getElementById('routing-close'); if (c) c.click(); });

  const chip = await page.$(`#find-chips [data-chip="${FOOD}"]`);
  if (chip) {
    await chip.click();
    await page.waitForFunction(() => document.getElementById('find-results-strip'), { timeout: 30000 }).catch(() => {});
    await sleep(800);
    bad = await unbracketed(page, ['#find-results-strip']);
    check(!bad.length, 'Find carousel', bad.join(' | '));
    await page.evaluate(() => { const c = document.querySelector('.find-result-card'); if (c) c.click(); });
    await page.waitForFunction(() => document.getElementById('place-detail'), { timeout: 10000 }).catch(() => {});
    await sleep(600);
    bad = await unbracketed(page, ['#place-detail']);
    check(!bad.length, 'place sheet', bad.join(' | '));
  } else {
    console.log('skip  Find carousel / place sheet (no "' + FOOD + '" chip)');
  }

  // ---- pseudo-locale, Find page
  await page.goto(origin + '/places.html?uilang=qps', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => {
    const s = document.getElementById('status-text');
    return s && s.dataset.state && s.dataset.state !== 'loading';
  }, { timeout: 30000 }).catch(() => {});
  await sleep(800);
  bad = await unbracketed(page, ['body']);
  check(!bad.length, 'Find page', bad.join(' | '));

  // ---- 2. German
  await page.goto(origin + '/index.html?uilang=de', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => window.__szMap && window.streetzimRouting, { timeout: 60000 });
  const de = await page.evaluate(() => ({
    lang: document.documentElement.lang,
    clear: document.getElementById('routing-clear').textContent,
    about: document.getElementById('attr-btn').textContent,
    placeholder: document.getElementById('search-input').placeholder,
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
