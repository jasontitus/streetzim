// Right-to-left labels (Arabic, Hebrew, ...). MapLibre shapes them only with
// the RTL text plugin (@mapbox/mapbox-gl-rtl-text, BSD-2-Clause), which the
// builder writes into the ZIM next to maplibre-gl.js and names in
// map-config.json as `rtlTextPlugin`. Older ZIMs lack both, and in-place
// patching cannot add the file, so without the key this is a no-op and
// labels render as they always have.
//
// Loaded only when needed: MapLibre flips its plugin status to 'requested'
// the first time a tile carries RTL text, and nothing is fetched before
// that, so a region without such labels never reads the ~150 KB file. It is
// fetched with the zimtile retry helper (Kiwix drops requests under load)
// and handed to MapLibre as a blob: URL, because MapLibre imports the plugin
// inside its blob: workers, where a relative or zim: URL may not resolve.
// Once loaded MapLibre re-lays out the tiles already shown.
// BEGIN rtl-text
function initRtlText(map, config, lib) {
  lib = lib || window.maplibregl;
  var file = config && config.rtlTextPlugin;
  if (!file || typeof file !== 'string' || /^[a-z]+:|^\/|\.\./i.test(file)) return false;
  if (!lib || typeof lib.setRTLTextPlugin !== 'function' ||
      typeof lib.getRTLTextPluginStatus !== 'function') return false;
  var started = false;
  function check() {
    if (started || lib.getRTLTextPluginStatus() !== 'requested') return;
    started = true;
    map.off('data', check);
    var fetcher = window.__szFetchWithRetry;
    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var got = fetcher
      ? fetcher(baseUrl + file, ctl ? ctl.signal : { aborted: false })
      : fetch(baseUrl + file).then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.arrayBuffer().then(function(b) { return { data: b }; });
        });
    got.then(function(res) {
      var url = URL.createObjectURL(new Blob([res.data], { type: 'text/javascript' }));
      return lib.setRTLTextPlugin(url, false);
    }).then(function() {
      dbg('rtl text plugin loaded');
    }).catch(function(err) {
      // Labels stay as they were (unshaped); nothing else depends on it.
      dbg('rtl text plugin failed', describeError(err));
    });
  }
  map.on('data', check);
  check();
  return true;
}
// END rtl-text
