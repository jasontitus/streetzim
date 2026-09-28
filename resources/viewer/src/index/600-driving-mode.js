  // --- Driving mode ---
  // Project GPS onto the route polyline, show next-turn distance + ETA,
  // keep camera pitched/zoomed and oriented to the direction of travel.

  function haversineMeters(lat1, lon1, lat2, lon2) {
    var R = 6371000;
    var toRad = Math.PI / 180;
    var dLat = (lat2 - lat1) * toRad;
    var dLon = (lon2 - lon1) * toRad;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
  }

  function bearingDeg(lat1, lon1, lat2, lon2) {
    var toRad = Math.PI / 180, toDeg = 180 / Math.PI;
    var y = Math.sin((lon2 - lon1) * toRad) * Math.cos(lat2 * toRad);
    var x = Math.cos(lat1 * toRad) * Math.sin(lat2 * toRad) -
            Math.sin(lat1 * toRad) * Math.cos(lat2 * toRad) * Math.cos((lon2 - lon1) * toRad);
    return (Math.atan2(y, x) * toDeg + 360) % 360;
  }

  // Augment a route with data the HUD needs:
  //   cumM[i]  — distance from start to coords[i] (meters)
  //   turns[]  — { distM, coordIdx, nameIdx, flags, fromFlags } for each
  //              road-name-or-flags change. flags = road you're entering,
  //              fromFlags = road you're leaving (bit 0 roundabout, bit 1 link).
  function augmentRouteForDriving(route) {
    var coords = route.coords;
    var cumM = new Float64Array(coords.length);
    cumM[0] = 0;
    for (var i = 1; i < coords.length; i++) {
      cumM[i] = cumM[i - 1] + haversineMeters(
        coords[i - 1][1], coords[i - 1][0],
        coords[i][1],     coords[i][0]
      );
    }
    // Map road boundaries (by cumulative metric distance) to coord indices.
    // roads[].distM is the length of each named stretch, so prefix-sum gives
    // the cumulative distance of each boundary between roads[i] and roads[i+1].
    var turns = [];
    var running = 0;
    var roads = route.roads || [];
    var searchFrom = 0;
    for (var r = 0; r < roads.length - 1; r++) {
      running += roads[r].distM;
      // Find first coord whose cumM >= running
      while (searchFrom < coords.length && cumM[searchFrom] < running) searchFrom++;
      var ci = Math.min(searchFrom, coords.length - 1);
      turns.push({
        distM: running,
        coordIdx: ci,
        nameIdx: roads[r + 1].nameIdx,
        flags: roads[r + 1].flags || 0,
        fromFlags: roads[r].flags || 0
      });
    }
    return {
      coords: coords,
      distance: route.distance,
      time: route.time,
      roads: roads,
      cumM: cumM,
      turns: turns
    };
  }

  // Project a point onto the route polyline; return { distAlong, segIdx, bearing }.
  function projectOnRoute(route, lat, lon) {
    var coords = route.coords;
    var cumM = route.cumM;
    var best = { d2: Infinity, segIdx: 0, t: 0 };
    // Equirectangular flat-earth projection around the query point is accurate
    // enough at street-scale (hundreds of meters) and orders of magnitude
    // faster than haversine for each candidate segment.
    var toRad = Math.PI / 180;
    var cosLat = Math.cos(lat * toRad);
    function xy(ll) {
      return [(ll[0] - lon) * cosLat, (ll[1] - lat)];
    }
    for (var i = 0; i < coords.length - 1; i++) {
      var a = xy(coords[i]);
      var b = xy(coords[i + 1]);
      var px = -a[0], py = -a[1];     // vector from a to query (origin)
      var vx = b[0] - a[0], vy = b[1] - a[1];
      var len2 = vx * vx + vy * vy;
      var t = len2 > 0 ? (px * vx + py * vy) / len2 : 0;
      if (t < 0) t = 0; else if (t > 1) t = 1;
      // Closest point on segment, expressed in the local frame where the
      // query point sits at the origin — so its distance to the query is
      // just sqrt(cx^2 + cy^2).
      var cx = a[0] + t * vx, cy = a[1] + t * vy;
      var d2 = cx * cx + cy * cy;
      if (d2 < best.d2) { best.d2 = d2; best.segIdx = i; best.t = t; }
    }
    var si = best.segIdx;
    var distAlong = cumM[si] + best.t * (cumM[si + 1] - cumM[si]);
    var tangent = bearingDeg(
      coords[si][1], coords[si][0],
      coords[si + 1][1], coords[si + 1][0]
    );
    // d2 is in normalized degree units (x scaled by cosLat), so one unit ≈
    // one degree of latitude. ~111.32 km per degree converts to meters.
    var offMeters = Math.sqrt(best.d2) * 111320;
    return { distAlong: distAlong, segIdx: si, t: best.t, bearing: tangent, offMeters: offMeters };
  }

  function formatDistanceLocalized(meters, unit) {
    if (unit === 'imperial') {
      var feet = meters * 3.28084;
      if (feet < 1000) return Math.round(feet / 10) * 10 + ' ft';
      var miles = meters / 1609.344;
      if (miles < 10) return miles.toFixed(1) + ' mi';
      return Math.round(miles) + ' mi';
    }
    if (meters < 1000) return Math.round(meters / 10) * 10 + ' m';
    if (meters < 10000) return (meters / 1000).toFixed(1) + ' km';
    return Math.round(meters / 1000) + ' km';
  }

  function formatEtaClock(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '—';
    var d = new Date(Date.now() + seconds * 1000);
    var h = d.getHours(), m = d.getMinutes();
    var suffix = '';
    try {
      var fmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
      return fmt.format(d);
    } catch (e) {
      var mm = (m < 10 ? '0' : '') + m;
      return h + ':' + mm + suffix;
    }
  }

  // Pick a turn arrow glyph from the bearing delta between the
  // current tangent and the tangent just after the turn.
  function turnGlyph(deltaDeg) {
    // Normalize to [-180, 180]
    var d = ((deltaDeg + 540) % 360) - 180;
    if (d > 135 || d < -135) return '↺';  // U-turn
    if (d > 30)  return '↷';              // right
    if (d < -30) return '↶';              // left
    if (d > 15)  return '↗';              // slight right
    if (d < -15) return '↖';              // slight left
    return '↑';                           // straight
  }

  var driveMode = (function() {
    var hudEl = document.getElementById('drive-hud');
    var distEl = document.getElementById('drive-dist');
    var streetEl = document.getElementById('drive-street');
    var arrowEl = document.getElementById('drive-arrow');
    var exitBtn = document.getElementById('drive-exit');
    var fullscreenBtn = document.getElementById('drive-fullscreen');
    var remainEl = document.getElementById('drive-remaining');
    var etaEl = document.getElementById('drive-eta');
    var statusEl = document.getElementById('drive-status');

    // Elements we hide while driving to reclaim map pixels. Resolved
    // once — some (controls, search-container) are always present; the
    // routing panel is always present too but in a different display
    // state depending on whether the user opened it.
    var controlsEl = document.getElementById('controls');
    var searchEl = document.getElementById('search-container');
    // panel (routing-panel) is already in this function's closure.

    var state = {
      active: false,
      mode: 'drive',
      route: null,
      savedCam: null,
      savedUI: null,       // { panel, controls, search } — prior display values
      watchId: null,
      fallbackTimer: null,
      lastBearing: null,
      offRouteSince: 0,
      lastTick: 0,
      userMarker: null,    // maplibregl.Marker rendering the blue "you are here" dot
      gotFirstFix: false,
      lastLat: null,       // most recent GPS fix — fed to re-route button
      lastLon: null,
      reroutePending: false,
      // True once the driver pinches / tilts / scrolls the map. Locks
      // the follow-camera's zoom + pitch to whatever the driver chose,
      // so we don't snap back on every position tick. Reset via the
      // HUD "recenter" button or on drive-mode exit.
      userPerspective: false,
      wakeLock: null       // Screen Wake Lock sentinel while navigating
    };

    // Screen Wake Lock: a phone that locks mid-navigation stops the GPS
    // watch and the turn prompts (docs/mobile-browser-review.md §B5). The
    // OS releases the lock whenever the page is hidden, so it is asked
    // for again on return. Browsers without the API just do nothing.
    function acquireWakeLock() {
      if (!navigator.wakeLock || !navigator.wakeLock.request) return;
      if (state.wakeLock || document.visibilityState !== 'visible') return;
      navigator.wakeLock.request('screen').then(function(lock) {
        // Exited while the request was pending, or a second request
        // resolved after the first (enter → exit → enter): drop this one.
        if (!state.active || state.wakeLock) {
          try { lock.release().catch(function() {}); } catch (e) {}
          return;
        }
        state.wakeLock = lock;
        lock.addEventListener('release', function() {
          if (state.wakeLock === lock) state.wakeLock = null;
        });
      }).catch(function(err) { dbg('wake lock refused', describeError(err)); });
    }
    function releaseWakeLock() {
      var lock = state.wakeLock;
      state.wakeLock = null;
      if (lock) { try { lock.release().catch(function() {}); } catch (e) {} }
    }
    document.addEventListener('visibilitychange', function() {
      if (state.active && document.visibilityState === 'visible') acquireWakeLock();
    });

    // Fullscreen behavior depends on context:
    //   • Desktop / Android Chrome: real Fullscreen API hides browser
    //     chrome AND our HUD.
    //   • iOS Safari (non-standalone): pseudo-fullscreen scrolls the URL
    //     bar away AND hides our HUD.
    //   • Standalone PWA: there's no browser chrome to chase — tapping
    //     the button just hides our HUD so the driver gets more map.
    //     A floating restore button brings it back.
    var htmlEl = document.documentElement;
    var GLYPH_ENTER = '⛶';  // ⛶ corners-box
    var GLYPH_EXIT = '✕';   // ✕

    function isStandalone() {
      return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
             window.navigator.standalone === true;
    }

    function realFullscreenActive() {
      return !!(document.fullscreenElement || document.webkitFullscreenElement);
    }
    function pseudoFullscreenActive() {
      return htmlEl.classList.contains('drive-fullscreen-pseudo');
    }
    function fullscreenActive() {
      return realFullscreenActive() || pseudoFullscreenActive();
    }

    function enterRealFullscreen() {
      var el = htmlEl;
      var fn = el.requestFullscreen || el.webkitRequestFullscreen;
      if (!fn) return false;
      try {
        var p = fn.call(el);
        // Some browsers (notably iOS Safari) expose requestFullscreen
        // but reject the promise. Fall back to pseudo mode then — and
        // re-sync the glyph, since fullscreenchange won't fire on the
        // rejected path.
        if (p && typeof p.then === 'function') {
          p.catch(function() {
            enterPseudoFullscreen();
            syncFullscreenGlyph();
          });
        }
        return true;
      } catch (e) { return false; }
    }
    function enterPseudoFullscreen() {
      htmlEl.classList.add('drive-fullscreen-pseudo');
      // iOS Safari only collapses its URL bar once the document is
      // actually scrollable AND we scroll. The CSS above makes the body
      // 1px taller; we wait two animation frames for that to commit,
      // then nudge the scroll position and re-size the map to fill the
      // now-larger visible area.
      requestAnimationFrame(function() {
        requestAnimationFrame(function() {
          window.scrollTo(0, 1);
          map.resize();
          // Some iOS builds re-expand the URL bar on the first paint —
          // a second nudge ~300 ms later sticks the collapse.
          setTimeout(function() { window.scrollTo(0, 1); map.resize(); }, 300);
        });
      });
    }
    function exitRealFullscreen() {
      var fn = document.exitFullscreen || document.webkitExitFullscreen;
      if (fn) { try { fn.call(document); } catch (e) {} }
    }
    function exitPseudoFullscreen() {
      htmlEl.classList.remove('drive-fullscreen-pseudo');
      window.scrollTo(0, 0);
      map.resize();
    }

    function syncFullscreenGlyph() {
      var on = fullscreenActive();
      htmlEl.classList.toggle('drive-fullscreen-active', on);
      fullscreenBtn.textContent = on ? GLYPH_EXIT : GLYPH_ENTER;
      fullscreenBtn.title = on ? 'Exit fullscreen' : 'Enter fullscreen';
    }
    // Keep the glyph in sync when the user exits via the ESC key / system
    // gesture rather than our button.
    document.addEventListener('fullscreenchange', syncFullscreenGlyph);
    document.addEventListener('webkitfullscreenchange', syncFullscreenGlyph);
    syncFullscreenGlyph();  // initial paint

    // iOS can't do anything useful with this button:
    //   • iOS Safari has tied URL-bar collapse to touch-scroll gestures
    //     since iOS 13 — window.scrollTo(0,1) is a no-op, and
    //     documentElement.requestFullscreen() either rejects or silently
    //     fails depending on the iOS version.
    //   • iOS PWA / WKWebView (Kiwix iOS) has no browser chrome AND no
    //     working Fullscreen API.
    // Kiwix Desktop (Qt WebEngine), Android, and desktop browsers all do
    // support the real Fullscreen API — show the button there.
    var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
    if (isIOS) {
      fullscreenBtn.style.display = 'none';
    }

    fullscreenBtn.addEventListener('click', function(e) {
      e.stopPropagation();
      if (fullscreenActive()) {
        if (realFullscreenActive()) exitRealFullscreen();
        if (pseudoFullscreenActive()) exitPseudoFullscreen();
      } else {
        if (!enterRealFullscreen()) enterPseudoFullscreen();
      }
      syncFullscreenGlyph();
    });

    exitBtn.addEventListener('click', function() {
      api.exit();
      resetGoButtons();
    });

    // Recenter button — only shows after the driver has touched the
    // camera manually; tapping it snaps back to the mode preset and
    // resumes auto-follow of zoom + pitch.
    var recenterBtn = document.getElementById('drive-recenter');
    recenterBtn.addEventListener('click', function() {
      state.userPerspective = false;
      recenterBtn.style.display = 'none';
      if (state.lastLat != null && state.lastLon != null) {
        var p = MODE_PRESETS[state.mode] || MODE_PRESETS.drive;
        map.easeTo({
          center: [state.lastLon, state.lastLat],
          zoom: p.zoom, pitch: p.pitch,
          duration: 500, easing: function(t) { return t; }
        });
      }
    });

    // Detect driver-initiated camera changes. MapLibre fires zoom/pitch
    // with `originalEvent` set when the change came from a user gesture
    // (touch, wheel). Programmatic easeTo does not set it, so this
    // cleanly separates "driver pinched" from "follow-cam updated".
    function onUserCameraGesture(e) {
      if (!state.active || !e || !e.originalEvent) return;
      if (state.userPerspective) return;
      state.userPerspective = true;
      recenterBtn.style.display = '';
    }
    map.on('zoom', onUserCameraGesture);
    map.on('pitch', onUserCameraGesture);

    var statusTextEl = document.getElementById('drive-status-text');
    var rerouteBtn = document.getElementById('drive-reroute-btn');

    function setStatus(msg, opts) {
      opts = opts || {};
      if (!msg) {
        statusEl.classList.remove('visible');
        if (statusTextEl) statusTextEl.textContent = '';
        if (rerouteBtn) rerouteBtn.style.display = 'none';
        return;
      }
      if (statusTextEl) statusTextEl.textContent = msg;
      if (rerouteBtn) {
        rerouteBtn.style.display = opts.showReroute ? 'inline-block' : 'none';
        rerouteBtn.disabled = !!state.reroutePending;
        rerouteBtn.textContent = state.reroutePending ? 'Re-routing…' : 'Re-route';
      }
      statusEl.classList.add('visible');
    }

    rerouteBtn.addEventListener('click', function() {
      if (state.reroutePending) return;
      if (state.lastLat == null || state.lastLon == null) {
        setStatus('Waiting for GPS fix…');
        return;
      }
      if (destNode === -1) {
        setStatus('No destination set');
        return;
      }
      state.reroutePending = true;
      setStatus('Re-routing from current location…', { showReroute: true });
      // setOriginFromLatLon triggers computeAndDrawRoute, which updates
      // lastRoute + calls driveMode.setRoute on success. One tick of
      // yield so the "Re-routing…" status paints before A* runs.
      setTimeout(function() {
        try {
          setOriginFromLatLon(state.lastLat, state.lastLon, 'Current location');
        } finally {
          state.reroutePending = false;
          // setStatus('') will be called by the next onPosition if the new
          // route puts us back on track. Clear the banner optimistically.
          setStatus('');
          state.offRouteSince = 0;
        }
      }, 0);
    });

    function onPosition(pos) {
      if (!state.active || !state.route) return;
      var lat = pos.coords.latitude, lon = pos.coords.longitude;
      state.lastLat = lat;
      state.lastLon = lon;
      // Mirror the fix into the session-wide cache so the search-pin
      // popup and any post-drive routing keeps one-tap directions.
      window.__streetzimLastLoc = { lat: lat, lon: lon, ts: Date.now() };
      // Place / update the "you are here" dot before doing any route
      // projection — the user wants to see themselves immediately, even
      // if the projection math below throws or the route is weird.
      if (state.userMarker) {
        state.userMarker.setLngLat([lon, lat]);
        if (!state.gotFirstFix) {
          state.userMarker.addTo(map);
          state.gotFirstFix = true;
        }
      }
      var proj = projectOnRoute(state.route, lat, lon);
      var totalDist = state.route.distance;
      var remaining = Math.max(0, totalDist - proj.distAlong);
      // Remaining time. Drive mode uses the route's A*-computed time
      // (car speeds). Walk/bike override with a flat avg speed since the
      // routing graph only has car speeds.
      var preset = MODE_PRESETS[state.mode] || MODE_PRESETS.drive;
      var avgSpeed;
      if (preset.speed != null) {
        avgSpeed = preset.speed;
      } else {
        avgSpeed = state.route.time > 0 ? state.route.distance / state.route.time : 0;
      }
      var remainingTime = avgSpeed > 0 ? remaining / avgSpeed : 0;

      // Find next turn ahead of us
      var nextTurn = null;
      for (var i = 0; i < state.route.turns.length; i++) {
        if (state.route.turns[i].distM > proj.distAlong + 2) {
          nextTurn = state.route.turns[i];
          break;
        }
      }

      var unit = map._streetzimUnit || 'imperial';
      if (nextTurn) {
        var d = nextTurn.distM - proj.distAlong;
        distEl.textContent = formatDistanceLocalized(d, unit);
        // Prefer the name of the immediate next road; if it's unnamed,
        // scan forward through later turns until we find a named one.
        // Better to say "onto Bryant St" (even if we pass an unnamed sliver
        // first) than just "next road".
        var name = '';
        if (graph && graph.getName) {
          name = graph.getName(nextTurn.nameIdx) || '';
          if (!name) {
            for (var t = 0; t < state.route.turns.length; t++) {
              var tt = state.route.turns[t];
              if (tt.distM <= proj.distAlong + 2) continue;
              var nm = graph.getName(tt.nameIdx);
              if (nm) { name = nm; break; }
            }
          }
        }
        // Maneuver cue from SZRG-v4 class_access flags (v2/v3 ZIMs set
        // flags=0, falling through to the geometric arrow below).
        var nextFlags = nextTurn.flags || 0;
        var prevFlags = nextTurn.fromFlags || 0;
        var enteringRound = (nextFlags & 1) && !(prevFlags & 1);
        var exitingRound  = !(nextFlags & 1) && (prevFlags & 1);
        var enteringLink  = (nextFlags & 2) && !(prevFlags & 2);
        if (enteringRound) {
          // Hunt forward for the first named non-roundabout road — that's
          // the actual destination the driver cares about.
          var exitName = '';
          for (var t = 0; t < state.route.turns.length; t++) {
            var tt = state.route.turns[t];
            if (tt.distM <= nextTurn.distM) continue;
            if (tt.flags & 1) continue;
            var nm = graph.getName(tt.nameIdx);
            if (nm) { exitName = nm; break; }
          }
          streetEl.textContent = exitName
            ? ('take roundabout onto ' + exitName)
            : 'take roundabout';
          arrowEl.textContent = '↻';
        } else if (exitingRound) {
          streetEl.textContent = name ? ('exit onto ' + name) : 'exit roundabout';
          arrowEl.textContent = '↱';
        } else if (enteringLink) {
          streetEl.textContent = name ? ('take ramp onto ' + name) : 'take ramp';
          arrowEl.textContent = '↗';
        } else {
          streetEl.textContent = name ? ('onto ' + name) : 'next turn';
          // Tangent just after the turn → rotate arrow relative to current bearing
          var nextCi = Math.min(nextTurn.coordIdx + 1, state.route.coords.length - 1);
          var nextTangent = bearingDeg(
            state.route.coords[nextTurn.coordIdx][1], state.route.coords[nextTurn.coordIdx][0],
            state.route.coords[nextCi][1],           state.route.coords[nextCi][0]
          );
          arrowEl.textContent = turnGlyph(nextTangent - proj.bearing);
        }
      } else {
        // No more turns — you're on the final leg
        distEl.textContent = formatDistanceLocalized(remaining, unit);
        streetEl.textContent = remaining < 30 ? 'Arriving at destination' : 'Continue to destination';
        arrowEl.textContent = remaining < 30 ? '◎' : '↑';
      }

      remainEl.textContent = formatDistanceLocalized(remaining, unit) +
                             ' · ' + formatTime(remainingTime);
      etaEl.textContent = formatEtaClock(remainingTime);

      // Off-route detection — more than 60 m off for >6 s
      if (proj.offMeters > 60) {
        if (!state.offRouteSince) state.offRouteSince = Date.now();
        if (Date.now() - state.offRouteSince > 6000) {
          setStatus('Off route (' + Math.round(proj.offMeters) + ' m)',
                    { showReroute: true });
        }
      } else {
        state.offRouteSince = 0;
        setStatus('');
      }

      // Camera: follow user, orient to heading (prefer GPS heading when moving,
      // fall back to route tangent).
      var speed = pos.coords.speed;  // m/s, may be null
      var heading = pos.coords.heading;  // degrees, may be null or NaN
      var bearing;
      if (typeof heading === 'number' && !isNaN(heading) && speed != null && speed > 1) {
        bearing = heading;
      } else {
        bearing = proj.bearing;
      }
      // Smooth minor jitter
      if (state.lastBearing != null) {
        var delta = ((bearing - state.lastBearing + 540) % 360) - 180;
        if (Math.abs(delta) < 2) bearing = state.lastBearing;
      }
      state.lastBearing = bearing;

      var p = MODE_PRESETS[state.mode] || MODE_PRESETS.drive;
      // Follow the user — always update center + bearing. Leave zoom &
      // pitch alone once the driver has manually pinched or tilted, so
      // their chosen perspective sticks until they tap "recenter" (◎)
      // in the HUD. Auto-overriding every GPS fix made pinch-to-zoom
      // effectively useless while driving.
      var ease = {
        center: [lon, lat],
        bearing: bearing,
        duration: 600,
        easing: function(t) { return t; }
      };
      if (!state.userPerspective) {
        ease.zoom = p.zoom;
        ease.pitch = p.pitch;
      }
      map.easeTo(ease);
    }

    function onPositionError(err) {
      setStatus('Location unavailable: ' + (err && err.message ? err.message : 'error'));
    }

    var api = {
      get active() { return state.active; },
      setRoute: function(r) { state.route = r; state.lastBearing = null; },
      enter: function(mode) {
        if (state.active) return;
        if (!lastRoute) return;
        if (!navigator.geolocation) {
          setStatus('Geolocation not supported in this browser');
          return;
        }
        state.mode = MODE_PRESETS[mode] ? mode : 'drive';
        state.active = true;
        state.route = lastRoute;
        acquireWakeLock();
        state.savedCam = {
          center: map.getCenter(),
          zoom: map.getZoom(),
          bearing: map.getBearing(),
          pitch: map.getPitch()
        };
        // Hide the routing panel + top-right control cluster + search
        // box so the driver sees as much map as possible. Save prior
        // inline-display so exit() restores whatever was there.
        state.savedUI = {
          panel:    panel.style.display,
          controls: controlsEl ? controlsEl.style.display : '',
          search:   searchEl ? searchEl.style.display : ''
        };
        panel.style.display = 'none';
        if (controlsEl) controlsEl.style.display = 'none';
        if (searchEl)   searchEl.style.display = 'none';
        // Fresh session starts with auto-follow on and recenter hidden.
        state.userPerspective = false;
        recenterBtn.style.display = 'none';

        hudEl.classList.add('visible');
        hudEl.setAttribute('aria-hidden', 'false');
        distEl.textContent = '—';
        streetEl.textContent = 'Getting location…';
        arrowEl.textContent = '↑';
        remainEl.textContent = '—';
        etaEl.textContent = '—';
        setStatus('');

        // Create the user-location dot. Added to the map only after the
        // first GPS fix arrives (in onPosition) — adding a marker with
        // no lngLat would plant it at [0,0].
        state.gotFirstFix = false;
        var dotEl = document.createElement('div');
        dotEl.className = 'drive-user-dot';
        state.userMarker = new maplibregl.Marker({ element: dotEl });
        // Immediate camera shift so the user sees the perspective change
        // before the first fix arrives.
        var p = MODE_PRESETS[state.mode];
        map.easeTo({ pitch: p.pitch, zoom: p.zoom, duration: 600 });
        try {
          state.watchId = navigator.geolocation.watchPosition(
            onPosition,
            onPositionError,
            { enableHighAccuracy: true, timeout: 15000, maximumAge: 2000 }
          );
        } catch (e) {
          // Full teardown via exit(): restores the hidden panel /
          // controls / search box and the saved camera. Before this
          // only the HUD was hidden, leaving the page with no UI and
          // no way back short of a reload.
          api.exit();
          setStatus('Could not start location tracking');
        }
      },
      exit: function() {
        if (!state.active) return;
        state.active = false;
        releaseWakeLock();
        if (state.watchId != null) {
          try { navigator.geolocation.clearWatch(state.watchId); } catch (e) {}
          state.watchId = null;
        }
        if (state.userMarker) {
          try { state.userMarker.remove(); } catch (e) {}
          state.userMarker = null;
          state.gotFirstFix = false;
        }
        hudEl.classList.remove('visible');
        hudEl.setAttribute('aria-hidden', 'true');
        if (realFullscreenActive()) exitRealFullscreen();
        if (pseudoFullscreenActive()) exitPseudoFullscreen();
        syncFullscreenGlyph();
        if (state.savedUI) {
          panel.style.display    = state.savedUI.panel;
          if (controlsEl) controlsEl.style.display = state.savedUI.controls;
          if (searchEl)   searchEl.style.display   = state.savedUI.search;
          state.savedUI = null;
        }
        if (state.savedCam) {
          map.easeTo({
            center: state.savedCam.center,
            zoom: state.savedCam.zoom,
            bearing: state.savedCam.bearing,
            pitch: state.savedCam.pitch,
            duration: 800
          });
          state.savedCam = null;
        }
        state.route = null;
        state.lastBearing = null;
        state.offRouteSince = 0;
        state.userPerspective = false;
        recenterBtn.style.display = 'none';
      }
    };
    return api;
  })();

  Object.keys(modeBtns).forEach(function(mode) {
    modeBtns[mode].addEventListener('click', function() {
      if (!lastRoute) return;
      if (driveMode.active) {
        driveMode.exit();
        resetGoButtons();
        return;
      }
      driveMode.enter(mode);
      if (driveMode.active) {
        Object.keys(modeBtns).forEach(function(m) {
          if (m === mode) {
            modeBtns[m].classList.add('active-mode');
            modeBtns[m].textContent = 'Exit';
          } else {
            modeBtns[m].classList.add('hidden-mode');
          }
        });
      }
    });
  });

