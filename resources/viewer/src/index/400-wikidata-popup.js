// --- Wikidata info popup on feature click ---
function initWikidataPopups(map, config) {
  if (!config.hasWikidata) return;

  // Cache for loaded Wikidata chunks: prefix -> {Q123: {...}, ...}
  var wdCache = {};
  var currentPopup = null;

  // Layers to query for clickable features (order = priority)
  var clickLayers = [
    'place-city', 'place-town', 'place-village', 'place-hamlet',
    'place-suburb', 'place-country', 'place-state',
    'poi-label', 'park-label', 'water-label',
    'aerodrome-label', 'mountain-peak'
  ];

  // Fallback: query all layers with wikidata attribute
  var allQueryLayers = null;

  function getQueryLayers() {
    if (allQueryLayers) return allQueryLayers;
    // Find which of our target layers actually exist in the style
    var styleLayers = map.getStyle().layers.map(function(l) { return l.id; });
    allQueryLayers = clickLayers.filter(function(id) {
      return styleLayers.indexOf(id) >= 0;
    });
    // If none matched, query all layers
    if (allQueryLayers.length === 0) allQueryLayers = null;
    return allQueryLayers;
  }

  function fetchWdChunk(prefix) {
    if (wdCache[prefix]) return Promise.resolve(wdCache[prefix]);
    var url = baseUrl + 'wikidata/' + prefix + '.json';
    function attempt(n) {
      return fetch(url)
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(data) {
          wdCache[prefix] = data;
          return data;
        })
        .catch(function(err) {
          if (n < 3) {
            return new Promise(function(resolve) {
              setTimeout(function() { resolve(attempt(n + 1)); }, 300 * n);
            });
          }
          return null;
        });
    }
    return attempt(1);
  }

  function getWdPrefix(qid) {
    var num = qid.substring(1); // strip 'Q'
    return num.length >= 2 ? num.substring(0, 2) : num + '0';
  }

  function formatNumber(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return n.toString();
  }

  // Expose popup builder for use by wiki sidebar
  map._buildWdPopup = buildPopupHTML;
  map._wdCache = wdCache;
  map._fetchWdChunk = fetchWdChunk;
  map._getWdPrefix = getWdPrefix;

  function buildPopupHTML(name, wd) {
    var h = '<div style="font-family:-apple-system,system-ui,sans-serif;max-width:280px;font-size:13px;">';
    h += '<div style="font-size:16px;font-weight:600;margin-bottom:4px;">' + escapeHtml(name) + '</div>';

    if (wd.d) {
      h += '<div style="color:var(--szd-fg-2, #555);font-size:12px;margin-bottom:6px;font-style:italic;">' + escapeHtml(wd.d) + '</div>';
    }

    // Info grid
    var facts = [];
    if (wd.p) facts.push(['Population', formatNumber(wd.p)]);
    if (wd.a) facts.push(['Area', wd.a.toLocaleString() + ' km\u00B2']);
    if (wd.e) facts.push(['Elevation', wd.e.toLocaleString() + ' m']);
    if (wd.c) facts.push(['Country', wd.c]);
    if (wd.cap) facts.push(['Capital', wd.cap]);
    if (wd.tz) facts.push(['Timezone', wd.tz]);

    if (facts.length > 0) {
      h += '<table style="border-collapse:collapse;width:100%;margin-bottom:6px;">';
      facts.forEach(function(f) {
        h += '<tr><td style="color:var(--szd-fg-3, #888);font-size:11px;padding:1px 8px 1px 0;white-space:nowrap;">' +
             f[0] + '</td><td style="font-size:12px;padding:1px 0;">' + escapeHtml(String(f[1])) + '</td></tr>';
      });
      h += '</table>';
    }

    if (wd.x) {
      h += '<div style="color:var(--szd-fg-2, #444);font-size:12px;line-height:1.4;border-top:1px solid var(--szd-line, #eee);padding-top:6px;">' +
           escapeHtml(wd.x) + '</div>';
    }

    if (wd.i) {
      h += '<div style="color:var(--szd-fg-3, #999);font-size:10px;margin-top:4px;">' + escapeHtml(wd.i) + '</div>';
    }

    h += '</div>';
    return h;
  }

  function escapeHtml(s) {
    var d = document.createElement('div');
    d.appendChild(document.createTextNode(s));
    return d.innerHTML;
  }

  // Build the "Directions" button used inside wiki/feature popups.
  // Mirrors the placeSearchPin one so the user gets the same flow
  // regardless of how they discovered the destination.
  // NOTE: see project_directions_button_duplicated.md memory.
  // This is the WIKIDATA-FEATURE / FIND-RESULT copy of the
  // Directions button. There's a near-identical sibling at line
  // ~3635 (search-pin path). Any click-handler change here MUST
  // be applied to BOTH copies or only one popup-source gets the
  // new behaviour.
  function buildDirectionsButton(lat, lon, name) {
    var btn = document.createElement('button');
    btn.className = 'pin-directions';
    btn.textContent = 'Directions to here';
    btn.style.marginTop = '8px';
    btn.addEventListener('click', function(ev) {
      if (!window.streetzimRouting ||
          typeof window.streetzimRouting.open !== 'function') {
        return;
      }
      // Close the parent popup BEFORE opening directions — otherwise
      // it lingers above the route asking the user to choose
      // "Directions to here" again. Walk to the maplibregl-popup
      // ancestor and trigger its close button (preserves whatever
      // teardown maplibre wires up internally). Backup: blanket
      // close via map._closeWikidataPopup + clearAllFindMarkerPopups.
      try {
        var popEl = ev && ev.target && ev.target.closest
          ? ev.target.closest('.maplibregl-popup') : null;
        if (popEl) {
          var closer = popEl.querySelector('.maplibregl-popup-close-button');
          if (closer) closer.click();
        }
        if (typeof map._closeWikidataPopup === 'function') {
          map._closeWikidataPopup();
        }
        if (typeof _findResultsState !== 'undefined'
            && _findResultsState.markers) {
          for (var __i = 0; __i < _findResultsState.markers.length; __i++) {
            var __mm = _findResultsState.markers[__i];
            if (!__mm) continue;
            try {
              var __pp = __mm.getPopup();
              if (__pp && __pp.isOpen()) __mm.togglePopup();
            } catch (e2) {}
          }
        }
      } catch (e3) {}
      window.streetzimRouting.open();
      window.streetzimRouting.setDest(lat, lon, name || undefined);
      var cached = window.__streetzimLastLoc;
      var fresh = cached && (Date.now() - cached.ts) < 10 * 60 * 1000;
      if (fresh) {
        window.streetzimRouting.setOrigin(
          cached.lat, cached.lon, 'Current location');
      } else if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(
          function(pos) {
            window.__streetzimLastLoc = {
              lat: pos.coords.latitude, lon: pos.coords.longitude, ts: Date.now()
            };
            window.streetzimRouting.setOrigin(
              pos.coords.latitude, pos.coords.longitude, 'Current location');
          },
          function() {},
          { enableHighAccuracy: false, maximumAge: 60000, timeout: 8000 });
      }
    });
    return btn;
  }

  // Build a popup DOM (rather than HTML string) so we can append an
  // event-bound Directions button. The text content is the same as
  // buildPopupHTML — wrapping HTML in a fresh div keeps that helper
  // useful for the wiki sidebar (which still uses setHTML).
  function buildWikiPopupDOM(name, wd, lngLat, articlePath) {
    var box = document.createElement('div');
    box.className = 'pin-popup wiki-popup';
    if (wd) {
      box.innerHTML = buildPopupHTML(name, wd);
    } else {
      var h = document.createElement('div');
      h.style.cssText = 'font-family:-apple-system,system-ui,sans-serif;font-size:14px;font-weight:600;';
      h.textContent = name;
      box.appendChild(h);
    }
    // Full bundled-article link — opens the in-ZIM wiki-article/<Title> page
    // (served by the drive SW) when this place has one.
    if (articlePath) {
      var ab = document.createElement('button');
      ab.className = 'wiki-article-btn';
      ab.textContent = '📖 Read full article';
      ab.style.cssText = 'display:block;width:100%;margin-top:8px;padding:7px 10px;border:0;border-radius:6px;background:#2a4a7a;color:#fff;font-size:13px;font-weight:600;cursor:pointer;';
      ab.addEventListener('click', function(e) {
        e.stopPropagation();
        openWikiArticle(articlePath);
      });
      box.appendChild(ab);
    }
    box.appendChild(buildDirectionsButton(lngLat.lat, lngLat.lng, name));
    return box;
  }

  // Expose for the wiki sidebar (so its panel-spawned popups can use
  // the same Directions button without duplicating the routing-bridge
  // glue).
  map._buildDirectionsButton = buildDirectionsButton;
  map._buildWikiPopupDOM = buildWikiPopupDOM;

  // Expose a closer so the find-result marker click handler can
  // dismiss the Wikidata feature popup (otherwise the two popup
  // systems accumulate when the user alternates between feature
  // clicks and find-result-marker clicks).
  map._closeWikidataPopup = function() {
    if (currentPopup) { currentPopup.remove(); currentPopup = null; }
  };

  map.on('click', function(e) {
    if (currentPopup) { currentPopup.remove(); currentPopup = null; }
    // Close find-result marker popups too — same reason as above.
    if (typeof _findResultsState !== 'undefined'
        && _findResultsState.markers) {
      for (var __k = 0; __k < _findResultsState.markers.length; __k++) {
        var __mm = _findResultsState.markers[__k];
        if (!__mm) continue;
        try {
          var __p = __mm.getPopup();
          if (__p && __p.isOpen()) __mm.togglePopup();
        } catch (e3) {}
      }
    }

    // Query rendered features at click point
    var queryOpts = {};
    var layers = getQueryLayers();
    if (layers) queryOpts.layers = layers;

    var features = map.queryRenderedFeatures(e.point, queryOpts);
    if (!features || features.length === 0) return;

    // Find the first feature with a wikidata Q-ID in tile properties
    var feat = null;
    var qid = null;
    for (var i = 0; i < features.length; i++) {
      var props = features[i].properties || {};
      if (props.wikidata && props.wikidata.match(/^Q\d+$/)) {
        feat = features[i];
        qid = props.wikidata;
        break;
      }
    }
    if (!qid) return;

    var name = feat.properties['name:latin'] || feat.properties.name || feat.properties.label || qid;
    var prefix = getWdPrefix(qid);

    fetchWdChunk(prefix).then(function(chunk) {
      var wd = (chunk && chunk[qid]) ? chunk[qid] : null;
      currentPopup = new maplibregl.Popup({ maxWidth: '320px' })
        .setLngLat(e.lngLat)
        .setDOMContent(buildWikiPopupDOM(name, wd, e.lngLat,
                                         _wikiArticlePath(qid, null)));
      _szPopupGap(map, currentPopup);
      currentPopup.addTo(map);
    });
  });

  // Change cursor on hover over clickable features
  map.on('mousemove', function(e) {
    var queryOpts = {};
    var layers = getQueryLayers();
    if (layers) queryOpts.layers = layers;
    var features = map.queryRenderedFeatures(e.point, queryOpts);
    var hasWd = features && features.some(function(f) {
      return !!(f.properties || {}).wikidata;
    });
    map.getCanvas().style.cursor = hasWd ? 'pointer' : '';
  });
}

