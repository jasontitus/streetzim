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
  // Per-mode presets — camera feels right for the modality and the avg
  // speed is used to re-estimate remaining-time since the routing graph
  // is driver-speeds only. Speeds in m/s: walk≈3.1 mph, bike≈10 mph.
  var MODE_PRESETS = {
    drive: { pitch: 60, zoom: 17,   speed: null },  // null = use route's car-speed
    walk:  { pitch: 45, zoom: 18,   speed: 1.4 },
    bike:  { pitch: 55, zoom: 17.5, speed: 4.5 }
  };

  function resetGoButtons() {
    Object.keys(modeBtns).forEach(function(m) {
      modeBtns[m].classList.remove('active-mode', 'hidden-mode');
      modeBtns[m].textContent = MODE_LABELS[m];
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
    if (typeof cancelInFlightRoute === 'function') cancelInFlightRoute();
    stopRouteProgressIndicator();
    originNode = -1;
    destNode = -1;
    originCoordE7 = null;
    destCoordE7 = null;
    originInput.value = '';
    destInput.value = '';
    originResultsEl.style.display = 'none';
    destResultsEl.style.display = 'none';
    resultEl.style.display = 'none';
    clearBtn.style.display = 'none';
    goRow.classList.remove('visible');
    resetGoButtons();
    if (driveMode.active) driveMode.exit();
    lastRoute = null;
    if (originMarker) { originMarker.remove(); originMarker = null; }
    if (destMarker) { destMarker.remove(); destMarker = null; }
    removeRouteLine();
  }

