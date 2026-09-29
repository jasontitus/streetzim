// --- Place detail bottom-sheet ---
// Slides up over the carousel and shows the full enrichment for one
// place. Carousel keeps running underneath. Tap close, the backdrop,
// or press Esc to dismiss. Wikipedia extract loads async via the
// existing _fetchWdChunk hook when the place has a Wikidata Q-ID.
function _showPlaceDetail(map, r, idx) {
  if (_findResultsState.detailPanel) _hidePlaceDetail();
  var backdrop = document.createElement('div');
  backdrop.style.cssText = (
    'position:fixed; left:0; right:0; top:0; bottom:0; z-index:1700;'
    + 'background:rgba(0,0,0,0.32); opacity:0;'
    + 'transition:opacity .2s ease;'
  );
  backdrop.addEventListener('click', _hidePlaceDetail);

  var panel = document.createElement('div');
  panel.id = 'place-detail';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Place details');
  panel.style.cssText = (
    'position:fixed; left:0; right:0; bottom:var(--bottom-inset, 0px); z-index:1701;'
    + 'max-height:calc(var(--app-height, 100vh) * 0.78); overflow-y:auto;'
    + 'background:var(--szd-surface, #fff); color:var(--szd-fg, #222);'
    + 'border-top:1px solid var(--szd-line, #ddd);'
    + 'border-radius:18px 18px 0 0;'
    + 'box-shadow:0 -8px 28px rgba(0,0,0,0.28);'
    + 'transform:translateY(100%); transition:transform .25s ease;'
    + 'padding:8px 16px calc(max(20px, min(var(--bottom-inset, 0px), 64px)) + env(safe-area-inset-bottom, 0px));'
    + 'font:14px/1.45 -apple-system,system-ui,sans-serif;'
  );

  // Drag-affordance handle
  var handle = document.createElement('div');
  handle.style.cssText = (
    'width:40px; height:4px; border-radius:2px;'
    + 'background:var(--szd-handle, #d8dadd); margin:6px auto 12px;'
  );
  panel.appendChild(handle);

  // Header row: name + close X
  var header = document.createElement('div');
  header.style.cssText = 'display:flex; align-items:flex-start; gap:10px;';
  var title = document.createElement('h2');
  title.style.cssText = 'margin:0; flex:1 1 auto; font-size:20px;'
    + 'font-weight:700; line-height:1.2;';
  title.textContent = r.n || '(unnamed)';
  header.appendChild(title);
  var close = document.createElement('button');
  close.type = 'button';
  close.title = 'Close';
  close.textContent = '×';
  close.style.cssText = (
    'flex:0 0 auto; background:transparent; border:none;'
    + 'font-size:28px; line-height:1; cursor:pointer; color:var(--szd-fg-3, #666);'
    + 'padding:0 6px; margin-top:-4px;'
  );
  close.addEventListener('click', _hidePlaceDetail);
  header.appendChild(close);
  panel.appendChild(header);

  // Subhead: category / brand / location
  var subParts = [];
  var kind = r.cat || r.s || r.t;
  if (kind) subParts.push(String(kind).replace(/_/g, ' '));
  if (r.brand) subParts.push(r.brand);
  if (r.l) subParts.push(r.l);
  if (subParts.length) {
    var sub = document.createElement('div');
    sub.style.cssText = 'color:var(--szd-fg-3, #666); font-size:13px; margin:4px 0 10px;';
    sub.textContent = subParts.join(' · ');
    panel.appendChild(sub);
  }

  // Distance from current location / origin
  var dline = _detailDistanceLine(r);
  if (dline) panel.appendChild(dline);

  // Rough travel-time estimate (straight-line). Not a real ETA — the
  // routing graph isn't available as a programmatic compute API, so
  // we sub in average-speed heuristics (walking 5 km/h, driving
  // 40 km/h urban average). Labelled "≈" so the user can tell.
  var tline = _detailTravelTimesLine(r);
  if (tline) panel.appendChild(tline);

  // Action row: Directions / Call / Website / (Save placeholder)
  var actions = document.createElement('div');
  actions.style.cssText = (
    'display:flex; gap:8px; margin:14px 0 12px; flex-wrap:wrap;'
  );
  actions.appendChild(_detailActionBtn(
    'Directions', '🧭', /*primary*/true,
    function() { _detailOpenDirections(r); }));
  if (r.p) {
    actions.appendChild(_detailActionBtn(
      'Call', '📞', false,
      function() { window.location.href =
        'tel:' + String(r.p).replace(/\s+/g, ''); }));
  }
  if (r.ws) {
    actions.appendChild(_detailActionBtn(
      'Website', '🌐', false,
      function() { window.open(r.ws, '_blank', 'noopener'); }));
  }
  // Full bundled Wikipedia article (in-ZIM) when this place has one.
  var _detailArt = _wikiArticlePath(r.q, r.w);
  if (_detailArt) {
    actions.appendChild(_detailActionBtn(
      'Wikipedia', '📖', false,
      function() { openWikiArticle(_detailArt); }));
  }
  panel.appendChild(actions);

  // Address line (if location string distinct from subhead)
  // Plus phone/website spelled out for accessibility / copy-paste
  var info = document.createElement('div');
  info.style.cssText = (
    'border-top:1px solid var(--szd-line, #eee); margin-top:6px; padding-top:10px;'
    + 'display:flex; flex-direction:column; gap:6px; font-size:13px;'
  );
  if (r.p) {
    info.appendChild(_detailFactRow('Phone',
      String(r.p), 'tel:' + String(r.p).replace(/\s+/g, '')));
  }
  if (r.ws) {
    info.appendChild(_detailFactRow('Website',
      _detailHostname(r.ws), r.ws, '_blank'));
  }
  if (Array.isArray(r.soc) && r.soc.length) {
    info.appendChild(_detailFactRow('Social',
      r.soc.length + ' link' + (r.soc.length === 1 ? '' : 's'),
      r.soc[0], '_blank'));
  }
  // Coordinates (always available, useful for cross-app copy-paste)
  if (typeof r.a === 'number' && typeof r.o === 'number') {
    var coordRow = _detailFactRow(
      'Coords',
      r.a.toFixed(5) + ', ' + r.o.toFixed(5),
      null);
    info.appendChild(coordRow);
  }
  if (info.childElementCount > 0) panel.appendChild(info);

  // Wikipedia extract — async fetch via the existing wd cache.
  var wdSlot = document.createElement('div');
  wdSlot.style.cssText = 'margin-top:14px;';
  panel.appendChild(wdSlot);
  if (r.wd && map && map._fetchWdChunk && map._getWdPrefix) {
    var qid = String(r.wd);
    if (qid.charAt(0) !== 'Q') qid = 'Q' + qid; // tolerate stripped prefix
    var prefix = map._getWdPrefix(qid);
    map._fetchWdChunk(prefix).then(function(chunk) {
      if (!chunk) return;
      var wd = chunk[qid] || chunk[qid.replace(/^Q/, '')];
      if (!wd) return;
      _populateWikipediaExtract(wdSlot, wd);
    }).catch(function() {});
  }

  // Nearby — up to 5 closest places from the current result set,
  // excluding self. Cheap (no fetch) and immediately useful: lets
  // the user explore "what else is around this restaurant" without
  // closing the panel and flicking the carousel.
  var nearbyRow = document.createElement('div');
  nearbyRow.id = 'detail-nearby-slot';
  nearbyRow.style.cssText = 'margin-top:14px;';
  panel.appendChild(nearbyRow);
  _populateNearby(nearbyRow, map, r, idx);

  document.body.appendChild(backdrop);
  document.body.appendChild(panel);
  _findResultsState.detailPanel = panel;
  _findResultsState.detailBackdrop = backdrop;

  // Two RAFs so the slide-up animation runs from the offscreen state.
  requestAnimationFrame(function() {
    requestAnimationFrame(function() {
      backdrop.style.opacity = '1';
      panel.style.transform = 'translateY(0)';
    });
  });
}

function _hidePlaceDetail(opts) {
  var panel = _findResultsState.detailPanel;
  var backdrop = _findResultsState.detailBackdrop;
  _findResultsState.detailPanel = null;
  _findResultsState.detailBackdrop = null;
  // `instant: true` removes the sheet immediately rather than running
  // the 250 ms slide-down. Callers like _detailOpenDirections want
  // this so the routing panel they're about to open isn't visually
  // hidden behind a slowly-departing bottom sheet.
  var instant = opts && opts.instant;
  if (panel) {
    if (instant) {
      if (panel.parentNode) panel.parentNode.removeChild(panel);
    } else {
      panel.style.transform = 'translateY(100%)';
      setTimeout(function() {
        if (panel.parentNode) panel.parentNode.removeChild(panel);
      }, 250);
    }
  }
  if (backdrop) {
    if (instant) {
      if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
    } else {
      backdrop.style.opacity = '0';
      setTimeout(function() {
        if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
      }, 250);
    }
  }
}

function _detailActionBtn(label, emoji, primary, onClick) {
  var btn = document.createElement('button');
  btn.type = 'button';
  var leadIcon = document.createElement('span');
  leadIcon.setAttribute('aria-hidden', 'true');
  leadIcon.textContent = emoji;
  leadIcon.style.cssText = 'margin-right:6px;';
  var lab = document.createElement('span');
  lab.textContent = label;
  btn.appendChild(leadIcon);
  btn.appendChild(lab);
  btn.style.cssText = (
    'flex:1 1 auto; min-width:88px; padding:10px 14px;'
    + 'border-radius:10px; font:inherit; font-weight:600;'
    + 'cursor:pointer;'
    + 'transition:transform .08s ease, filter .08s ease;'
    + (primary
       ? 'background:#1a73e8; color:#fff; border:1px solid #1a73e8;'
       : 'background:var(--szd-surface, #fff); color:var(--szd-link, #1a73e8);'
         + 'border:1px solid #1a73e8;')
  );
  // Tap feedback: dim + slight scale on press so the user gets
  // instant visual confirmation before the route compute (~1-3 s)
  // makes anything else change. Pointer events cover both mouse and
  // touch in a single listener.
  function press() { btn.style.filter = 'brightness(0.92)'; btn.style.transform = 'scale(0.97)'; }
  function release() { btn.style.filter = ''; btn.style.transform = ''; }
  btn.addEventListener('pointerdown', press);
  btn.addEventListener('pointerup', release);
  btn.addEventListener('pointerleave', release);
  btn.addEventListener('pointercancel', release);
  btn.addEventListener('click', onClick);
  return btn;
}

function _detailFactRow(label, value, href, target) {
  var row = document.createElement('div');
  row.style.cssText = (
    'display:flex; align-items:flex-start; gap:10px;'
  );
  var lbl = document.createElement('span');
  lbl.textContent = label;
  lbl.style.cssText = (
    'flex:0 0 70px; color:var(--szd-fg-3, #888); font-size:12px; padding-top:2px;'
  );
  row.appendChild(lbl);
  var val;
  if (href) {
    val = document.createElement('a');
    val.href = href;
    if (target) { val.target = target; val.rel = 'noopener'; }
  } else {
    val = document.createElement('span');
  }
  val.textContent = value;
  val.style.cssText = (
    'flex:1 1 auto; min-width:0;'
    + 'overflow:hidden; text-overflow:ellipsis; white-space:nowrap;'
    + 'color:var(--szd-link, #1a73e8); text-decoration:none;'
  );
  if (!href) val.style.color = 'var(--szd-fg, #222)';
  row.appendChild(val);
  return row;
}

function _detailHostname(url) {
  try { return new URL(url).hostname.replace(/^www\./, ''); }
  catch (e) { return url; }
}

function _detailTravelTimesLine(r) {
  if (!_findResultsState.origin
      || typeof r.a !== 'number' || typeof r.o !== 'number') {
    return null;
  }
  var o = _findResultsState.origin;
  var m = _haversineMetersStrip(o.lat, o.lon, r.a, r.o);
  // Walking 1.4 m/s (~5 km/h), driving 11 m/s (~40 km/h urban).
  // Round up so 0 doesn't display as "0 min" for non-zero distances.
  function fmtMin(sec) {
    var min = Math.max(1, Math.round(sec / 60));
    if (min < 60) return min + ' min';
    var h = Math.floor(min / 60), rest = min % 60;
    return rest === 0 ? h + ' hr' : h + ' hr ' + rest + ' min';
  }
  var walkMin = fmtMin(m / 1.4);
  var driveMin = fmtMin(m / 11);
  var line = document.createElement('div');
  line.style.cssText = (
    'color:var(--szd-fg-3, #666); font-size:12px; margin:2px 0 0;'
  );
  // Drop walking estimate when it would exceed 2 hours; switch to
  // drive-only display so we're not telling the user to walk for
  // four hours across CA.
  var walkMinValue = m / 1.4 / 60;
  if (walkMinValue > 120) {
    line.textContent = '≈ ' + driveMin + ' drive (straight-line)';
  } else {
    line.textContent = '≈ ' + walkMin + ' walk · ' + driveMin + ' drive (straight-line)';
  }
  return line;
}

function _detailDistanceLine(r) {
  // Show distance from origin (when stash had one) — labelled "From X"
  // so the user sees what we're measuring against. Always-on coords
  // (lat/lng) live in the fact table below; this is just the headline.
  if (!_findResultsState.origin
      || typeof r.a !== 'number' || typeof r.o !== 'number') {
    return null;
  }
  var o = _findResultsState.origin;
  var m = _haversineMetersStrip(o.lat, o.lon, r.a, r.o);
  var line = document.createElement('div');
  line.style.cssText = 'color:var(--szd-fg-2, #444); font-size:13px; margin:4px 0 0;';
  var label = (o.label && o.label !== 'Current location')
    ? o.label : 'current location';
  line.textContent = _formatDistanceStrip(m) + ' from ' + label;
  return line;
}

function _detailOpenDirections(r) {
  if (!window.streetzimRouting || !window.streetzimRouting.open) return;
  // Close the bottom-sheet detail view INSTANTLY (no 250 ms slide-
  // down) so the routing panel underneath becomes visible
  // immediately. Without instant:true the user sees no UI change
  // for ~250 ms after tapping Directions while the sheet animates
  // away — felt like the click did nothing.
  _hidePlaceDetail({ instant: true });
  // Kick off destination-cell prewarm IMMEDIATELY (before opening
  // the panel + waiting on GPS resolution) so by the time A* runs
  // the destination cell is already in cache. Combined with the
  // GPS-cell prewarm shipped earlier, both endpoints' starting
  // cells are warm before the route fires.
  if (typeof window.__streetzim_prewarmRoutingCells === 'function'
      && typeof r.a === 'number' && typeof r.o === 'number') {
    window.__streetzim_prewarmRoutingCells([{ lat: r.a, lon: r.o }]);
  }
  window.streetzimRouting.open();
  window.streetzimRouting.setDest(r.a, r.o, r.n || '');
  var cached = window.__streetzimLastLoc;
  var fresh = cached && (Date.now() - cached.ts) < 10 * 60 * 1000;
  if (fresh && window.streetzimRouting.setOrigin) {
    window.streetzimRouting.setOrigin(cached.lat, cached.lon, 'Current location');
  } else if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(function(pos) {
      window.__streetzimLastLoc = {
        lat: pos.coords.latitude, lon: pos.coords.longitude, ts: Date.now()
      };
      if (window.streetzimRouting.setOrigin) {
        window.streetzimRouting.setOrigin(
          pos.coords.latitude, pos.coords.longitude, 'Current location');
      }
    }, function() {}, { enableHighAccuracy: false, maximumAge: 60000, timeout: 8000 });
  }
}

function _populateWikipediaExtract(slot, wd) {
  if (!wd) return;
  if (wd.d) {
    var desc = document.createElement('div');
    desc.style.cssText = (
      'color:var(--szd-fg-2, #555); font-size:13px; font-style:italic; margin-bottom:8px;'
    );
    desc.textContent = wd.d;
    slot.appendChild(desc);
  }
  if (wd.x) {
    var head = document.createElement('div');
    head.style.cssText = (
      'font-size:11px; color:var(--szd-fg-3, #888); text-transform:uppercase;'
      + 'letter-spacing:0.04em; margin:0 0 4px;'
    );
    head.textContent = 'About';
    slot.appendChild(head);
    var body = document.createElement('div');
    body.style.cssText = 'color:var(--szd-fg, #333); font-size:14px; line-height:1.5;';
    body.textContent = wd.x;
    slot.appendChild(body);
  }
  // Compact facts row
  var facts = [];
  if (wd.p) facts.push(['Pop.', _detailFmtNumber(wd.p)]);
  if (wd.a) facts.push(['Area', wd.a.toLocaleString() + ' km²']);
  if (wd.e) facts.push(['Elev.', wd.e.toLocaleString() + ' m']);
  if (wd.c) facts.push(['Country', wd.c]);
  if (facts.length) {
    var grid = document.createElement('div');
    grid.style.cssText = (
      'margin-top:10px; display:grid; gap:6px 16px;'
      + 'grid-template-columns:repeat(2, minmax(0,1fr));'
      + 'font-size:13px;'
    );
    for (var i = 0; i < facts.length; i++) {
      var k = document.createElement('div');
      k.style.cssText = 'color:var(--szd-fg-3, #888);';
      k.textContent = facts[i][0];
      var v = document.createElement('div');
      v.style.cssText = 'color:var(--szd-fg, #222);';
      v.textContent = facts[i][1];
      grid.appendChild(k); grid.appendChild(v);
    }
    slot.appendChild(grid);
  }
}

function _detailFmtNumber(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

