// Debug logging — enable by appending ?debug=1 to the URL.
// Keeps a ring-buffer of the last 200 log lines so we can render them
// into a visible panel if something goes wrong (consoles are often
// unavailable inside the Kiwix WebView on Windows/iOS).
var DEBUG = /[?&]debug=1/.test(window.location.search);
var _debugLog = [];
function dbg() {
  var args = Array.prototype.slice.call(arguments);
  var line = args.map(function(a) {
    if (a instanceof Error) return (a.name || 'Error') + ': ' + a.message;
    if (typeof a === 'object') {
      try { return JSON.stringify(a); } catch (e) { return String(a); }
    }
    return String(a);
  }).join(' ');
  _debugLog.push('[' + new Date().toISOString().substr(11, 12) + '] ' + line);
  if (_debugLog.length > 200) _debugLog.shift();
  if (window.console && console.log) console.log.apply(console, ['[streetzim]'].concat(args));
}
function describeError(err) {
  if (!err) return 'unknown error';
  var parts = [];
  if (err.name) parts.push(err.name);
  if (err.message) parts.push(err.message);
  if (err.code) parts.push('code=' + err.code);
  return parts.join(': ') || String(err);
}
function showFatalError(title, err, url) {
  var info = document.getElementById('info');
  if (!info) return;
  var html = '<h3>' + title + '</h3>';
  html += '<p style="white-space:normal;word-break:break-word;">' + describeError(err) + '</p>';
  if (url) html += '<p style="font-size:8px;opacity:0.7;word-break:break-all;">URL: ' + url + '</p>';
  html += '<p style="font-size:8px;opacity:0.7;">UA: ' + (navigator.userAgent || '?').substring(0, 80) + '</p>';
  html += '<p style="font-size:8px;opacity:0.7;">Base: ' + baseUrl + '</p>';
  html += '<p style="font-size:9px;"><a href="#" id="debug-show" style="color:var(--szd-link, #2563eb);">Show debug log</a></p>';
  info.innerHTML = html;
  info.style.maxWidth = '420px';
  var link = document.getElementById('debug-show');
  if (link) {
    link.addEventListener('click', function(e) {
      e.preventDefault();
      var pre = document.createElement('pre');
      pre.style.cssText = 'max-height:200px;overflow:auto;font-size:9px;background:var(--szd-surface, #fff);padding:6px;margin-top:6px;border:1px solid var(--szd-line, #ddd);white-space:pre-wrap;word-break:break-all;';
      pre.textContent = _debugLog.join('\n') || '(empty)';
      info.appendChild(pre);
      link.style.display = 'none';
    });
  }
}
dbg('boot', { href: window.location.href, baseUrl: baseUrl, ua: navigator.userAgent });

// Register a custom protocol that fetches with retry logic and concurrency
// limiting. Kiwix Desktop (especially macOS) drops requests when too many
// fire at once — terrain zoom-in triggers 30-50+ parallel tile fetches.
// We queue requests and allow at most MAX_CONCURRENT in-flight at a time.
(function() {
  if (window.__szUnsupported) return;   // 025: no MapLibre/Fetch; page already explains
  var MAX_CONCURRENT = 6;
  var inflight = 0;
  var queue = [];

  function drain() {
    while (queue.length > 0 && inflight < MAX_CONCURRENT) {
      var next = queue.shift();
      inflight++;
      next();
    }
  }

  function fetchWithRetry(url, signal) {
    function attempt(n) {
      return fetch(url, { signal: signal })
        .then(function(r) {
          if (!r.ok) {
            var e = new Error('HTTP ' + r.status + ' ' + r.statusText);
            e.status = r.status;
            e.upstream = !!r.headers.get('X-Streetzim-Upstream');
            throw e;
          }
          return r.arrayBuffer();
        })
        .then(function(buf) {
          return { data: buf };
        })
        .catch(function(err) {
          dbg('tile fetch failed', 'attempt', n, url, describeError(err));
          // A 404 is definitive (tile outside the bbox / beyond the baked
          // satellite zoom): retrying it four times held one of the six
          // fetch slots for ~2.4 s per missing tile and blocked
          // MapLibre's parent-tile fallback, stalling every pan at z13+
          // on regions whose satellite stops at z12.
          if (err && err.status === 404) throw err;
          // The PWA's worker has already retried the streamed source for
          // ~8 s when it answers 503 with X-Streetzim-Upstream (and a 503
          // without it means no ZIM at all); more attempts here would
          // hold a fetch slot for most of a minute per tile.
          if (err && (err.upstream || err.status === 503 || err.status === 502)) throw err;
          // Otherwise (a dropped request, a transient 5xx) back off for
          // ~7.5 s in all — long enough to come out of a tunnel
          // (docs/mobile-browser-review.md §A5).
          if (n < 5 && !signal.aborted) {
            return new Promise(function(resolve) {
              setTimeout(function() { resolve(attempt(n + 1)); },
                         500 * Math.pow(2, n - 1) + Math.random() * 300);
            });
          }
          throw err;
        });
    }
    return attempt(1);
  }
  // Also used for one-off ZIM files outside a style (the RTL text plugin).
  window.__szFetchWithRetry = fetchWithRetry;

  maplibregl.addProtocol('zimtile', function(params, abortController) {
    var url = params.url.replace('zimtile://', '');
    return new Promise(function(resolve, reject) {
      function run() {
        fetchWithRetry(url, abortController.signal)
          .then(resolve)
          .catch(reject)
          .finally(function() { inflight--; drain(); });
      }
      if (inflight < MAX_CONCURRENT) {
        inflight++;
        run();
      } else {
        queue.push(run);
      }
    });
  });
})();

