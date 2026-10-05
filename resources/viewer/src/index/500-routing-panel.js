// --- Routing with A* pathfinding ---
function initRouting(map, config) {
  if (!config.hasRouting) return;

  // The old "Route" button in the right rail was dropped in favor of a
  // single "Find" entry-point — routing is now triggered from the search
  // pin's "Directions" popup or places.html's "Directions" CTA via the
  // streetzimRouting.open() hook. We tolerate the button being absent so
  // legacy layouts (and host containers that inject their own entrypoint)
  // still work.
  var toggleBtn = document.getElementById('route-toggle');
  var panel = document.getElementById('routing-panel');
  var closeBtn = document.getElementById('routing-close');
  var originInput = document.getElementById('routing-origin-input');
  var destInput = document.getElementById('routing-dest-input');
  var originResultsEl = document.getElementById('routing-origin-results');
  var destResultsEl = document.getElementById('routing-dest-results');
  var gpsBtn = document.getElementById('routing-gps-btn');
  var statusEl = document.getElementById('routing-status');
  var resultEl = document.getElementById('routing-result');
  var distEl = document.getElementById('route-distance');
  var timeEl = document.getElementById('route-time');
  var roadsEl = document.getElementById('route-roads');
  var clearBtn = document.getElementById('routing-clear');
  var goRow = document.getElementById('routing-go-row');
  var modeBtns = {
    drive: document.getElementById('routing-drive'),
    walk:  document.getElementById('routing-walk'),
    bike:  document.getElementById('routing-bike')
  };
  var MODE_LABELS = { drive: 'Drive', walk: 'Walk', bike: 'Bike' };
  // Per-mode presets — camera feels right for the modality. `speed`
  // (m/s: walk≈3.1 mph, bike≈10 mph) re-estimates the remaining time
  // only when the route was planned for another mode (older ZIMs plan
  // car routes only); a route planned for the mode uses its own pace.
  // Off-route: a walker moves slowly, so the trigger waits longer before
  // calling them off route (GPS on foot wanders; sidewalks sit metres off
  // the centreline), and at walking pace 50 m is already far.
  var MODE_PRESETS = {
    drive: { pitch: 60, zoom: 17,   speed: null, offM: 60, offMs: 6000 },
    walk:  { pitch: 45, zoom: 18,   speed: 1.4,  offM: 50, offMs: 12000 },
    bike:  { pitch: 55, zoom: 17.5, speed: 4.5,  offM: 60, offMs: 8000 }
  };

  // Travel mode, chosen before routing: the snap and the router both
  // use it (routing-worker.js edgeCostWB). Only the modes the ZIM's graph
  // carries are offered (map-config routingModes); older ZIMs route cars
  // only, keep the picker hidden, and their Walk / Bike buttons start
  // navigation on the car route with a walking / cycling pace, as before.
  var routingModes = ['drive'];
  (config.routingModes || []).forEach(function(m) {
    if ((m === 'walk' || m === 'bike') && routingModes.indexOf(m) < 0) routingModes.push(m);
  });
  var multiModal = routingModes.length > 1;
  var travelMode = 'drive';
  try {
    var savedTravel = localStorage.getItem('streetzim.travelMode');
    if (routingModes.indexOf(savedTravel) >= 0) travelMode = savedTravel;
  } catch (e) {}
  var travelRow = document.getElementById('routing-travel-row');
  var travelBtns = {
    drive: document.getElementById('travel-drive'),
    walk:  document.getElementById('travel-walk'),
    bike:  document.getElementById('travel-bike')
  };
  function syncTravelButtons() {
    Object.keys(travelBtns).forEach(function(m) {
      var b = travelBtns[m];
      if (!b) return;
      b.style.display = routingModes.indexOf(m) >= 0 ? '' : 'none';
      b.classList.toggle('on', m === travelMode);
      b.setAttribute('aria-checked', m === travelMode ? 'true' : 'false');
      b.tabIndex = m === travelMode ? 0 : -1;
    });
  }
  if (travelRow) {
    travelRow.style.display = multiModal ? '' : 'none';
    syncTravelButtons();
    // Radio-group keys: arrows move the choice (and focus) along the row.
    travelRow.addEventListener('keydown', function(e) {
      var step = (e.key === 'ArrowRight' || e.key === 'ArrowDown') ? 1
               : (e.key === 'ArrowLeft' || e.key === 'ArrowUp') ? -1 : 0;
      if (!step) return;
      e.preventDefault();
      var i = routingModes.indexOf(travelMode);
      var next = routingModes[(i + step + routingModes.length) % routingModes.length];
      setTravelMode(next);
      if (travelBtns[next]) travelBtns[next].focus();
    });
  }
  // Bumped whenever a planned navigation start must not happen any more
  // (route cleared, mode or endpoints changed): see switchDriveMode.
  var navPlanSeq = 0;

  var expandHintEl = document.getElementById('routing-expand-hint');
  var EXPAND_HINT = expandHintEl ? expandHintEl.textContent : '';
  function setExpandHint(text) {
    if (expandHintEl) expandHintEl.textContent = text || EXPAND_HINT;
  }

  function resetGoButtons() {
    Object.keys(modeBtns).forEach(function(m) {
      modeBtns[m].classList.remove('active-mode', 'hidden-mode');
      modeBtns[m].textContent = MODE_LABELS[m];
      // The route was planned for one mode: offer navigation in that
      // mode only.
      modeBtns[m].removeAttribute('aria-label');
      if (multiModal) {
        if (m === travelMode) {
          modeBtns[m].textContent = 'Start';
          modeBtns[m].setAttribute('aria-label', 'Start ' + (m === 'walk' ? 'walking'
            : m === 'bike' ? 'cycling' : 'driving') + ' navigation');
        } else {
          modeBtns[m].classList.add('hidden-mode');
        }
      }
    });
  }

  if (toggleBtn) toggleBtn.style.display = 'block';

  var active = false;
  var graph = null;       // {nodes: Float64Array, adj: Array, geom: Array}
  var originNode = -1;
  var destNode = -1;
  var originCoordE7 = null;
  var destCoordE7 = null;
  var originMarker = null;
  var destMarker = null;
  var routeDrawn = false;
  var lastRoute = null;   // last computed {coords, distance, time, roads} — used by driving mode

  // #search-container and #routing-panel are both absolutely positioned at
  // top: calc(10px + var(--top-inset)) and overlap by 262x79 px at 390 px
  // wide. The panel is z-index 3 and the search box 2, so desktop Chrome
  // hit-tests to the panel and everything looks fine -- but in Kiwix's iOS
  // WKWebView the search box takes the taps, so the origin field is dead and
  // the panel's x is unreachable. Reported from a shipped mexico ZIM.
  // The overlap gate never saw it: the panel is display:none on load, so the
  // detector measured it as absent on all eleven regions that passed.
  // Same treatment the find-results pill already gets (#search-container
  // .streetzim-hidden, !important so a stray inline display can't beat it).
  var searchBoxEl = document.getElementById('search-container');

  function activate() {
    active = true;
    if (toggleBtn) toggleBtn.classList.add('active-control');
    panel.style.display = 'block';
    if (searchBoxEl) searchBoxEl.classList.add('streetzim-hidden');
    map.getCanvas().style.cursor = 'crosshair';
    loadGraph();
  }

  // Toggle routing mode — only wired if the legacy Route button is in
  // the DOM. Otherwise activation happens via streetzimRouting.open().
  if (toggleBtn) {
    toggleBtn.addEventListener('click', function() {
      if (active) deactivate(); else activate();
    });
  }

  closeBtn.addEventListener('click', deactivate);

  // Minimize: collapse the panel to just its header stripe so the
  // map + route are visible underneath. On phones this is essential
  // — the directions panel otherwise covers the whole screen and
  // you can't see the line you're about to drive. Tap the header
  // (or the "▢" glyph) to expand again.
  var minBtn = document.getElementById('routing-minimize');
  var heading = panel.querySelector('h4');
  function toggleMinimized(e) {
    if (e) { e.preventDefault(); e.stopPropagation(); }
    panel.classList.toggle('minimized');
    var folded = panel.classList.contains('minimized');
    minBtn.title = folded ? 'Unfold (show directions)' : 'Fold down (show map)';
    // Tell MapLibre the available canvas size changed so any fit
    // bounds / camera calc uses the new dims. Route line + markers
    // are NOT touched — they stay on the map through the fold cycle.
    try { map.resize(); } catch (err) {}
  }
  if (minBtn) minBtn.addEventListener('click', toggleMinimized);
  // Clicking anywhere on the header while minimized expands. When
  // not minimized the h4 is plain text so we only hook the handler
  // through the class-conditional cursor:pointer rule above.
  heading.addEventListener('click', function(e) {
    // Any click on the folded header expands. The minimize chevron
    // has its own click handler that calls toggleMinimized AND
    // stopPropagation, so it won't double-fire here. The close
    // button is display:none while folded (CSS), so it can't be the
    // origin of a bubbled click in this branch. Used to require
    // e.target === heading; that excluded the new "tap to expand"
    // hint span.
    if (panel.classList.contains('minimized')) {
      toggleMinimized();
    }
  });

  function deactivate() {
    active = false;
    if (toggleBtn) toggleBtn.classList.remove('active-control');
    panel.style.display = 'none';
    panel.classList.remove('minimized');
    if (searchBoxEl) searchBoxEl.classList.remove('streetzim-hidden');
    map.getCanvas().style.cursor = '';
    clearRoute();
  }

  clearBtn.addEventListener('click', function() {
    clearRoute();
    statusEl.textContent = '';
  });

  function clearRoute() {
    // Invalidate any route still computing: its result must neither be
    // drawn after the user cleared, nor satisfy enterDriveMode's
    // lastRoute poll with the OLD endpoints.
    routeSeq++;
    // Snaps still in flight must not bring the cleared route back, nor a
    // planned navigation start fire on a later route.
    originSnapSeq++;
    destSnapSeq++;
    navPlanSeq++;
    if (typeof cancelInFlightRoute === 'function') cancelInFlightRoute();
    stopRouteProgressIndicator();
    originNode = -1;
    destNode = -1;
    originPick = null;
    destPick = null;
    originMoved = false;
    destMoved = false;
    originCoordE7 = null;
    destCoordE7 = null;
    originInput.value = '';
    destInput.value = '';
    originResultsEl.style.display = 'none';
    destResultsEl.style.display = 'none';
    resultEl.style.display = 'none';
    setExpandHint();
    clearBtn.style.display = 'none';
    goRow.classList.remove('visible');
    resetGoButtons();
    if (driveMode.active) driveMode.exit();
    lastRoute = null;
    if (originMarker) { originMarker.remove(); originMarker = null; }
    if (destMarker) { destMarker.remove(); destMarker = null; }
    removeRouteLine();
  }

