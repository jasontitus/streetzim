// Read map config from embedded JSON (with retry — on Windows kiwix-desktop
// the first fetch after ZIM load can occasionally fail before the internal
// server is ready).
function fetchConfig(n) {
  var url = baseUrl + 'map-config.json';
  dbg('fetch map-config.json', 'attempt', n, url);
  return fetch(url)
    .then(function(r) {
      dbg('map-config.json response', r.status, r.statusText, 'type=' + r.type);
      if (!r.ok) {
        var e = new Error('HTTP ' + r.status + ' ' + r.statusText);
        e.status = r.status;
        // Set by the PWA's service worker when the streamed source, not
        // the ZIM, failed (docs/online-preview.md).
        e.upstream = r.headers.get('X-Streetzim-Upstream');
        throw e;
      }
      return r.json();
    })
    .catch(function(err) {
      dbg('map-config.json fetch failed', 'attempt', n, describeError(err));
      // 404 (no ZIM), 500 (a ZIM that does not open) and the worker's
      // "No ZIM loaded" 503 are definitive; only network hiccups and the
      // upstream 503 (already retried for ~8 s by the worker) get more.
      var definitive = err && (err.status === 404 || err.status === 500 || err.status === 502 ||
                               (err.status === 503 && !err.upstream));
      if (n < 4 && !definitive) {
        return new Promise(function(resolve) {
          setTimeout(function() { resolve(fetchConfig(n + 1)); }, 400 * n);
        });
      }
      // Re-throw with URL attached so the outer catch can show it.
      err._url = url;
      throw err;
    });
}
// 025-home-about-fatal.html has already replaced the page when the browser
// lacks what the viewer needs (Fetch, Promise, MapLibre).
if (!window.__szUnsupported) fetchConfig(1)
  .then(function(config) {
    // Check WebGL support before initializing MapLibre
    try {
      var canvas = document.createElement('canvas');
      var gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
      if (!gl) throw new Error('WebGL not available');
    } catch (e) {
      szFatalPage({
        title: 'This map needs WebGL',
        lines: ['The map is drawn with WebGL, which this app or browser has turned off or does not support.'],
        tips: ['Turn on hardware acceleration / WebGL in the browser or app settings, or update it.',
               'Try another reader: a current Kiwix app, Kiwix JS, or a recent desktop browser.'],
        details: describeError(e) + '\nUser agent: ' + (navigator.userAgent || '?')
      });
      return;
    }

    // Opening camera comes straight from map-config.json. Its centre is the
    // bbox centre, which for some regions is empty water or desert -- hawaii
    // opens on open Pacific west of the islands. Attempts to improve that
    // were reverted on 2026-09-25; the note further down and
    // docs/todo-opening-view.md record why, so the next attempt does not
    // repeat them. The zoom is already extent-based (create_osm_zim's
    // get_center_and_zoom), so only the centre needs work.
    // The last view of this map, unless the URL names one (140).
    var openCam = _szOpeningCamera(config, location.hash, _szStorage());
    var map = new maplibregl.Map({
      container: 'map',
      style: makeStyle(config),
      center: openCam.center,
      zoom: openCam.zoom,
      bearing: openCam.bearing,
      pitch: openCam.pitch,
      minZoom: config.minZoom || 0,
      maxZoom: 20,
      attributionControl: true,
      maxBounds: config.bounds ? [
        [config.bounds[0] - 0.01, config.bounds[1] - 0.01],
        [config.bounds[2] + 0.01, config.bounds[3] + 0.01]
      ] : undefined
    });
    // Exposed so module-scope helpers (e.g. openWikiArticle) can stamp the
    // current view into the URL hash before navigating away.
    window.__szMap = map;
    // POI icons are drawn on demand, the theme follows the OS scheme, and
    // the RTL plugin loads when the first RTL label appears (135-137).
    initPoiIcons(map);
    initMapTheme(map, config);
    initRtlText(map, config);

    // NOTE: the opening view is deliberately left as map-config.json ships
    // it. See docs/todo-opening-view.md.
    //
    // Two attempts were made on 2026-09-25 and both were reverted:
    //
    //  1. fitBounds(config.bounds) to frame the whole region. Cannot work as
    //     written: maxBounds is set from the same bounds, and MapLibre's
    //     constraint zooms back IN to fill the viewport, so the fit is always
    //     undone. Measured on a phone viewport, the US showed 30% of its
    //     bbox width, hawaii 25%. Framing needs maxBounds widened to the
    //     fitted view first.
    //  2. A rescue that jumped to a city when the opening view rendered
    //     almost nothing. It works where places are already in loaded tiles
    //     (hawaii -> Honolulu, 1270 features) but not where they are not:
    //     over the mid-Pacific querySourceFeatures returns no candidates at
    //     the opening zoom, and a widen-and-retry still found none because
    //     the post-jump idle can fire before the wider tiles arrive.
    //
    // The original diagnosis was also wrong in one respect worth recording:
    // the opening ZOOM is not fixed at 6. create_osm_zim.get_center_and_zoom
    // buckets it by extent -- 4 for the US, 7 for the baltics, 11 for
    // washington-dc. Only the CENTRE is wrong, and only for regions whose
    // bbox centre is empty.

    // iOS/Kiwix WebView body-scroll lock. CSS (overflow:hidden, position:fixed,
    // 100dvh) does NOT stop that WebView from dragging the whole page, so block
    // document-level touch scrolling in JS. We allow it only inside genuinely
    // scrollable regions (a panel list that overflows) and on the map (maplibre
    // pans via JS — preventing the browser default doesn't stop it). Pinch
    // (2+ touches) is always allowed so map zoom keeps working.
    (function lockBodyScroll() {
      function scrollableAllowed(node) {
        var el = node;
        while (el && el !== document.body && el !== document.documentElement) {
          if (el.classList && (el.classList.contains('maplibregl-canvas')
              || el.classList.contains('maplibregl-map')
              || el.classList.contains('maplibregl-control-container'))) return true;
          if (el.nodeType === 1) {
            var st = null;
            try { st = window.getComputedStyle(el); } catch (e) {}
            if (st) {
              if ((st.overflowY === 'auto' || st.overflowY === 'scroll')
                  && el.scrollHeight > el.clientHeight + 1) return true;
              // Allow HORIZONTAL scrollers too — the find-results card strip
              // is an overflow-x carousel; without this its swipe gesture was
              // preventDefaulted (taps worked, swiping didn't).
              if ((st.overflowX === 'auto' || st.overflowX === 'scroll')
                  && el.scrollWidth > el.clientWidth + 1) return true;
            }
          }
          el = el.parentNode;
        }
        return false;
      }
      document.addEventListener('touchmove', function(e) {
        if (e.touches && e.touches.length > 1) return;     // pinch — let map zoom
        if (!scrollableAllowed(e.target) && e.cancelable) e.preventDefault();
      }, { passive: false });
    })();

    map.addControl(new maplibregl.NavigationControl(), 'top-right');
    initHomeButton(map, config);
    initViewMemory(map, config);
    initAbout(config);
    // Scale bar with mi/km toggle — click to switch units
    var scaleUnit = 'imperial';
    map._streetzimUnit = scaleUnit;  // shared with driving-mode HUD
    var scaleControl = new maplibregl.ScaleControl({ unit: scaleUnit });
    map.addControl(scaleControl, 'bottom-left');
    document.addEventListener('click', function(e) {
      if (e.target.closest('.maplibregl-ctrl-scale')) {
        scaleUnit = scaleUnit === 'imperial' ? 'metric' : 'imperial';
        scaleControl.setUnit(scaleUnit);
        map._streetzimUnit = scaleUnit;
        map.fire('streetzim.units', { unit: scaleUnit });
      }
    });
    // Force resize after load — Kiwix WebView may not have final dimensions at init
    map.on('load', function() { map.resize(); });
    window.addEventListener('resize', function() { map.resize(); });
    // "You are here" geolocation dot — placed bottom-right to avoid overlapping layer controls.
    // Cache every fix on `window.__streetzimLastLoc` so "Directions to here"
    // and the routing GPS button can fill the origin without re-prompting.
    // Custom locate control. MapLibre's GeolocateControl hides its button
    // unless navigator.permissions.query({name:'geolocation'}) reports the
    // permission granted — which the Kiwix iOS/macOS WebView never does: its
    // geolocation bridge (app 3.15.0) wires up getCurrentPosition() but not
    // navigator.permissions. So we call getCurrentPosition() directly — the
    // path Kiwix supports and Directions already use — and the button always
    // shows + works (web and Kiwix). Reuses MapLibre's control CSS so it
    // looks identical to the native control + the user-location dot.
    function _onLocated(pos) {
      if (!pos || !pos.coords) return;
      window.__streetzimLastLoc = {
        lat: pos.coords.latitude, lon: pos.coords.longitude, ts: Date.now()
      };
      // Pre-warm the routing cell containing the user's GPS so the first
      // "Directions to here" doesn't pay a cold cell-fetch.
      if (typeof window.__streetzim_prewarmRoutingCells === 'function') {
        window.__streetzim_prewarmRoutingCells([{
          lat: pos.coords.latitude, lon: pos.coords.longitude
        }]);
      }
    }
    // Find-my-location: maplibre's own GeolocateControl, bottom-right (its
    // conventional home). It shows fine in the Kiwix WebView — maplibre does
    // `if (navigator.permissions === undefined) return !!navigator.geolocation`,
    // so it falls back to true there. (The earlier "missing button" was the
    // whole page scrolling, not the control being off-screen; that's handled by
    // the JS scroll lock, so it can live at the bottom again.)
    var geolocate = new maplibregl.GeolocateControl({
      positionOptions: { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
      trackUserLocation: false,
      showUserLocation: true
    });
    map.addControl(geolocate, 'bottom-right');
    geolocate.on('geolocate', function(pos) { _onLocated(pos); });

    // #info, #attr-btn and the scale bar sit above MapLibre's attribution,
    // which wraps to more lines on a phone when the satellite credit joins
    // it: its height is published as --sz-attrib-h for their CSS.
    (function () {
      var attrib = document.querySelector('.maplibregl-ctrl-attrib');
      if (!attrib || typeof ResizeObserver === 'undefined') return;
      new ResizeObserver(function () {
        document.documentElement.style.setProperty('--sz-attrib-h', attrib.offsetHeight + 'px');
      }).observe(attrib);
    })();

    // Satellite layer toggle
    if (config.hasSatellite) {
      var toggleBtn = document.getElementById('layer-toggle');
      toggleBtn.style.display = 'block';
      var satelliteVisible = false;
      var satelliteAdded = false;

      // Layers to hide when satellite is active
      var hiddenInSatellite = [
        'landcover-grass', 'landcover-wood', 'landcover-farmland',
        'landuse-residential', 'landuse-commercial', 'landuse-park',
        'water', 'water-lowzoom', 'waterway', 'building', 'building-outline',
        'boundary-country', 'boundary-state', 'background'
      ];

      function addSatelliteLayer() {
        if (satelliteAdded) return;
        var satExt = config.satelliteFormat || 'webp';
        var satTileSize = config.satelliteTileSize || 256;
        // Use zimtile:// protocol for retry logic on Kiwix service worker
        // EOX requires its credit in the map itself; MapLibre shows a
        // source's attribution while one of its layers is visible.
        var satCredit = _szSatelliteCreditHtml(config);
        map.addSource('satellite', {
          type: 'raster',
          tiles: ['zimtile://' + baseUrl + 'satellite/{z}/{x}/{y}.' + satExt],
          attribution: satCredit,
          tileSize: satTileSize,
          minzoom: 0,
          maxzoom: config.satelliteMaxZoom || 14
        });
        map.addLayer({
          id: 'satellite-layer',
          type: 'raster',
          source: 'satellite',
          paint: { 'raster-opacity': 1 }
        }, map.getStyle().layers[0].id);
        satelliteAdded = true;
      }

      // Current MapLibre fires an `error` *event* (not a throw) when
      // setPaintProperty / setLayoutProperty hits a non-existent layer,
      // so try/catch can't suppress it. Always gate on map.getLayer(id).
      function setLayout(id, k, v) {
        if (map.getLayer(id)) map.setLayoutProperty(id, k, v);
      }
      function setPaint(id, k, v) {
        if (map.getLayer(id)) map.setPaintProperty(id, k, v);
      }

      // Paint values hideSatellite must put back, captured from the live
      // style the first time showSatellite overrides them. The hand-
      // copied tables in hideSatellite drifted from makeStyle (trunk,
      // secondary, path colours, village halo), so one satellite
      // round-trip permanently recoloured the base map.
      var SAT_LABEL_IDS = ['road-label','water-label','place-city','place-town',
                           'place-village','place-suburb','poi-label'];
      // road-track / road-path-paved added 2026-09-21 with the trail styling:
      // omitting them left satellite mode recolouring every road EXCEPT the
      // trails, so brown dashes sat unreadably on aerial imagery.
      var SAT_ROAD_IDS = ['road-motorway','road-trunk','road-primary','road-secondary',
                          'road-tertiary','road-minor','road-path',
                          'road-path-paved','road-track'];
      var satSavedPaint = null;
      function captureSatPaint() {
        if (satSavedPaint) return;
        satSavedPaint = {};
        function save(id, prop) {
          if (!map.getLayer(id)) return;
          try { satSavedPaint[id + '|' + prop] = map.getPaintProperty(id, prop); }
          catch (e) {}
        }
        SAT_LABEL_IDS.forEach(function(id) {
          save(id, 'text-color'); save(id, 'text-halo-color'); save(id, 'text-halo-width');
        });
        SAT_ROAD_IDS.forEach(function(id) { save(id, 'line-color'); });
      }
      function satRestore(id, prop, fallback) {
        var k = id + '|' + prop;
        return (satSavedPaint && Object.prototype.hasOwnProperty.call(satSavedPaint, k))
          ? satSavedPaint[k] : fallback;
      }

      function showSatellite() {
        addSatelliteLayer();
        captureSatPaint();
        setLayout('satellite-layer', 'visibility', 'visible');
        setPaint('satellite-layer', 'raster-opacity', 1);
        hiddenInSatellite.forEach(function(id) {
          setLayout(id, 'visibility', 'none');
        });
        // White text with strong dark outline on all labels
        ['road-label','water-label','place-city','place-town','place-village',
         'place-suburb','poi-label'].forEach(function(id) {
          setPaint(id, 'text-color', '#fff');
          setPaint(id, 'text-halo-color', 'rgba(0,0,0,0.85)');
          setPaint(id, 'text-halo-width', 2);
        });
        setPaint('water-label', 'text-color', '#aad3df');
        ['road-motorway','road-trunk','road-primary','road-secondary',
         'road-tertiary','road-minor','road-path',
         'road-path-paved','road-track'].forEach(function(id) {
          setPaint(id, 'line-color', 'rgba(255,255,255,0.6)');
        });
        ['road-motorway-casing','road-trunk-casing','road-primary-casing',
         'road-secondary-casing'].forEach(function(id) {
          setLayout(id, 'visibility', 'none');
        });
        map.triggerRepaint();
      }

      function hideSatellite() {
        if (satelliteAdded) {
          setLayout('satellite-layer', 'visibility', 'none');
        }
        hiddenInSatellite.forEach(function(id) {
          setLayout(id, 'visibility', 'visible');
        });
        // Restore label colors and halos (with wikidata blue tint).
        // Only includes layer IDs that actually exist in the style —
        // place-hamlet / place-neighbourhood don't, and styling them
        // would fire console errors on every satellite toggle.
        var labelDefaults = {
          'road-label': {c: '#555', hc: 'rgba(255,255,255,0.8)', hw: 1.5},
          'water-label': {c: ['case',['has','wikidata'],'#3a60a0','#5d80b4'], hc: 'rgba(255,255,255,0.7)', hw: 1},
          'place-city': {c: ['case',['has','wikidata'],'#2a4a7a','#333'], hc: 'rgba(255,255,255,0.8)', hw: 1.5},
          'place-town': {c: ['case',['has','wikidata'],'#3a5a8a','#444'], hc: 'rgba(255,255,255,0.7)', hw: 1.2},
          'place-village': {c: ['case',['has','wikidata'],'#3a5a8a','#555'], hc: 'rgba(255,255,255,0.8)', hw: 1.5},
          'place-suburb': {c: ['case',['has','wikidata'],'#4a6fa5','#666'], hc: 'rgba(255,255,255,0.8)', hw: 1},
          'poi-label': {c: ['case',['has','wikidata'],'#4a6fa5','#666'], hc: 'rgba(255,255,255,0.7)', hw: 1}
        };
        // Prefer the values captured from the live style; the table is
        // only a fallback for a layer that wasn't present at capture.
        Object.keys(labelDefaults).forEach(function(id) {
          var d = labelDefaults[id];
          setPaint(id, 'text-color', satRestore(id, 'text-color', d.c));
          setPaint(id, 'text-halo-color', satRestore(id, 'text-halo-color', d.hc));
          setPaint(id, 'text-halo-width', satRestore(id, 'text-halo-width', d.hw));
        });
        ['road-motorway-casing','road-trunk-casing','road-primary-casing',
         'road-secondary-casing'].forEach(function(id) {
          setLayout(id, 'visibility', 'visible');
        });
        var roadColors = {
          'road-motorway': '#ffa35c', 'road-trunk': '#ffd592',
          'road-primary': '#fcd6a4', 'road-secondary': '#f5f5dc',
          'road-tertiary': '#fff', 'road-minor': '#fff', 'road-path': '#a2674a',
          'road-path-paved': '#c9a08a', 'road-track': '#b08858'
        };
        Object.keys(roadColors).forEach(function(id) {
          setPaint(id, 'line-color', satRestore(id, 'line-color', roadColors[id]));
        });
      }

      // The theme repainted the base layers from makeStyle(): what
      // hideSatellite restores is now stale, and while satellite is on its
      // label/road overrides were just overwritten. Recapture, re-apply.
      map.on('streetzim.theme', function() {
        satSavedPaint = null;
        if (satelliteVisible) showSatellite();
      });

      toggleBtn.addEventListener('click', function() {
        satelliteVisible = !satelliteVisible;
        toggleBtn.classList.toggle('active-control', satelliteVisible);
        toggleBtn.textContent = satelliteVisible ? 'Map' : 'Satellite';
        if (satelliteVisible) { showSatellite(); } else { hideSatellite(); }
      });
    }

    // Terrain toggle (3D terrain + hillshade)
    if (config.hasTerrain) {
      var terrainToggle = document.getElementById('terrain-toggle');
      terrainToggle.style.display = 'block';
      var terrainMode = 0; // 0=off, 1=hillshade, 2=3D terrain
      var terrainSourceAdded = false;
      var terrainLabels = ['3D', 'Hillshade', '3D Terrain'];

      function addTerrainSource() {
        if (terrainSourceAdded) return;
        map.addSource('terrain-dem', {
          type: 'raster-dem',
          tiles: ['zimtile://' + baseUrl + 'terrain/{z}/{x}/{y}.webp'],
          tileSize: 256,
          minzoom: 0,
          maxzoom: config.terrainMaxZoom || 12,
          encoding: 'mapbox'
        });
        terrainSourceAdded = true;
      }

      function setTerrainMode(mode) {
        addTerrainSource();
        // Remove existing hillshade layer if present. MapLibre fires
        // an 'error' EVENT (not a throw) for non-existing layers in
        // recent versions, so try/catch isn't enough — explicitly
        // check first to keep the console clean.
        if (map.getLayer('hillshade-layer')) {
          map.removeLayer('hillshade-layer');
        }

        if (mode === 0) {
          // Off
          map.setTerrain(null);
        } else if (mode === 1) {
          // Hillshade only (flat 2D with shading)
          map.setTerrain(null);
          map.addLayer({
            id: 'hillshade-layer',
            type: 'hillshade',
            source: 'terrain-dem',
            paint: {
              'hillshade-exaggeration': 0.4,
              'hillshade-shadow-color': '#473B24',
              'hillshade-highlight-color': '#fff',
              'hillshade-illumination-direction': 315
            }
          }, _hillshadeBeforeId(map));
        } else if (mode === 2) {
          // Full 3D terrain + hillshade
          map.addLayer({
            id: 'hillshade-layer',
            type: 'hillshade',
            source: 'terrain-dem',
            paint: {
              'hillshade-exaggeration': 0.3,
              'hillshade-shadow-color': '#473B24',
              'hillshade-highlight-color': '#fff',
              'hillshade-illumination-direction': 315
            }
          }, _hillshadeBeforeId(map));
          map.setTerrain({ source: 'terrain-dem', exaggeration: 1.5 });
        }
        map.triggerRepaint();
      }

      terrainToggle.addEventListener('click', function() {
        terrainMode = (terrainMode + 1) % 3;
        terrainToggle.textContent = terrainLabels[terrainMode] || '3D';
        terrainToggle.classList.toggle('active-control', terrainMode > 0);
        setTerrainMode(terrainMode);
      });
    }

    // Update info box with area name and build date
    if (config.name) {
      document.title = config.name + ' — Offline Map';
      document.querySelector('#info h3').textContent = config.name;
    }
    if (config.buildDate) {
      document.querySelector('#info p').textContent = 'Map data: ' + config.buildDate;
    }
    // PWA only: a way back to the picker from an installed app, whose
    // standalone launch skips it (docs/mobile-browser-review.md §B5).
    if (/^\/drive\/viewer\/?/.test(location.pathname)) {
      var infoP = document.querySelector('#info p');
      if (infoP) {
        infoP.appendChild(document.createTextNode(' \u00b7 '));
        var changeLink = document.createElement('a');
        changeLink.href = '/drive/?picker=1';
        changeLink.textContent = 'Change map';
        changeLink.className = 'sz-change';
        infoP.appendChild(changeLink);
      }
    }

    // URL-fragment routing. Supports three independent fragments
    // that can mix (`#dest=…&label=…`, `#map=…&dest=…`, …):
    //   map=zoom/lat/lon              ← legacy "show this on the map"
    //   dest=lat,lon                  ← pre-populate routing destination
    //   origin=lat,lon                ← pre-populate routing origin
    //   label=…                       ← optional label for the dest pin
    // Search detail pages (search/<slug>.html) link in via dest=… so
    // the viewer pops the routing panel open with the place already
    // filled in — see "Directions to here" CTA.
    // Out-of-range coordinates make maplibre throw ("Invalid LngLat
    // latitude value") out of the map `load` handler, which used to
    // abort the rest of applyHash (dest/origin/find=results) for a
    // hand-typed or truncated URL. Validate before use.
    function validLatLon(lat, lon) {
      return isFinite(lat) && isFinite(lon) && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180;
    }
    function applyHash() {
      var hash = window.location.hash || '';
      var m = hash.match(/map=(\d+\.?\d*)\/(-?\d+\.?\d*)\/(-?\d+\.?\d*)/);
      if (m) {
        var z = parseFloat(m[1]), lat = parseFloat(m[2]), lon = parseFloat(m[3]);
        if (validLatLon(lat, lon) && isFinite(z) && z >= 0 && z <= 24) {
          map.flyTo({ center: [lon, lat], zoom: z, duration: 1500 });
        } else {
          m = null;
        }
      }
      var destMatch  = hash.match(/dest=(-?\d+\.?\d*),(-?\d+\.?\d*)/);
      var origMatch  = hash.match(/origin=(-?\d+\.?\d*),(-?\d+\.?\d*)/);
      var pinMatch   = hash.match(/pin=(-?\d+\.?\d*),(-?\d+\.?\d*)/);
      var labelMatch = hash.match(/label=([^&]+)/);
      var label = null;
      if (labelMatch) {
        // A malformed escape (hand-typed or truncated URL) threw
        // URIError here and aborted the whole hash handler — no pin,
        // no routing panel — on load and on every hashchange.
        try { label = decodeURIComponent(labelMatch[1]); }
        catch (e) { label = labelMatch[1]; }
      }
      // `pin=` drops a red pin + Directions popup at a location without
      // opening the routing panel. Used by the Find-page "Map" button so
      // the user sees what they picked instead of landing on an
      // unannotated camera move.
      if (pinMatch) {
        var pinLat = parseFloat(pinMatch[1]), pinLon = parseFloat(pinMatch[2]);
        if (validLatLon(pinLat, pinLon)) placeSearchPin(map, pinLat, pinLon, label || '');
      }
      if (destMatch && !validLatLon(parseFloat(destMatch[1]), parseFloat(destMatch[2]))) destMatch = null;
      if (origMatch && !validLatLon(parseFloat(origMatch[1]), parseFloat(origMatch[2]))) origMatch = null;
      if (destMatch) {
        var dLat = parseFloat(destMatch[1]), dLon = parseFloat(destMatch[2]);
        // If we don't already have a `map=` fragment, frame the
        // destination so the user sees what they picked.
        if (!m) map.flyTo({ center: [dLon, dLat], zoom: 16, duration: 1200 });
        // Open the routing panel via the streetzimRouting surface.
        // The legacy path clicked a now-removed `#route-toggle` button,
        // which silently failed after the Find-first rework — breaking
        // the entire deep-link flow.
        var routingApi = window.streetzimRouting;
        if (routingApi && typeof routingApi.open === 'function') {
          routingApi.open();
        }
        if (routingApi && typeof routingApi.setDest === 'function') {
          routingApi.setDest(dLat, dLon, label || '');
        }
        if (origMatch && routingApi && typeof routingApi.setOrigin === 'function') {
          var oLat = parseFloat(origMatch[1]), oLon = parseFloat(origMatch[2]);
          routingApi.setOrigin(oLat, oLon, 'Current location');
          // Stash on the window so the next "Directions to here" popup
          // (or any in-session action) can reuse the fix without asking
          // the browser for a new one.
          window.__streetzimLastLoc = { lat: oLat, lon: oLon, ts: Date.now() };
        }
      }
      // `#find=results` deep-link from places.html — the Find page
      // stashed `state.results` in sessionStorage; render them as
      // pins on the map plus a floating sidebar with click-to-fly.
      if (/find=results/.test(hash)) {
        renderFindResultsFromStash(map);
      }
    }
    // Run applyHash both on `load` and (if the map already finished
    // loading by the time we got here) immediately. Without the
    // immediate fallback a fast PWA cold-start can leave `#pin=…` /
    // `#dest=…` deep-links broken because the listener registered
    // after the event already fired.
    map.on('load', applyHash);
    if (map.loaded && map.loaded()) applyHash();
    window.addEventListener('hashchange', applyHash);

    // Attribution dialog
    var attrBtn = document.getElementById('attr-btn');
    var attrOverlay = document.getElementById('attr-overlay');
    var attrClose = document.getElementById('attr-close');
    attrBtn.addEventListener('click', function() { attrOverlay.style.display = 'block'; });
    attrClose.addEventListener('click', function() { attrOverlay.style.display = 'none'; });
    attrOverlay.addEventListener('click', function(e) {
      if (e.target === attrOverlay) attrOverlay.style.display = 'none';
    });
    // Show the Overture attribution section only for ZIMs that actually
    // ship Overture-derived address data (the flag is set by the builder
    // when --overture-addresses was used). Older ZIMs without Overture
    // content shouldn't credit a dataset they don't include.
    if (config.hasOvertureAddresses) {
      var overtureSection = document.getElementById('attr-overture-section');
      if (overtureSection) overtureSection.style.display = '';
    }
    // Same for the other optional layers: a ZIM without satellite imagery
    // must not show the imagery's non-commercial licence as if it applied.
    [['attr-satellite-section', config.hasSatellite],
     ['attr-terrain-section', config.hasTerrain],
     ['attr-wiki-section', config.hasWikidata || config.hasWikiArticles],
     ['attr-rtl-section', config.rtlTextPlugin]
    ].forEach(function (s) {
      var el = document.getElementById(s[0]);
      if (el && s[1]) el.style.display = '';
    });

    // Initialize search
    initSearch(map);

    // Initialize Wikidata info popups
    initWikidataPopups(map, config);

    // Initialize Wiki sidebar
    initWikiSidebar(map, config);

    // Initialize routing
    initRouting(map, config);

    // On-map Find chip rail — replaces the navigation to places.html
    // for chip-based browsing. Tap a chip → results pin on the map +
    // carousel below.
    initFindChips(map);

    // "Explore" — top-right floating button that opens a chip menu.
    // Each chip links into places.html with that chip preselected and
    // the "Limit to map area" toggle on, so the user goes straight to
    // a constrained category browse. Now mostly redundant with the
    // chip rail above; kept as the entry-point for full-list view.
    initExplore(map);

    // MapLibre does not retry a tile that errored until its source
    // reloads, so a tunnel left holes until the next pan. Point every
    // tiled source at its own URLs again, which reloads it: when the
    // connection comes back, and — the connection never went away for
    // an archive.org 5xx or the proxy's quota — ~30 s after the worker
    // reports the streamed source failing (its Retry-After), once per
    // outage (docs/mobile-browser-review.md §A5).
    function reloadTileSources() {
      try {
        var style = map.getStyle();
        var sources = (style && style.sources) || {};
        Object.keys(sources).forEach(function(id) {
          var src = map.getSource(id);
          var def = sources[id];
          if (src && def && Array.isArray(def.tiles) && typeof src.setTiles === 'function') {
            src.setTiles(def.tiles.slice());
          }
        });
      } catch (e) { dbg('tile source reload failed', describeError(e)); }
    }
    window.addEventListener('online', function() { upstreamReloadDelay = 30000; reloadTileSources(); });
    var upstreamReload = null, upstreamReloadDelay = 30000;
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.addEventListener('message', function(e) {
        var d = e.data;
        // Not for a permanent failure, nor the proxy's daily 429 (that
        // would just poll it); the wait doubles per notice, to 5 min.
        if (!d || d.type !== 'streetzim-upstream' || d.permanent || d.status === 429 || upstreamReload) return;
        upstreamReload = setTimeout(function() { upstreamReload = null; reloadTileSources(); }, upstreamReloadDelay);
        upstreamReloadDelay = Math.min(upstreamReloadDelay * 2, 300000);
      });
      // Without this (or an onmessage assignment) the spec leaves the
      // client's message queue closed; Chromium opens it on
      // addEventListener, WebKit is not promised to.
      if (navigator.serviceWorker.startMessages) navigator.serviceWorker.startMessages();
    }
  })
  .catch(function(err) {
    dbg('fatal error loading map', describeError(err), err && err.stack);
    // map-config.json 404 from the PWA path almost always means the
    // SW has no ZIM loaded — the user navigated to /drive/viewer/
    // directly instead of /drive/. Redirect to the picker instead of
    // dumping a stack trace they can't act on. (Kiwix Desktop and
    // other ZIM-baked hosts always have map-config.json present, so
    // this only triggers in the PWA case.)
    var isPwaPath = /\/drive\/viewer\/?(\?|#|$)/.test(location.pathname + location.search);
    var isConfig = !!(err && err._url && err._url.indexOf('map-config.json') >= 0);
    var status = err && err.status;
    // 404: no ZIM loaded. 503 without the upstream header: the worker has
    // no ZIM either ("No ZIM loaded"). 500: the ZIM it has does not open
    // (a moved file, a corrupt pick). All three are the picker's to sort
    // out — it explains and offers Remove. A 503 *with* the header is the
    // streamed source failing, which a retry can fix: say so instead.
    var isNoZim = isConfig && (status === 404 || status === 500 ||
                               (status === 503 && !err.upstream));
    if (isPwaPath && isNoZim) {
      try { sessionStorage.setItem('streetzim_redirect_reason', status === 500 ? 'zim-error' : 'no-zim'); }
      catch (e) {}
      // ?picker=1: an installed app's picker otherwise forwards straight
      // back here when it believes a ZIM is loaded.
      var pickerUrl = location.pathname.replace(/viewer\/?$/, '') + '?picker=1';
      location.replace(pickerUrl);
      return;
    }
    if (isPwaPath && isConfig && err.upstream) {
      showFatalError(status === 502 ? 'The map source no longer has this file'
                                    : 'The map source is not answering', err, err._url);
      var infoEl = document.getElementById('info');
      if (infoEl) {
        var retry = document.createElement('p');
        retry.className = 'sz-recover';
        // Set the picker href at runtime rather than emitting it in markup
        // (same approach as changeLink above). This branch only runs on the
        // PWA path, so Kiwix never reaches it — but zimcheck scans the static
        // HTML, where a literal "/drive/?picker=1" is an internal link that
        // does not exist inside a ZIM. It failed validate on every swapped
        // ZIM (2026-09-18) even though the code is unreachable there.
        retry.innerHTML = '<a href="#">Try again</a> &middot; <a>Change map</a>';
        retry.lastChild.setAttribute('href', '/drive/?picker=1');
        retry.firstChild.addEventListener('click', function(ev) { ev.preventDefault(); location.reload(); });
        infoEl.appendChild(retry);
      }
      return;
    }
    // map-config.json never arrived: nothing can be drawn, so say so on
    // a full page. A failure after the map exists keeps the small #info
    // note, so a bug in one feature does not hide a working map.
    if (isConfig && !window.__szMap) {
      szFatalPage({
        title: 'This map could not be opened',
        lines: ['Its settings file, map-config.json, could not be read' +
                (status ? ' (the reader answered HTTP ' + status + ')' : '') + '.'],
        tips: ['Try again: a reader that is still opening the file can miss the first requests.',
               'If it keeps failing, the file may be incomplete: check its size, or download it again.',
               'In Kiwix JS, use ServiceWorker mode, not JQuery mode.'],
        retry: true,
        details: describeError(err) + '\nURL: ' + err._url + '\nUser agent: ' +
                 (navigator.userAgent || '?') + '\n\n' + _debugLog.join('\n')
      });
      return;
    }
    showFatalError('Error loading map', err, err && err._url);
  });

