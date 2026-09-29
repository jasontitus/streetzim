<script>
// Compute base URL for absolute resource paths. Strip `#` fragment and
// `?` query first — deep-links like `index.html#map=16/37.4/-122.1`
// contain `/` inside the fragment, which used to leak into baseUrl and
// produce `.../index.html#map=16/37.4/map-config.json`, corrupting every
// subsequent fetch. Same for `?q=…` used by the Find mini-app.
var baseUrl = (function() {
  var href = window.location.href;
  var hashAt = href.indexOf('#');
  if (hashAt !== -1) href = href.substring(0, hashAt);
  var queryAt = href.indexOf('?');
  if (queryAt !== -1) href = href.substring(0, queryAt);
  return href.substring(0, href.lastIndexOf('/') + 1);
})();

// Optional Q-ID -> Wikipedia-title bridge for full-article links. Hosted at
// /drive/wiki-qid-titles.json (served from the web, NOT the ZIM, so it sits
// one level above the viewer base). When present, wiki popups gain a
// "Read full article" button that opens the in-ZIM wiki-article/<Title> page
// (served by the drive service worker). Absent (e.g. plain Kiwix) -> popups
// just show the wikidata blurb as before. This is a transitional side-load,
// requested only inside the PWA (SZ_ON_DRIVE_PWA); ZIMs with bundled
// articles carry wiki-geo-index.json, which supersedes it.
var WIKI_QID_TITLES = null;   // optional side-load bridge (transitional)
var WIKI_GEO_INDEX = null;    // {title:[lat,lon,type,qid]} from the ZIM's wiki-geo-index.json
var WIKI_GEO_QID = null;      // reverse {qid:title} built from WIKI_GEO_INDEX
var WIKI_GEO_BUCKETS = null;     // Map<1°-cell key, [title, …]>, built lazily
var WIKI_GEO_BUCKETS_SRC = null; // the index object the buckets were built from

function wikiGeoBucketKey(lat, lon) {
  return (Math.floor(lat) + 90) * 1000 + (Math.floor(lon) + 180);
}

// Titles whose point lies inside [W,E]×[S,N]. Uses a lazily built 1°
// bucket grid so a pan/zoom only touches the cells under the viewport
// instead of rescanning every placed article in the ZIM (hundreds of
// thousands on country-scale builds) on every debounced moveend.
function wikiGeoTitlesInBounds(W, E, S, N) {
  if (!WIKI_GEO_INDEX) return [];
  if (WIKI_GEO_BUCKETS_SRC !== WIKI_GEO_INDEX) {
    WIKI_GEO_BUCKETS = new Map();
    for (var t in WIKI_GEO_INDEX) {
      var g = WIKI_GEO_INDEX[t];
      if (!g || typeof g[0] !== 'number' || typeof g[1] !== 'number') continue;
      var k = wikiGeoBucketKey(g[0], g[1]);
      var arr = WIKI_GEO_BUCKETS.get(k);
      if (!arr) WIKI_GEO_BUCKETS.set(k, arr = []);
      arr.push(t);
    }
    WIKI_GEO_BUCKETS_SRC = WIKI_GEO_INDEX;
  }
  var out = [];
  var la0 = Math.max(-90, Math.floor(S)), la1 = Math.min(89, Math.floor(N));
  var lo0 = Math.max(-180, Math.floor(W)), lo1 = Math.min(179, Math.floor(E));
  for (var la = la0; la <= la1; la++) {
    for (var lo = lo0; lo <= lo1; lo++) {
      var b = WIKI_GEO_BUCKETS.get(wikiGeoBucketKey(la, lo));
      if (!b) continue;
      for (var i = 0; i < b.length; i++) {
        var g2 = WIKI_GEO_INDEX[b[i]];
        if (g2[1] < W || g2[1] > E || g2[0] < S || g2[0] > N) continue;
        out.push(b[i]);
      }
    }
  }
  return out;
}
(function loadWikiTitleBridge() {
  // The bridge sits beside the PWA, never in a ZIM: under kiwix-serve this
  // fetch was a 404 on every page load.
  if (typeof SZ_ON_DRIVE_PWA === 'undefined' || !SZ_ON_DRIVE_PWA) return;
  try {
    fetch(baseUrl + '../wiki-qid-titles.json')
      .then(function(r) { return r && r.ok ? r.json() : null; })
      .then(function(m) { if (m && typeof m === 'object') WIKI_QID_TITLES = m; })
      .catch(function() {});
  } catch (e) {}
})();

// Resolve the in-ZIM article path for a place. Returns null UNLESS this ZIM
// actually bundles Wikipedia articles — signalled by the presence of
// wiki-geo-index.json (WIKI_GEO_INDEX). That gate is what makes the viewer
// safe on other ZIMs that have no bundled articles: no broken article links.
// When articles exist, prefer the OSM `wikipedia` tag (`w`, "en:Golden Gate
// Bridge"), else the geo-index reverse map, else the side-load bridge.
function _wikiArticlePath(qid, w) {
  if (!WIKI_GEO_INDEX) return null;
  var t = null;
  if (w) {
    var s = String(w), ci = s.indexOf(':');
    t = (ci >= 2 && ci <= 3 && /^[a-z]+$/i.test(s.slice(0, ci))) ? s.slice(ci + 1) : s;
  } else if (qid) {
    t = (WIKI_GEO_QID && WIKI_GEO_QID[qid])
        || (WIKI_QID_TITLES && WIKI_QID_TITLES[qid]) || null;
  }
  return t ? t.replace(/ /g, '_') : null;
}

// Open a bundled Wikipedia article in a full-screen in-app overlay with a
// "← Back to map" bar, instead of window.open('_blank'). In the Kiwix WebView
// '_blank' replaces the view with the chrome-less article and leaves no way
// back (no close/back button). An iframe overlay keeps the map underneath and
// the back bar is TOP-anchored, so it renders in the Kiwix WebView. Internal
// article links stay inside the iframe; the bar always returns to the map.
function openWikiArticle(path) {
  if (!path) return;
  // Keep '/' literal: a title like Expo_Park/USC_station is stored two
  // levels deep and its images are linked ../../wiki-image/…, which
  // only resolves if the browser sees the real depth (%2F collapses
  // it to one segment).
  var url = baseUrl + 'wiki-article/' + encodeURIComponent(path).replace(/%2F/g, '/');
  // Navigate the whole view to the article as a native ZIM page. We do NOT use
  // an in-page iframe overlay: in the iOS/Kiwix WebView an <iframe> expands to
  // its full content height and escapes any scroll container, so the article —
  // and its chrome — could be scrolled off-screen. A native page navigation
  // gives proper native scrolling and the app's own Back button. First stamp
  // the current map view into the URL hash so Back returns to the same place
  // (applyHash restores `#map=z/lat/lon` on the way back).
  try {
    var mp = window.__szMap;
    if (mp) {
      var c = mp.getCenter();
      var z = Math.round(mp.getZoom() * 100) / 100;
      var h = '#map=' + z + '/' + (Math.round(c.lat * 1e5) / 1e5)
            + '/' + (Math.round(c.lng * 1e5) / 1e5);
      if (history && history.replaceState) {
        history.replaceState(null, '', location.pathname + location.search + h);
      } else {
        location.hash = h;
      }
    }
  } catch (e) {}
  window.location.href = url;
}

// Publish the REAL visible window size as CSS variables. In the Kiwix
// WKWebView the layout viewport is the full device screen (client/scrollHeight
// = 956 on an iPhone 16) while only window.innerHeight = 772 is actually
// visible between the app's chrome (measured with the ruler diagnostic;
// visualViewport agrees) — so percentage/vh layout dropped every
// bottom-anchored control ~184px below the fold. body{height:var(--app-height)}
// plus the fixed sheets' bottom:var(--bottom-inset) anchor the whole UI to
// what is really on screen. On desktop/PWA the inset is 0 and nothing changes.
function setViewportVars() {
  try {
    var de = document.documentElement;
    // iOS Safari: window.innerHeight still spans under the bottom toolbar
    // and the home-indicator band, so anything anchored to bottom:0 sits
    // behind them (the "Directions to here" / "Show in this area" buttons
    // were unreachable on an iPhone). visualViewport is what is really
    // visible; size the app to it and push bottom sheets up by the gap.
    var vv = window.visualViewport;
    // A pinch-zoom (iOS ignores user-scalable=no) shrinks the visual
    // viewport without changing what the layout has to fit; leave the
    // variables alone rather than squeeze --app-height to the zoomed
    // area (docs/mobile-browser-review.md §B5).
    if (vv && vv.scale && Math.abs(vv.scale - 1) > 0.01) return;
    var visible = (vv && vv.height) ? vv.height : window.innerHeight;
    var inset = Math.max(0, de.clientHeight - window.innerHeight);
    if (vv && vv.height) {
      inset = Math.max(inset, window.innerHeight - vv.height - (vv.offsetTop || 0));
    }
    de.style.setProperty('--app-height', Math.round(visible) + 'px');
    de.style.setProperty('--bottom-inset', Math.round(inset) + 'px');
  } catch (e) {}
}
setViewportVars();
window.addEventListener('resize', setViewportVars);
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', setViewportVars);
  window.visualViewport.addEventListener('scroll', setViewportVars);
}
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', setViewportVars);
}
window.addEventListener('orientationchange', function() {
  setTimeout(setViewportVars, 250);
});
window.addEventListener('load', function() { setTimeout(setViewportVars, 300); });

