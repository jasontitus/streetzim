// --- Wiki sidebar: show Wikipedia entries in current viewport ---
function initWikiSidebar(map, config) {
  if (!config.hasWikidata) return;

  var toggle = document.getElementById('wiki-toggle');
  var panel = document.getElementById('wiki-panel');
  var listEl = document.getElementById('wiki-panel-list');
  var countEl = document.getElementById('wiki-panel-count');
  var loadingEl = document.getElementById('wiki-panel-loading');
  var closeBtn = document.getElementById('wiki-panel-close');
  var refreshBar = document.getElementById('wiki-refresh');
  var refreshBtn = refreshBar.querySelector('button');
  toggle.style.display = 'block';

  var active = false;
  var debounceTimer = null;
  var wdCache = {};
  var wikiPulseMarker = null;
  var pulseTimeout = null;
  var staleView = false; // true after flying to an item (list is stale)

  // Baked geo-index: { title_underscored: [lat, lon, type, qid] } for every
  // placed Wikipedia article. When present (ZIMs built 2026-05-30+) it drives
  // the nearby list + map markers at ANY zoom, replacing the tile-scan path
  // and the side-loaded Q-ID bridge.
  // Load once the ZIM is ready; sets module-scope WIKI_GEO_INDEX (+ reverse
  // qid map) which also gate the "Read full article" links everywhere, so
  // ZIMs without bundled articles never show broken links.
  // Retry, because this used to be one shot with a silent catch. The ZIM is
  // still opening when the viewer boots and the wait scales with its size:
  // silicon-valley (16k entries) is ready in ~5 s, california (825k) takes
  // ~58 s. A single early fetch failed, WIKI_GEO_INDEX stayed null, and the
  // panel then reported "No Wikipedia places in view" — so california looked
  // like it had no Wikipedia data at all when it has 5,367 places.
  var wikiIndexTries = 0;
  function loadWikiGeoIndex() {
    wikiIndexTries++;
    fetch(baseUrl + 'wiki-geo-index.json')
      .then(function(r) { return r && r.ok ? r.json() : null; })
      .then(function(m) {
        if (m && typeof m === 'object') {
          WIKI_GEO_INDEX = m;
          WIKI_GEO_QID = {};
          for (var t in m) { var q = m[t] && m[t][3]; if (q) WIKI_GEO_QID[q] = t; }
          // Fill a panel the user opened while we were still loading.
          if (active) updateSidebarFromGeo();
          return;
        }
        retryWikiGeoIndex();
      })
      .catch(retryWikiGeoIndex);
  }
  function retryWikiGeoIndex() {
    // ~90 s of attempts, which covers the slowest region we ship.
    if (WIKI_GEO_INDEX || wikiIndexTries >= 12) return;
    setTimeout(loadWikiGeoIndex, Math.min(15000, 1000 * wikiIndexTries));
  }
  // A ZIM built without Wikipedia articles says so (hasWikiArticles false)
  // and has no geo-index to ask for; one from before the flag is asked.
  if (config.hasWikiArticles !== false) loadWikiGeoIndex();

  function geoPriority(type) {
    if (type === 'place') return 80;             // settlements (cities/towns)
    if (type === 'water' || type === 'peak') return 55;
    if (type === 'park') return 50;
    if (type === 'poi') return 40;
    return 35;
  }

  // HTML markers (DOM 📖 elements) rather than a MapLibre symbol layer: the
  // ZIM's bundled glyph font has no emoji range, so a symbol text-field of
  // '📖' renders nothing (and 404s the 128000-128255 glyph range). DOM
  // elements use the browser font, so the 📖 shows — same as the UI buttons.
  var _wikiMarkers = [];
  function setWikiMarkers(items) {
    clearWikiMarkers();
    items.forEach(function(it) {
      var el = document.createElement('div');
      el.className = 'wiki-map-marker';
      el.textContent = '📖';
      el.title = it.name;
      el.style.cssText = 'font-size:18px;line-height:1;cursor:pointer;'
        + 'text-shadow:0 0 3px #fff,0 1px 2px rgba(255,255,255,0.95);';
      el.addEventListener('click', function(e) {
        e.stopPropagation(); flyAndDetail(it);
      });
      _wikiMarkers.push(new maplibregl.Marker({ element: el, anchor: 'center' })
        .setLngLat(it.coords).addTo(map));
    });
  }
  function clearWikiMarkers() {
    for (var i = 0; i < _wikiMarkers.length; i++) {
      try { _wikiMarkers[i].remove(); } catch (e) {}
    }
    _wikiMarkers = [];
  }

  // Fly to a geo item, drop a pulse marker, and show its in-panel detail.
  function flyAndDetail(it) {
    // Mark the nearby list stale: we're flying to a specific item, so the
    // moveend handler must show the Refresh bar instead of rebuilding the list
    // (which would wipe the detail we render below and shrink the panel to the
    // few items now in view). The list-item click set this, but marker clicks
    // did not — so a marker tap lost its detail and the panel collapsed.
    staleView = true;
    if (refreshBar) refreshBar.style.display = 'block';
    var lon = it.coords[0], lat = it.coords[1];
    if (wikiPulseMarker) { wikiPulseMarker.remove(); wikiPulseMarker = null; }
    if (pulseTimeout) { clearTimeout(pulseTimeout); pulseTimeout = null; }
    var el = document.createElement('div'); el.className = 'pulse-marker';
    wikiPulseMarker = new maplibregl.Marker({ element: el, anchor: 'center' })
      .setLngLat([lon, lat]).addTo(map);
    var targetZoom = Math.max(map.getZoom(), 14);
    var isMobile = window.innerWidth <= 600;
    if (isMobile && panel.style.display === 'flex') {
      // Lift the marker above the bottom detail sheet with a zoom-INDEPENDENT
      // pixel offset. The old code shifted the center by 20% of the *current*
      // latitude span; when zoomed out to explore a few cities that span is
      // large, so it flew ~0.1-0.2° (many km) SOUTH of the real location.
      var _dy = Math.round((map.getCanvas().clientHeight || 600) * 0.22);
      _szFlyToClear(map, { center: [lon, lat], zoom: targetZoom, duration: 1000, offset: [0, -_dy] });
    } else {
      map.flyTo({ center: [lon, lat], zoom: targetZoom, duration: 1000 });
    }
    pulseTimeout = setTimeout(function() {
      if (wikiPulseMarker) { wikiPulseMarker.remove(); wikiPulseMarker = null; }
    }, 5000);
    // Render the detail immediately from the geo-index's baked description.
    // No wikidata chunk fetch — those are 20-45 MB and made the detail slow to
    // appear (and empty/short until they finished, so the panel never filled
    // out). The "Read full article" button covers the full content.
    renderWikiDetail(it.qid, it.name, [lon, lat], it.title, it.desc);
  }

  // Build the nearby list + markers from the baked geo-index (any zoom).
  function updateSidebarFromGeo() {
    loadingEl.style.display = 'none';
    listEl.textContent = '';
    var b = map.getBounds(), W = b.getWest(), E = b.getEast(), S = b.getSouth(), N = b.getNorth();
    var items = [];
    var titles = wikiGeoTitlesInBounds(W, E, S, N);
    for (var ti = 0; ti < titles.length; ti++) {
      var title = titles[ti];
      var g = WIKI_GEO_INDEX[title];
      var lat = g[0], lon = g[1];
      items.push({ title: title, name: title.replace(/_/g, ' '),
                   qid: g[3] || null, type: g[2] || '', coords: [lon, lat],
                   desc: g[4] || '', priority: geoPriority(g[2] || '') });
    }
    items.sort(function(a, b2) { return b2.priority - a.priority || a.name.localeCompare(b2.name); });
    countEl.textContent = items.length;
    if (items.length > 150) items = items.slice(0, 150);
    setWikiMarkers(items);
    if (!items.length) {
      var empty = createEl('div', 'wiki-empty-state');
      // data-wiki-empty: which empty state, for the smoke gates (the text
      // is translatable): loading / none.
      empty.setAttribute('data-wiki-empty', WIKI_GEO_INDEX ? 'none' : 'loading');
      if (!WIKI_GEO_INDEX) {
        // Don't claim the area is empty when we simply have not loaded the
        // index yet — that misreading is what made california look broken.
        empty.appendChild(createEl('div', 'wiki-empty-state-icon', '⏳'));
        empty.appendChild(createEl('div', 'wiki-empty-state-text', szT('wiki.loading_index', 'Loading Wikipedia index…')));
        countEl.textContent = '';
      } else {
        empty.appendChild(createEl('div', 'wiki-empty-state-icon', '🌍'));
        empty.appendChild(createEl('div', 'wiki-empty-state-text', szT('wiki.none_in_view', 'No Wikipedia places in view — pan or zoom to explore.')));
      }
      listEl.appendChild(empty);
      return;
    }
    // Render rows straight from the geo-index — the short description is now
    // baked into it (g[4]). We no longer prefetch wikidata/<prefix>.json for
    // the whole list: those chunks are region-global 20-45 MB files and a wide
    // "explore" used to Promise.all several at once and OOM mobile WebViews.
    // (Older ZIMs without a baked desc simply show name + type — no fetch.)
    items.forEach(function(it) {
      var desc = it.desc || '';
      var div = createEl('div', 'wiki-item' + (desc ? '' : ' wiki-item-nodesc'));
      div.appendChild(createEl('div', 'wiki-item-name', it.name));
      var meta = createEl('div', 'wiki-item-meta');
      meta.appendChild(createEl('span', 'wiki-item-type', szPlaceType(it.type || 'place')));
      div.appendChild(meta);
      if (desc) div.appendChild(createEl('div', 'wiki-item-desc', desc));
      div.addEventListener('click', function() {
        staleView = true; refreshBar.style.display = 'block'; flyAndDetail(it);
      });
      listEl.appendChild(div);
    });
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
        .then(function(data) { wdCache[prefix] = data; return data; })
        .catch(function() {
          if (n < 3) return new Promise(function(resolve) { setTimeout(function() { resolve(attempt(n + 1)); }, 300 * n); });
          return null;
        });
    }
    return attempt(1);
  }

  function getWdPrefix(qid) {
    var num = qid.substring(1);
    return num.length >= 2 ? num.substring(0, 2) : num + '0';
  }

  function makeTextNode(text) {
    return document.createTextNode(text);
  }

  function createEl(tag, className, text) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (text) el.appendChild(makeTextNode(text));
    return el;
  }

  // Show one entry's detail INSIDE the panel (replacing the list) so the
  // description + "Read full article" button are readable — a map popup
  // here would sit behind the list. A Back link restores the list.
  function renderWikiDetail(qid, name, coords, explicitTitle, bakedDesc) {
    loadingEl.style.display = 'none';
    refreshBar.style.display = 'none';
    countEl.textContent = '';
    listEl.textContent = '';

    // Padded wrapper — the list's horizontal padding comes from .wiki-item,
    // which the detail elements don't use, so add it here (was edge-to-edge
    // on mobile).
    var wrap = document.createElement('div');
    wrap.style.cssText = 'padding:12px 16px;box-sizing:border-box;';
    listEl.appendChild(wrap);

    var back = createEl('button', 'wiki-detail-back', szT('wiki.back_to_list', '← Back to list'));
    back.style.cssText = 'display:block;margin:0 0 12px;padding:6px 10px;border:0;border-radius:6px;background:var(--szd-btn2-bg, #eef2fa);color:var(--szd-btn2-fg, #2a4a7a);font-size:13px;font-weight:600;cursor:pointer;';
    back.addEventListener('click', function() { staleView = false; updateSidebar(); });
    wrap.appendChild(back);

    var nm = createEl('div', 'wiki-detail-name', name);
    // Primary action first, directly under the title. At the bottom of the
    // sheet it sat under iOS Safari's toolbar and could not be tapped.
    if (coords && map._buildDirectionsButton) {
      wrap.appendChild(map._buildDirectionsButton(coords[1], coords[0], name));
    }
    nm.style.cssText = 'font-size:17px;font-weight:700;margin-bottom:6px;color:var(--szd-fg, #1a1a2a);line-height:1.25;word-wrap:break-word;';
    wrap.appendChild(nm);

    // Prefer the description baked into the geo-index (instant, no fetch). Fall
    // back to a wikidata chunk only if a caller already had one loaded.
    // Geo-index entries without a wikidata tag arrive with qid=null —
    // getWdPrefix(null) threw and left the panel blank with no Back.
    var wd = qid ? ((wdCache[getWdPrefix(qid)] || {})[qid] || null) : null;
    var desc = bakedDesc || (wd ? (wd.x || wd.d || '') : '');
    if (desc) {
      var dd = createEl('div', 'wiki-detail-desc', desc);
      dd.style.cssText = 'font-size:13.5px;line-height:1.5;color:var(--szd-fg-2, #444);margin-bottom:10px;word-wrap:break-word;';
      wrap.appendChild(dd);
    }

    var ap = explicitTitle || _wikiArticlePath(qid, null);
    if (ap) {
      var ab = createEl('button', 'wiki-article-btn', szT('popup.read_article', '📖 Read full article'));
      ab.style.cssText = 'display:block;width:100%;margin-bottom:8px;padding:9px 10px;border:0;border-radius:6px;background:#2a4a7a;color:#fff;font-size:14px;font-weight:600;cursor:pointer;box-sizing:border-box;';
      ab.addEventListener('click', function() {
        openWikiArticle(ap);
      });
      wrap.appendChild(ab);
    } else {
      var noa = createEl('div', 'wiki-detail-noarticle', szT('wiki.no_article', 'No bundled article for this place.'));
      noa.style.cssText = 'font-size:12px;color:var(--szd-fg-3, #888);margin-bottom:8px;';
      wrap.appendChild(noa);
    }
  }

  // Collect wiki-tagged features across the viewport from the SOURCE tiles
  // (not just what's rendered) so the list isn't limited by the current
  // zoom's label-collision / layer min-zoom rules. Dedup happens downstream
  // via the qid `seen` map; the existing priority sort puts cities/regions
  // before specific POIs.
  function collectWikiFeatures() {
    var b = map.getBounds();
    var west = b.getWest(), east = b.getEast(),
        south = b.getSouth(), north = b.getNorth();
    var layers = ['place', 'poi', 'water_name', 'mountain_peak',
                  'aerodrome_label', 'transportation_name'];
    var out = [];
    for (var li = 0; li < layers.length; li++) {
      var feats;
      try {
        feats = map.querySourceFeatures('openmaptiles', { sourceLayer: layers[li] });
      } catch (e) { continue; }
      for (var fi = 0; fi < feats.length; fi++) {
        var f = feats[fi], p = f.properties || {};
        if (!p.wikidata || !/^Q\d+$/.test(p.wikidata)) continue;
        var g = f.geometry, cc = g && g.coordinates;
        var pt = null;
        if (cc) {
          if (g.type === 'Point') pt = cc;
          else if (g.type === 'MultiPoint' || g.type === 'LineString') pt = cc[0];
          else if (g.type === 'MultiLineString' || g.type === 'Polygon') pt = cc[0][0];
          else if (g.type === 'MultiPolygon') pt = cc[0][0][0];
        }
        if (!pt) continue;
        if (pt[0] < west || pt[0] > east || pt[1] < south || pt[1] > north) continue;
        out.push(f);
      }
    }
    // Fallback: if the source query came up empty (e.g. source not yet
    // queryable), use rendered features so the panel is never blank.
    return out.length ? out : map.queryRenderedFeatures();
  }

  // Generation counter for the tile-scan path below: successive moveends
  // each fire a Promise.all of chunk fetches, and a slower earlier set
  // could resolve after the newer one and render the wrong viewport
  // (or populate the list after the panel was closed).
  var _wikiScanSeq = 0;

  function updateSidebar() {
    if (!active) return;
    // Prefer the baked geo-index (any-zoom list + markers) when available.
    if (WIKI_GEO_INDEX) { updateSidebarFromGeo(); return; }

    var myScan = ++_wikiScanSeq;
    loadingEl.style.display = 'block';
    listEl.textContent = '';

    var features = collectWikiFeatures();
    var seen = {};
    var items = [];

    for (var i = 0; i < features.length; i++) {
      var props = features[i].properties || {};
      var qid = props.wikidata;
      if (!qid || !qid.match(/^Q\d+$/) || seen[qid]) continue;
      seen[qid] = true;

      var name = szLabelOf(props) || props.label || '';
      if (!name) continue;

      var cls = props['class'] || '';
      var layer = features[i].layer ? features[i].layer.id : '';
      var priority = 0;
      if (cls === 'country') priority = 100;
      else if (cls === 'state') priority = 90;
      else if (cls === 'city') priority = 80;
      else if (cls === 'town') priority = 60;
      else if (layer === 'poi-label') priority = 40;
      else if (layer.indexOf('place') >= 0) priority = 50;
      else priority = 30;

      // Extract a single [lng, lat] point from any geometry type
      var coords = null;
      var geom = features[i].geometry;
      if (geom && geom.coordinates) {
        var c = geom.coordinates;
        if (geom.type === 'Point') {
          coords = c;
        } else if (geom.type === 'MultiPoint') {
          coords = c[0];
        } else if (geom.type === 'LineString') {
          coords = c[Math.floor(c.length / 2)];
        } else if (geom.type === 'MultiLineString') {
          var line = c[0]; coords = line[Math.floor(line.length / 2)];
        } else if (geom.type === 'Polygon') {
          // Centroid of first ring
          var ring = c[0]; var sx = 0, sy = 0;
          for (var ri = 0; ri < ring.length; ri++) { sx += ring[ri][0]; sy += ring[ri][1]; }
          coords = [sx / ring.length, sy / ring.length];
        } else if (geom.type === 'MultiPolygon') {
          var ring = c[0][0]; var sx = 0, sy = 0;
          for (var ri = 0; ri < ring.length; ri++) { sx += ring[ri][0]; sy += ring[ri][1]; }
          coords = [sx / ring.length, sy / ring.length];
        }
      }
      items.push({ qid: qid, name: name, cls: cls, layer: layer, priority: priority, coords: coords });
    }

    items.sort(function(a, b) { return b.priority - a.priority || a.name.localeCompare(b.name); });
    if (items.length > 100) items = items.slice(0, 100);

    if (items.length === 0) {
      loadingEl.style.display = 'none';
      countEl.textContent = '';
      listEl.textContent = '';
      var empty = createEl('div', 'wiki-empty-state');
      empty.setAttribute('data-wiki-empty', 'none');
      empty.appendChild(createEl('div', 'wiki-empty-state-icon', '\uD83C\uDF0D'));
      empty.appendChild(createEl('div', 'wiki-empty-state-text', szT('wiki.none_at_zoom', 'No Wikipedia entries visible at this zoom level. Try zooming in to discover places.')));
      listEl.appendChild(empty);
      return;
    }

    var prefixes = {};
    items.forEach(function(item) { prefixes[getWdPrefix(item.qid)] = true; });

    Promise.all(Object.keys(prefixes).map(fetchWdChunk)).then(function() {
      if (myScan !== _wikiScanSeq || !active) return;  // superseded / closed
      loadingEl.style.display = 'none';
      listEl.textContent = '';

      // Filter out items with no wikidata info at all
      var enriched = items.filter(function(item) {
        var chunk = wdCache[getWdPrefix(item.qid)];
        return chunk && chunk[item.qid];
      });

      countEl.textContent = enriched.length;

      if (enriched.length === 0) {
        var empty = createEl('div', 'wiki-empty-state');
        empty.setAttribute('data-wiki-empty', 'none');
        empty.appendChild(createEl('div', 'wiki-empty-state-icon', '\uD83D\uDCD6'));
        empty.appendChild(createEl('div', 'wiki-empty-state-text', szT('wiki.no_data', 'Features found but no Wikipedia data available for this area.')));
        listEl.appendChild(empty);
        return;
      }

      enriched.forEach(function(item) {
        var chunk = wdCache[getWdPrefix(item.qid)];
        var wd = chunk[item.qid];

        var desc = wd.x || wd.d || '';
        var div = createEl('div', 'wiki-item' + (desc ? '' : ' wiki-item-nodesc'));
        if (item.coords) {
          div.setAttribute('data-lng', item.coords[0]);
          div.setAttribute('data-lat', item.coords[1]);
        }

        div.appendChild(createEl('div', 'wiki-item-name', item.name));

        var metaDiv = createEl('div', 'wiki-item-meta');
        var typeText = item.cls ? szPlaceType(item.cls) : item.layer.replace(/-/g, ' ');
        metaDiv.appendChild(createEl('span', 'wiki-item-type', typeText));
        if (wd.p) {
          var pop = wd.p >= 1e6 ? szT('num.millions', '{n}M', { n: szFixed(wd.p / 1e6, 1) })
                  : wd.p >= 1e3 ? szT('num.thousands', '{n}K', { n: szFixed(wd.p / 1e3, 0) })
                  : (SZ_I18N ? szLocaleNum(wd.p) : String(wd.p));
          metaDiv.appendChild(createEl('span', 'wiki-item-pop', szT('wiki.pop', 'pop. {n}', { n: pop })));
        }
        div.appendChild(metaDiv);

        if (desc) div.appendChild(createEl('div', 'wiki-item-desc', desc));

        var itemQid = item.qid;
        var itemName = item.name;
        div.addEventListener('click', function() {
          var lat = parseFloat(div.getAttribute('data-lat'));
          var lng = parseFloat(div.getAttribute('data-lng'));
          if (!isNaN(lat) && !isNaN(lng)) {
            // Clean up previous
            if (wikiPulseMarker) { wikiPulseMarker.remove(); wikiPulseMarker = null; }
            if (pulseTimeout) { clearTimeout(pulseTimeout); pulseTimeout = null; }
            // Place marker immediately at target
            var el = document.createElement('div');
            el.className = 'pulse-marker';
            wikiPulseMarker = new maplibregl.Marker({ element: el, anchor: 'center' })
              .setLngLat([lng, lat])
              .addTo(map);
            // Mark list as stale — show refresh button instead of auto-updating
            staleView = true;
            refreshBar.style.display = 'block';
            // Fly to target — on mobile, offset so target appears above the bottom sheet
            var targetZoom = Math.max(map.getZoom(), 14);
            var isMobile = window.innerWidth <= 600;
            if (isMobile && panel.style.display === 'flex') {
              // Zoom-independent pixel offset (same fix as flyAndDetail):
              // the old latitude-span shift flew kilometres south of the
              // target when the map was zoomed out.
              var _dy2 = Math.round((map.getCanvas().clientHeight || 600) * 0.22);
              _szFlyToClear(map, { center: [lng, lat], zoom: targetZoom, duration: 1200, offset: [0, -_dy2] });
            } else {
              map.flyTo({ center: [lng, lat], zoom: targetZoom, duration: 1200 });
            }
            // Show the entry detail INSIDE the panel (replacing the list)
            // rather than a map popup the list would overlay. Make sure the
            // wikidata chunk is loaded first so the description renders.
            if (map._wikiSidebarPopup) { map._wikiSidebarPopup.remove(); map._wikiSidebarPopup = null; }
            fetchWdChunk(getWdPrefix(itemQid)).then(function() {
              renderWikiDetail(itemQid, itemName, [lng, lat]);
            });
            // Remove marker after 5 seconds
            pulseTimeout = setTimeout(function() {
              if (wikiPulseMarker) { wikiPulseMarker.remove(); wikiPulseMarker = null; }
            }, 5000);
          }
        });

        listEl.appendChild(div);
      });
    });
  }

  function openPanel() {
    active = true;
    staleView = false;
    refreshBar.style.display = 'none';
    toggle.classList.add('active-control');
    panel.classList.add('wiki-visible');
    panel.style.display = 'flex';
    updateSidebar();
  }

  function closePanel() {
    active = false;
    toggle.classList.remove('active-control');
    panel.classList.remove('wiki-visible');
    panel.style.display = 'none';
    listEl.textContent = '';
    countEl.textContent = '';
    clearWikiMarkers();
    if (wikiPulseMarker) { wikiPulseMarker.remove(); wikiPulseMarker = null; }
  }

  toggle.addEventListener('click', function() {
    if (active) closePanel(); else openPanel();
  });

  closeBtn.addEventListener('click', closePanel);

  // Refresh button — manual refresh after item click navigation
  refreshBtn.addEventListener('click', function() {
    staleView = false;
    refreshBar.style.display = 'none';
    updateSidebar();
  });

  map.on('moveend', function() {
    if (!active) return;
    if (staleView) {
      // After an item click, show the refresh button but don't auto-update
      refreshBar.style.display = 'block';
      return;
    }
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(updateSidebar, 400);
  });
}

