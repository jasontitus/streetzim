// Report every pairwise overlap between visible UI overlays at phone size.
//
// In-ZIM viewer (the gates): ZIM_ORIGIN=<kiwix-serve /content/BOOK URL>.
// The /drive/ online preview: PREVIEW_SITE=<origin serving web/ (scripts/
// serve-web-local.py)> PREVIEW_ZIM=<a same-origin URL of a ZIM under it>
// streams that ZIM through the picker, then measures each UILANG x THEME
// (comma lists: UILANG=en,de,id,ar THEME=light,dark) with the preview
// banner shown, again with the attribution in its other compact state, and
// after the banner's x. SHOW_PREVIEW_BANNER=1: the in-ZIM viewer with the
// banner forced on and filled in (CI has no /drive/ preview). SAFE_BOTTOM=34:
// under a home-indicator inset. INFO_NAME: a longer map name in #info.
// SHOTS=<dir>: a screenshot of each state.
import puppeteer from 'puppeteer';
const ORIGIN = process.env.ZIM_ORIGIN;
const SITE = (process.env.PREVIEW_SITE || '').replace(/\/+$/, '');
const PZIM = process.env.PREVIEW_ZIM || '';
const W = +(process.env.VW || 390), H = +(process.env.VH || 844);
const LANGS = (process.env.UILANG || '').split(',').filter(Boolean);
const THEMES = (process.env.THEME || '').split(',').filter(Boolean);
const BANNER = !!SITE || process.env.SHOW_PREVIEW_BANNER === '1';
const SHOTS = process.env.SHOTS || '';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const b = await puppeteer.launch({ headless: true, executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox','--disable-dev-shm-usage'] });
const found = [];
try {
  const p = await b.newPage();
  await p.setViewport({ width: W, height: H, deviceScaleFactor: +(process.env.DSF || 2), isMobile: true, hasTouch: true });
  const load = async (url) => {
    await p.goto(url, { waitUntil: 'domcontentloaded', timeout: 240000 });
    await p.waitForSelector('canvas.maplibregl-canvas, .maplibregl-canvas', { timeout: 240000 });
  };
  if (SITE) {
    // The picker hands over to /drive/viewer/ once the service worker
    // streams the ZIM; each language and theme then loads the viewer
    // straight away, the worker still holding the URL.
    await p.goto(SITE + '/drive/?zim=' + encodeURIComponent(PZIM), { waitUntil: 'domcontentloaded' });
    await p.waitForFunction(() => /^\/drive\/viewer\//.test(location.pathname), { timeout: 180000 });
  }
  for (const lang of LANGS.length ? LANGS : ['']) for (const theme of THEMES.length ? THEMES : ['']) {
    const q = [lang && 'uilang=' + encodeURIComponent(lang), theme && 'theme=' + encodeURIComponent(theme)]
      .filter(Boolean).join('&');
    await load((SITE ? SITE + '/drive/viewer/' : ORIGIN + '/index.html') + (q ? '?' + q : ''));
    if (lang) {
      await p.waitForFunction((l) => typeof SZ_UI_LANG !== 'undefined' &&
        SZ_UI_LANG === ((typeof SZ_UI_FALLBACK !== 'undefined' && SZ_UI_FALLBACK[l]) || l),
        { timeout: 60000 }, lang);
    }
    if (SITE) await p.waitForSelector('#preview-banner:not([hidden])', { timeout: 60000 });
    await sleep(+(process.env.SETTLE_MS || 10000));
    // SHOW_ALL_CONTROLS=1: show every #controls button (satellite, terrain,
    // wiki) before measuring. Each is display:none unless the ZIM has that
    // feature, so a small test ZIM (CI's Monaco) measures an empty 0 px panel
    // and can't see the panel covering anything. The production gates leave
    // this unset and measure what the ZIM really shows.
    if (process.env.SHOW_ALL_CONTROLS === '1') {
      await p.evaluate(() => document.querySelectorAll('#controls button')
        .forEach(el => { el.style.display = 'block'; }));
      await sleep(300);
    }
    await p.evaluate((force, safe, name) => {
      if (safe) document.documentElement.style.setProperty('--safe-bottom', safe + 'px');
      if (name) document.querySelector('#info h3').textContent = name;
      if (!force) return;
      // What /drive/ writes into it for a streamed Switzerland.
      const set = (id, t) => { const el = document.getElementById(id); if (!el.textContent) el.textContent = t; };
      set('preview-banner-name', 'osm_en_switzerland_2026-10-22.zim');
      set('preview-banner-host', 'archive.org');
      set('preview-banner-bytes', ' · 12.3 MB streamed so far');
      document.getElementById('preview-banner-download').textContent = 'Download (1.1 GB)';
      document.getElementById('preview-banner').hidden = false;
    }, BANNER && !SITE, +(process.env.SAFE_BOTTOM || 0), process.env.INFO_NAME || '');
    await sleep(300);
    for (const state of BANNER ? ['banner', 'banner+attribution', 'dismissed'] : ['']) {
      if (state === 'banner+attribution' || state === 'dismissed') {
        // MapLibre's compact attribution opens and closes on a tap; the
        // reader's screenshots show both.
        await p.evaluate(() => {
          const a = document.querySelector('.maplibregl-ctrl-attrib.maplibregl-compact');
          if (a) a.classList.toggle('maplibregl-compact-show');
        });
      }
      if (state === 'dismissed') await p.click('#preview-banner-close');
      if (state === 'banner+attribution' || state === 'dismissed') await sleep(400);
      const label = [lang, theme, state].filter(Boolean).join(' ');
      if (label) console.log('state', label);
      report(await measure(), label);
      if (SHOTS) await p.screenshot({ path: `${SHOTS}/${W}x${H}${label ? '-' + label.replace(/[ +]/g, '-') : ''}.png` });
    }
  }
  async function measure() {
    return p.evaluate(() => {
      // Measure REAL controls, not MapLibre's positioning wrappers: the
      // .maplibregl-ctrl-bottom-right container spans the full viewport width
      // (390x108) while painting almost nothing, which manufactures fake
      // overlaps. Its children (.maplibregl-ctrl, .maplibregl-ctrl-attrib) are
      // the boxes that actually draw.
      const SEL = ['#search-container','#find-chips','#controls','#info','#attr-btn',
        '#viewer-build-stamp','#wiki-panel','#routing-panel','#search-results',
        '#preview-banner',
        '.maplibregl-ctrl-top-right > .maplibregl-ctrl',
        '.maplibregl-ctrl-top-left > .maplibregl-ctrl',
        '.maplibregl-ctrl-bottom-left > .maplibregl-ctrl',
        '.maplibregl-ctrl-bottom-right > .maplibregl-ctrl',
        '.maplibregl-ctrl-attrib'];
      const items = [];
      const seen = new Set();
      for (const s of SEL) {
        document.querySelectorAll(s).forEach((el, i) => {
          if (seen.has(el)) return;
          seen.add(el);
          const cs = getComputedStyle(el);
          if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return;
          const r = el.getBoundingClientRect();
          if (r.width < 2 || r.height < 2) return;
          if (r.bottom < 0 || r.top > innerHeight) return;
          items.push({ name: s + (i ? '#' + i : ''),
            x: Math.round(r.left), y: Math.round(r.top),
            w: Math.round(r.width), h: Math.round(r.height),
            r: Math.round(r.right), b: Math.round(r.bottom) });
        });
      }
      const overlaps = [];
      for (let i = 0; i < items.length; i++)
        for (let j = i + 1; j < items.length; j++) {
          const a = items[i], c = items[j];
          if (a.name.startsWith('#search-container') && c.name === '#find-chips') continue; // child
          if (c.name.startsWith('#search-container') && a.name === '#find-chips') continue;
          const ox = Math.min(a.r, c.r) - Math.max(a.x, c.x);
          const oy = Math.min(a.b, c.b) - Math.max(a.y, c.y);
          if (ox > 0 && oy > 0) overlaps.push({ a: a.name, b: c.name, ox, oy });
        }
      // While the preview banner shows, it must be all on screen, and its
      // buttons and the bottom controls must take a tap at their centre:
      // the reader saw Download under "Change map".
      const banner = document.getElementById('preview-banner');
      if (banner && !banner.hidden) {
        const br = banner.getBoundingClientRect();
        if (br.left < 0 || br.top < 0 || br.right > innerWidth || br.bottom > innerHeight)
          overlaps.push({ a: '#preview-banner', b: 'the viewport edge', ox: 0, oy: 0 });
        // Its copy must stay inside it: a line that does not fit paints
        // past its edge, over whatever is beside it, with no box of its own
        // to overlap. (Cut parts clip themselves and count as fitting.)
        for (const el of [banner, banner.firstElementChild]) {
          const over = el.scrollWidth - el.clientWidth;
          if (over > 1) overlaps.push({ a: el === banner ? '#preview-banner' : '#preview-banner copy',
            b: 'its own width', ox: over, oy: 0 });
        }
        for (const s of ['#preview-banner-download', '#preview-banner-close', '#attr-btn', '#info .sz-change',
                         '.maplibregl-ctrl-geolocate']) {
          const el = document.querySelector(s);
          if (!el || !el.getClientRects().length) continue;
          const r = el.getBoundingClientRect();
          const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          if (hit && !el.contains(hit))
            overlaps.push({ a: s, b: 'whatever covers its centre: ' + (hit.id ? '#' + hit.id : hit.tagName.toLowerCase() +
              (typeof hit.className === 'string' && hit.className ? '.' + hit.className.split(/\s+/)[0] : '')), ox: 0, oy: 0 });
        }
      }
      return { viewport: { w: innerWidth, h: innerHeight }, items, overlaps };
    });
  }
  function report(res, label) {
    console.log('viewport', JSON.stringify(res.viewport));
    for (const it of res.items)
      console.log(`  ${it.name.padEnd(30)} x=${String(it.x).padStart(4)} y=${String(it.y).padStart(4)} w=${String(it.w).padStart(4)} h=${String(it.h).padStart(4)} right=${it.r} bottom=${it.b}`);
    for (const o of res.overlaps) found.push({ ...o, label });
  }
  console.log(found.length ? 'OVERLAPS:' : 'NO OVERLAPS');
  for (const o of found)
    console.log(`  ${o.label ? '[' + o.label + '] ' : ''}${o.a}  x  ${o.b}   overlap ${o.ox}x${o.oy}px`);
} finally { await b.close(); }
console.log('OVERLAP-CHECK-DONE');
